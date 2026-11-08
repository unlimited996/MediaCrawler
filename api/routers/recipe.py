# -*- coding: utf-8 -*-
"""菜谱域 API（KitchenHelper）。

三组路由，统一挂在 ``/api`` 下：

菜谱（prefix=/recipes）
    GET    /recipes                      列表：搜索 + 分类 + 标签 + 难度 + 耗时 + 分页
    GET    /recipes/categories           分类（带菜谱数）
    GET    /recipes/tags                 标签（带菜谱数）
    GET    /recipes/stats                库统计
    GET    /recipes/bundle               整包（catalog.json 同构，支持 ETag / gzip）
    POST   /recipes/import               整包导入（管理用，幂等）
    POST   /recipes                      upsert 单个（含配料/步骤/标签）
    GET    /recipes/{id}                 详情（含配料、步骤、标签、相似推荐）
    PUT    /recipes/{id}                 部分更新
    DELETE /recipes/{id}                 删除（子表级联）

媒体（prefix=/media）
    GET/HEAD /media/{key}                代理 S3 对象（ETag 协商 + 长缓存）

配置（prefix=/app-config）
    GET    /app-config                   全量（{key: value}）
    GET    /app-config/{key}
    PUT    /app-config/{key}
    DELETE /app-config/{key}

返回体约定：菜谱对象用 camelCase（与 App 现有 catalog.json 解析、深链 id 一致），
分页信封用 snake_case（与后端其它路由一致）。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import case, delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from database.models_recipe import (
    AppConfig, DIFFICULTY_VALUES, RECIPE_STATUS_VALUES, Recipe, RecipeCategory,
    RecipeIngredient, RecipeStep, RecipeTag, RecipeTagLink,
)
from .story import _session

from ..service_paths import library_api

router = APIRouter(prefix="/recipes", tags=["recipe"])
media_router = APIRouter(prefix="/media", tags=["media"])
app_config_router = APIRouter(prefix="/app-config", tags=["app-config"])

#: 与 catalog.json 的结构版本号保持一致，客户端可据此判断兼容性
CATALOG_VERSION = 1

#: 列表默认页大小
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200

#: 整包进程内缓存 TTL（秒）。写操作会立即失效，所以这里只需挡住读放大。
BUNDLE_TTL = 30.0
_BUNDLE_CACHE: Dict[str, Any] = {"etag": "", "raw": b"", "count": 0, "at": 0.0}


def _bundle_cache_get() -> Optional[Dict[str, Any]]:
    if _BUNDLE_CACHE["raw"] and (time.monotonic() - _BUNDLE_CACHE["at"]) < BUNDLE_TTL:
        return _BUNDLE_CACHE
    return None


def invalidate_bundle_cache() -> None:
    """任何写操作后调用，下次读重建整包。"""
    _BUNDLE_CACHE.update({"etag": "", "raw": b"", "count": 0, "at": 0.0})


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _num(v: Optional[Decimal]) -> Optional[float]:
    return float(v) if v is not None else None


def _media_url(key: Optional[str]) -> Optional[str]:
    """对象键 → 相对访问地址。客户端用自身 base 拼绝对地址。"""
    if not key:
        return None
    return library_api("media/") + "/".join(
        _quote_segment(seg) for seg in key.split("/") if seg)


def _quote_segment(seg: str) -> str:
    from urllib.parse import quote

    return quote(seg, safe="")


def _escape_like(s: str) -> str:
    """转义 LIKE 通配符，避免用户输入 `%` / `_` 变成通配。"""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _recipe_brief(r: Recipe, category_name: Optional[str],
                  tags: Sequence[str] = ()) -> Dict[str, Any]:
    return {
        "id": r.id,
        "title": r.title,
        "categoryId": r.category_id,
        "categoryName": category_name,
        "coverKey": r.cover_key,
        "processKey": r.process_key,
        "coverUrl": _media_url(r.cover_key),
        "processUrl": _media_url(r.process_key),
        "intro": r.intro,
        "servings": r.servings,
        "durationMinutes": r.duration_minutes,
        "difficulty": r.difficulty,
        "tags": list(tags),
    }


# ---------------------------------------------------------------------------
# 分类 / 标签
# ---------------------------------------------------------------------------
@router.get("/categories")
async def list_categories():
    """全部分类（按 sort_order 排），带菜谱数。"""
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        rows = (await session.execute(
            select(RecipeCategory).order_by(RecipeCategory.sort_order,
                                            RecipeCategory.id))).scalars().all()
        counts = dict((await session.execute(
            select(Recipe.category_id, func.count())
            .group_by(Recipe.category_id))).all())
        return {"items": [
            {"id": c.id, "name": c.name, "sortOrder": c.sort_order,
             "recipeCount": counts.get(c.id, 0)}
            for c in rows
        ]}
    finally:
        await session.close()
        await engine.dispose()


@router.get("/tags")
async def list_tags():
    """全部标签（按菜谱数降序），带使用次数。"""
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        rows = (await session.execute(
            select(RecipeTag).order_by(RecipeTag.sort_order, RecipeTag.id))).scalars().all()
        counts = dict((await session.execute(
            select(RecipeTagLink.tag_id, func.count())
            .group_by(RecipeTagLink.tag_id))).all())
        items = [{"id": t.id, "name": t.name, "slug": t.slug, "kind": t.kind,
                  "recipeCount": counts.get(t.id, 0)} for t in rows]
        items.sort(key=lambda x: (-x["recipeCount"], x["id"]))
        return {"items": items}
    finally:
        await session.close()
        await engine.dispose()


@router.get("/stats")
async def recipe_stats():
    """库统计：总数、有图数、按分类/难度分布。"""
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        total = (await session.execute(
            select(func.count()).select_from(Recipe))).scalar() or 0
        with_cover = (await session.execute(
            select(func.count()).select_from(Recipe)
            .where(Recipe.cover_key.is_not(None)))).scalar() or 0
        with_process = (await session.execute(
            select(func.count()).select_from(Recipe)
            .where(Recipe.process_key.is_not(None)))).scalar() or 0
        by_cat = (await session.execute(
            select(Recipe.category_id, func.count())
            .group_by(Recipe.category_id))).all()
        by_diff = (await session.execute(
            select(Recipe.difficulty, func.count())
            .group_by(Recipe.difficulty))).all()
        return {
            "recipes": total,
            "withCover": with_cover,
            "withProcess": with_process,
            "categories": len(by_cat),
            "byCategory": {k: v for k, v in by_cat},
            "byDifficulty": {k or "unknown": v for k, v in by_diff},
            "counts": {
                "ingredients": (await session.execute(
                    select(func.count()).select_from(RecipeIngredient))).scalar() or 0,
                "steps": (await session.execute(
                    select(func.count()).select_from(RecipeStep))).scalar() or 0,
                "tagLinks": (await session.execute(
                    select(func.count()).select_from(RecipeTagLink))).scalar() or 0,
            },
        }
    finally:
        await session.close()
        await engine.dispose()


# ---------------------------------------------------------------------------
# 整包（与 catalog.json 同构，App 可直接替换数据来源）
# ---------------------------------------------------------------------------
async def _build_bundle(session: AsyncSession) -> Dict[str, Any]:
    cats = (await session.execute(
        select(RecipeCategory).order_by(RecipeCategory.sort_order))).scalars().all()
    tags = (await session.execute(
        select(RecipeTag).order_by(RecipeTag.id))).scalars().all()
    recipes = (await session.execute(
        select(Recipe).where(Recipe.status != "archived")
        .order_by(Recipe.sort_order, Recipe.id))).scalars().all()
    ings = (await session.execute(
        select(RecipeIngredient).order_by(RecipeIngredient.recipe_id,
                                          RecipeIngredient.seq))).scalars().all()
    steps = (await session.execute(
        select(RecipeStep).order_by(RecipeStep.recipe_id,
                                    RecipeStep.seq))).scalars().all()
    links = (await session.execute(
        select(RecipeTagLink))).scalars().all()

    # archived 的菜谱不进整包，其子行也一并剔掉，避免客户端拿到孤立数据
    visible = {r.id for r in recipes}
    ings = [i for i in ings if i.recipe_id in visible]
    steps = [s for s in steps if s.recipe_id in visible]
    links = [l for l in links if l.recipe_id in visible]

    cat_name = {c.id: c.name for c in cats}
    tag_name = {t.id: t.name for t in tags}
    tags_by_recipe: Dict[str, List[str]] = {}
    for l in links:
        if l.tag_id in tag_name:
            tags_by_recipe.setdefault(l.recipe_id, []).append(tag_name[l.tag_id])

    return {
        "version": CATALOG_VERSION,
        "generatedAt": max((r.updated_at.isoformat() for r in recipes if r.updated_at),
                           default=None),
        "categories": [{"id": c.id, "name": c.name, "sortOrder": c.sort_order}
                       for c in cats],
        "tags": [{"id": t.id, "name": t.name, "slug": t.slug} for t in tags],
        "recipes": [{
            "id": r.id,
            "title": r.title,
            "categoryId": r.category_id,
            "categoryName": cat_name.get(r.category_id),
            "coverKey": r.cover_key,
            "processKey": r.process_key,
            "intro": r.intro,
            "servings": r.servings,
            "durationMinutes": r.duration_minutes,
            "difficulty": r.difficulty,
            "variants": r.variants or [],
            "tags": tags_by_recipe.get(r.id, []),
        } for r in recipes],
        "ingredients": [{
            "id": i.id, "recipeId": i.recipe_id, "seq": i.seq, "name": i.name,
            "amount": _num(i.amount), "unit": i.unit, "note": i.note,
        } for i in ings],
        "steps": [{
            "id": s.id, "recipeId": s.recipe_id, "seq": s.seq,
            "description": s.description, "imageKey": s.image_key,
            "durationSeconds": s.duration_seconds,
        } for s in steps],
        "recipeTags": [{"recipeId": l.recipe_id, "tagId": l.tag_id} for l in links],
    }


@router.get("/bundle")
async def get_bundle(request: Request):
    """整包下发（约 310KB，gzip 后约 41KB）。

    - ETag 用内容哈希，任何写操作都会变；支持 ``If-None-Match`` → 304
    - ``Accept-Encoding: gzip`` 时压缩到约 1/7
    - 进程内缓存 30s（写操作立即失效），避免每次请求都重建 2471 行
    """
    cached = _bundle_cache_get()
    if cached is None:
        engine, session = await _session()
        if session is None:
            raise HTTPException(status_code=503, detail="配置库不可用")
        try:
            bundle = await _build_bundle(session)
        finally:
            await session.close()
            await engine.dispose()
        raw = json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        _BUNDLE_CACHE.update({
            "etag": '"' + hashlib.md5(raw).hexdigest() + '"',
            "raw": raw,
            "count": len(bundle["recipes"]),
            "at": time.monotonic(),
        })
        cached = _BUNDLE_CACHE

    etag: str = cached["etag"]
    raw: bytes = cached["raw"]
    headers = {
        "ETag": etag,
        "Cache-Control": "public, max-age=300",
        "Vary": "Accept-Encoding",
        "X-Catalog-Count": str(cached["count"]),
    }
    if request.headers.get("if-none-match", "").strip() == etag:
        return Response(status_code=304, headers=headers)
    if "gzip" in (request.headers.get("accept-encoding") or "").lower():
        return Response(content=gzip.compress(raw, 6), media_type="application/json",
                        headers={**headers, "Content-Encoding": "gzip"})
    return Response(content=raw, media_type="application/json", headers=headers)


# ---------------------------------------------------------------------------
# 列表 / 搜索
# ---------------------------------------------------------------------------
@router.get("")
async def list_recipes(
    q: Optional[str] = Query(None, description="关键词：菜名/食材/标签/分类，空格分词 AND"),
    category: Optional[str] = Query(None, description="分类 id 或中文名"),
    tags: Optional[str] = Query(None, description="标签名，逗号分隔"),
    tag_mode: str = Query("any", pattern="^(any|all)$"),
    difficulty: Optional[str] = Query(None, description="L1~L5"),
    max_duration: Optional[int] = Query(None, ge=1, description="耗时上限（分钟）"),
    sort: str = Query("relevance", pattern="^(relevance|title|duration|difficulty)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
):
    """菜谱列表 + 检索。

    检索走 ``recipe.search_text``（标题+分类+食材+标签的冗余列，pg_trgm GIN 索引），
    多词之间是 AND；排序按「菜名全等 > 菜名包含 > 其它」加权。
    """
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        stmt = select(Recipe).where(Recipe.status == "published")

        if category:
            cat_id = category
            if not (await session.execute(
                    select(RecipeCategory.id).where(RecipeCategory.id == category))
            ).scalar():
                cat_id = (await session.execute(
                    select(RecipeCategory.id).where(RecipeCategory.name == category)
                )).scalar() or category
            stmt = stmt.where(Recipe.category_id == cat_id)

        if difficulty:
            stmt = stmt.where(Recipe.difficulty == difficulty.upper())
        if max_duration:
            stmt = stmt.where(Recipe.duration_minutes.is_not(None),
                              Recipe.duration_minutes <= max_duration)

        terms: List[str] = []
        if q:
            terms = [t for t in q.replace("\u3000", " ").split() if t]
            for t in terms:
                stmt = stmt.where(Recipe.search_text.ilike(
                    f"%{_escape_like(t)}%", escape="\\"))

        if tags:
            names = [x.strip() for x in tags.split(",") if x.strip()]
            if names:
                tag_ids = select(RecipeTag.id).where(RecipeTag.name.in_(names))
                sub = (select(RecipeTagLink.recipe_id)
                       .where(RecipeTagLink.tag_id.in_(tag_ids))
                       .group_by(RecipeTagLink.recipe_id))
                if tag_mode == "all":
                    sub = sub.having(
                        func.count(func.distinct(RecipeTagLink.tag_id)) == len(names))
                stmt = stmt.where(Recipe.id.in_(sub))

        total = (await session.execute(
            select(func.count()).select_from(stmt.subquery()))).scalar() or 0

        if sort == "title":
            stmt = stmt.order_by(Recipe.title)
        elif sort == "duration":
            stmt = stmt.order_by(Recipe.duration_minutes.is_(None),
                                 Recipe.duration_minutes, Recipe.id)
        elif sort == "difficulty":
            stmt = stmt.order_by(Recipe.difficulty, Recipe.id)
        elif q:
            stmt = stmt.order_by(
                case((Recipe.title == q, 100),
                     (Recipe.title.ilike(f"%{_escape_like(q)}%", escape="\\"), 60),
                     else_=10).desc(),
                Recipe.sort_order, Recipe.id)
        else:
            stmt = stmt.order_by(Recipe.sort_order, Recipe.id)

        rows = (await session.execute(
            stmt.offset((page - 1) * page_size).limit(page_size))).scalars().all()
        cat_map = dict((await session.execute(
            select(RecipeCategory.id, RecipeCategory.name))).all())
        tag_map = await _tags_for(session, [r.id for r in rows])

        return {
            "items": [_recipe_brief(r, cat_map.get(r.category_id),
                                    tag_map.get(r.id, [])) for r in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "query": {"q": q, "category": category, "tags": tags,
                      "tag_mode": tag_mode, "difficulty": difficulty,
                      "max_duration": max_duration, "sort": sort},
        }
    finally:
        await session.close()
        await engine.dispose()


async def _tags_for(session: AsyncSession,
                    recipe_ids: Sequence[str]) -> Dict[str, List[str]]:
    if not recipe_ids:
        return {}
    rows = (await session.execute(
        select(RecipeTagLink.recipe_id, RecipeTag.name)
        .join(RecipeTag, RecipeTag.id == RecipeTagLink.tag_id)
        .where(RecipeTagLink.recipe_id.in_(list(recipe_ids)))
        .order_by(RecipeTag.id))).all()
    out: Dict[str, List[str]] = {}
    for rid, name in rows:
        out.setdefault(rid, []).append(name)
    return out


# ---------------------------------------------------------------------------
# 详情 / 相关推荐
# ---------------------------------------------------------------------------
@router.get("/{recipe_id:path}")
async def get_recipe(recipe_id: str, related: int = Query(6, ge=0, le=20)):
    """菜谱详情：配料（按 seq）、步骤（按 seq）、标签、相似菜谱。"""
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        r = (await session.execute(
            select(Recipe).where(Recipe.id == recipe_id))).scalar_one_or_none()
        if r is None:
            raise HTTPException(status_code=404, detail=f"菜谱不存在：{recipe_id}")

        ings = (await session.execute(
            select(RecipeIngredient).where(RecipeIngredient.recipe_id == r.id)
            .order_by(RecipeIngredient.seq))).scalars().all()
        steps = (await session.execute(
            select(RecipeStep).where(RecipeStep.recipe_id == r.id)
            .order_by(RecipeStep.seq))).scalars().all()
        cat_name = (await session.execute(
            select(RecipeCategory.name).where(RecipeCategory.id == r.category_id)
        )).scalar()
        tag_rows = (await session.execute(
            select(RecipeTag.id, RecipeTag.name)
            .join(RecipeTagLink, RecipeTagLink.tag_id == RecipeTag.id)
            .where(RecipeTagLink.recipe_id == r.id)
            .order_by(RecipeTag.id))).all()

        body = _recipe_brief(r, cat_name, [t[1] for t in tag_rows])
        body.update({
            "tagIds": [t[0] for t in tag_rows],
            "variants": r.variants or [],
            "status": r.status,
            "searchText": r.search_text,
            "ingredients": [{
                "id": i.id, "seq": i.seq, "name": i.name,
                "amount": _num(i.amount), "unit": i.unit, "note": i.note,
            } for i in ings],
            "steps": [{
                "id": s.id, "seq": s.seq, "description": s.description,
                "imageKey": s.image_key,
                "imageUrl": _media_url(s.image_key),
                "durationSeconds": s.duration_seconds,
            } for s in steps],
            "updatedAt": r.updated_at.isoformat() if r.updated_at else None,
        })
        body["related"] = await _related(session, r.id, tag_rows, related) if related else []
        return body
    finally:
        await session.close()
        await engine.dispose()


async def _related(session: AsyncSession, recipe_id: str,
                   tag_rows: Sequence[Tuple[int, str]], limit: int) -> List[Dict[str, Any]]:
    """按共享标签数推荐相似菜谱（同分类加权），排除自己。"""
    if limit <= 0:
        return []
    cat_id = (await session.execute(
        select(Recipe.category_id).where(Recipe.id == recipe_id))).scalar()
    tag_ids = [t[0] for t in tag_rows]

    shared = func.count(func.distinct(RecipeTagLink.tag_id)).label("shared")
    stmt = (select(Recipe, shared)
            .join(RecipeTagLink, RecipeTagLink.recipe_id == Recipe.id)
            .where(RecipeTagLink.tag_id.in_(tag_ids) if tag_ids else text("false"),
                   Recipe.id != recipe_id,
                   Recipe.status == "published")
            .group_by(Recipe.id)
            .order_by(shared.desc(), Recipe.id)
            .limit(limit))
    rows = (await session.execute(stmt)).all()
    if not rows and not tag_ids:
        rows = []

    cat_map = dict((await session.execute(
        select(RecipeCategory.id, RecipeCategory.name))).all())
    tag_map = await _tags_for(session, [r.id for r, _ in rows])
    out = []
    for r, n in rows:
        item = _recipe_brief(r, cat_map.get(r.category_id), tag_map.get(r.id, []))
        item["sharedTags"] = int(n)
        item["sameCategory"] = (r.category_id == cat_id)
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# 写入（管理用）
# ---------------------------------------------------------------------------
def _rebuild_search_text(r: Recipe, category_name: str,
                         ings: Sequence[Any], tag_names: Sequence[str]) -> str:
    from tools.recipe_import import build_search_text

    return build_search_text(
        r.title, category_name,
        [i.name if hasattr(i, "name") else str(i) for i in ings],
        tag_names,
    )


async def _apply_payload(session: AsyncSession, r: Recipe, payload: Dict[str, Any]) -> None:
    """把请求体写到 Recipe 行（只写出现的字段），并同步 search_text。"""
    scalar_fields = {
        "title": "title", "categoryId": "category_id", "coverKey": "cover_key",
        "processKey": "process_key", "intro": "intro", "servings": "servings",
        "durationMinutes": "duration_minutes", "difficulty": "difficulty",
        "variants": "variants", "status": "status", "sortOrder": "sort_order",
    }
    if payload.get("difficulty") and payload["difficulty"] not in DIFFICULTY_VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"difficulty 只能是 {', '.join(DIFFICULTY_VALUES)}")
    if payload.get("status") and payload["status"] not in RECIPE_STATUS_VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"status 只能是 {', '.join(RECIPE_STATUS_VALUES)}")
    for src, attr in scalar_fields.items():
        if src in payload:
            val = payload.get(src)
            if src == "categoryId" and val:
                exists = (await session.execute(
                    select(RecipeCategory.id).where(RecipeCategory.id == val))).scalar()
                if not exists:
                    raise HTTPException(status_code=400, detail=f"分类不存在：{val}")
            setattr(r, attr, val)

    if r.servings is None:
        r.servings = 1
    if not r.id or not r.title:
        raise HTTPException(status_code=400, detail="id / title 必填")

    cat_name = (await session.execute(
        select(RecipeCategory.name).where(RecipeCategory.id == r.category_id)
    )).scalar() or ""

    # 配料
    if "ingredients" in payload:
        await session.execute(
            delete(RecipeIngredient).where(RecipeIngredient.recipe_id == r.id))
        rows = []
        for idx, i in enumerate(payload.get("ingredients") or [], start=1):
            name = (i.get("name") or "").strip()
            if not name:
                continue
            amount = i.get("amount")
            unit = (i.get("unit") or "").strip() or None
            if amount is not None and not unit:
                unit = "g"
            rows.append(dict(recipe_id=r.id, seq=int(i.get("seq") or idx),
                             name=name, amount=amount, unit=unit,
                             note=i.get("note")))
        if rows:
            await session.execute(RecipeIngredient.__table__.insert(), rows)

    # 步骤
    if "steps" in payload:
        await session.execute(delete(RecipeStep).where(RecipeStep.recipe_id == r.id))
        rows = []
        for idx, s in enumerate(payload.get("steps") or [], start=1):
            desc = (s.get("description") or "").strip()
            if not desc:
                continue
            rows.append(dict(recipe_id=r.id, seq=int(s.get("seq") or idx),
                             description=desc,
                             image_key=s.get("imageKey"),
                             duration_seconds=s.get("durationSeconds")))
        if rows:
            await session.execute(RecipeStep.__table__.insert(), rows)

    # 标签（按名字，缺的自动建）
    if "tags" in payload:
        names = [n.strip() for n in (payload.get("tags") or []) if n and n.strip()]
        await session.execute(
            delete(RecipeTagLink).where(RecipeTagLink.recipe_id == r.id))
        for name in names:
            tag_id = (await session.execute(
                select(RecipeTag.id).where(RecipeTag.name == name))).scalar()
            if tag_id is None:
                slug = _slugify(name)
                tag = RecipeTag(name=name, slug=slug, kind="manual")
                session.add(tag)
                await session.flush()
                tag_id = tag.id
            await session.execute(RecipeTagLink.__table__.insert(),
                                  [dict(recipe_id=r.id, tag_id=tag_id)])

    ings = (await session.execute(
        select(RecipeIngredient).where(RecipeIngredient.recipe_id == r.id))).scalars().all()
    tag_names = [x for x in (await session.execute(
        select(RecipeTag.name)
        .join(RecipeTagLink, RecipeTagLink.tag_id == RecipeTag.id)
        .where(RecipeTagLink.recipe_id == r.id))).scalars().all()]
    r.search_text = _rebuild_search_text(r, cat_name, ings, tag_names)


def _slugify(name: str) -> str:
    """中文标签没有天然 slug，用 tag-<md5 前 8 位> 占位并保证唯一。"""
    return "tag-" + hashlib.md5(name.encode("utf-8")).hexdigest()[:8]


@router.post("")
async def upsert_recipe(payload: Dict[str, Any] = Body(default={})):
    """新增或整体覆盖一个菜谱（含配料/步骤/标签，传了就整段替换）。"""
    rid = (payload.get("id") or "").strip()
    if not rid:
        raise HTTPException(status_code=400, detail="id 必填（形如 分类/菜名）")
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        r = (await session.execute(
            select(Recipe).where(Recipe.id == rid))).scalar_one_or_none()
        created = r is None
        if created:
            if not payload.get("categoryId"):
                raise HTTPException(status_code=400, detail="categoryId 必填")
            r = Recipe(id=rid, title=payload.get("title") or rid.split("/")[-1],
                       category_id=payload["categoryId"])
            session.add(r)
            await session.flush()
        await _apply_payload(session, r, payload)
        await session.commit()
        await session.refresh(r)
        invalidate_bundle_cache()
        return {"success": True, "created": created, "id": r.id,
                "updatedAt": r.updated_at.isoformat() if r.updated_at else None}
    finally:
        await session.close()
        await engine.dispose()


@router.put("/{recipe_id:path}")
async def update_recipe(recipe_id: str, payload: Dict[str, Any] = Body(default={})):
    """部分更新：只改请求里出现的字段；配料/步骤/标签传了就整段替换。"""
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        r = (await session.execute(
            select(Recipe).where(Recipe.id == recipe_id))).scalar_one_or_none()
        if r is None:
            raise HTTPException(status_code=404, detail=f"菜谱不存在：{recipe_id}")
        await _apply_payload(session, r, payload)
        await session.commit()
        await session.refresh(r)
        invalidate_bundle_cache()
        return {"success": True, "id": r.id,
                "updatedAt": r.updated_at.isoformat() if r.updated_at else None}
    finally:
        await session.close()
        await engine.dispose()


@router.delete("/{recipe_id:path}")
async def delete_recipe(recipe_id: str):
    """删除菜谱（配料/步骤/标签关联由外键级联清除）。"""
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        res = await session.execute(delete(Recipe).where(Recipe.id == recipe_id))
        await session.commit()
        if not res.rowcount:
            raise HTTPException(status_code=404, detail=f"菜谱不存在：{recipe_id}")
        invalidate_bundle_cache()
        return {"success": True, "deleted": recipe_id}
    finally:
        await session.close()
        await engine.dispose()


@router.post("/import")
async def import_all(payload: Dict[str, Any] = Body(default={})):
    """整包导入（幂等）。body 为 catalog.json 结构；传 {"reset": true} 可先清空。"""
    if not payload or "recipes" not in payload:
        raise HTTPException(status_code=400, detail="请求体需为 catalog.json 结构（含 recipes）")
    from tools.recipe_import import import_catalog

    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        stats = await import_catalog(session, payload,
                                     reset=bool(payload.get("reset")))
        await session.commit()
        invalidate_bundle_cache()
        return {"success": True, **stats}
    finally:
        await session.close()
        await engine.dispose()


# ---------------------------------------------------------------------------
# 媒体代理（Garage 私有桶 → 带缓存头的公开读）
# ---------------------------------------------------------------------------
@media_router.get("/{key:path}")
async def serve_media(key: str, request: Request):
    """代理 S3 对象（GET）。

    Garage 目前不支持公共读，客户端直连会 403；这里由服务端签名转发。
    带 ETag + 长缓存：客户端第二次起走 304，不产生回源流量。
    """
    return await _serve_media(key, request)


@media_router.head("/{key:path}")
async def head_media(key: str, request: Request):
    """同 ``serve_media``，只回头信息（不吐 body）。

    单独声明而不是 api_route(methods=["GET","HEAD"])：后者会给两个方法生成
    同一个 operation id，/docs 与 openapi 生成时会告警。
    """
    return await _serve_media(key, request)


async def _serve_media(key: str, request: Request):
    from tools.media_backup import active_settings
    from tools.s3_client import call

    if not key or ".." in key:
        raise HTTPException(status_code=400, detail="非法对象键")

    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        cfg = await active_settings(session)
        if cfg is None:
            raise HTTPException(status_code=503, detail="未配置可用的 S3 存储")
        settings = cfg["settings"]

        meta: Dict[str, Any] = {}
        try:
            meta = await call(settings, "head_object",
                              Bucket=settings.bucket, Key=key)
        except Exception as e:
            code = ""
            try:
                code = str(e.response.get("Error", {}).get("Code", ""))
            except Exception:
                pass
            if code in ("404", "NoSuchKey", "NotFound"):
                raise HTTPException(status_code=404, detail="对象不存在")
            raise HTTPException(status_code=502, detail=f"S3 读取失败: {e}")

        etag = (meta.get("ETag") or "").strip('"')
        ctype = meta.get("ContentType") or "application/octet-stream"
        length = meta.get("ContentLength")
        headers = {
            "Cache-Control": "public, max-age=604800, stale-while-revalidate=86400",
            "Vary": "Accept-Encoding",
        }
        if etag:
            headers["ETag"] = f'"{etag}"'
        if length is not None:
            headers["Content-Length"] = str(length)

        if etag and request.headers.get("if-none-match", "").strip('"') == etag:
            return Response(status_code=304, headers=headers)
        if request.method == "HEAD":
            return Response(status_code=200, headers=headers, media_type=ctype)

        resp = await call(settings, "get_object", Bucket=settings.bucket, Key=key)
        body = resp.get("Body")
        if body is None:
            raise HTTPException(status_code=404, detail="对象为空")

        def _iter():
            try:
                while True:
                    chunk = body.read(256 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                try:
                    body.close()
                except Exception:
                    pass

        headers.pop("Content-Length", None)   # 交给分块传输
        return StreamingResponse(_iter(), media_type=ctype, headers=headers)
    finally:
        await session.close()
        await engine.dispose()


# ---------------------------------------------------------------------------
# 配置中心（KV）
# ---------------------------------------------------------------------------
@app_config_router.get("")
async def list_app_config(scope: Optional[str] = None):
    """全量配置：{"items": {key: value}, "meta": {...}}。"""
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        stmt = select(AppConfig)
        if scope:
            stmt = stmt.where(AppConfig.scope == scope)
        rows = (await session.execute(stmt.order_by(AppConfig.key))).scalars().all()
        return {
            "items": {r.key: r.value for r in rows},
            "meta": {r.key: {"scope": r.scope, "version": r.version,
                            "description": r.description,
                            "updatedAt": r.updated_at.isoformat() if r.updated_at else None}
                     for r in rows},
        }
    finally:
        await session.close()
        await engine.dispose()


@app_config_router.get("/{cfg_key:path}")
async def get_app_config(cfg_key: str):
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        r = (await session.execute(
            select(AppConfig).where(AppConfig.key == cfg_key))).scalar_one_or_none()
        if r is None:
            raise HTTPException(status_code=404, detail=f"配置不存在：{cfg_key}")
        return {"key": r.key, "value": r.value, "scope": r.scope,
                "version": r.version, "description": r.description,
                "updatedAt": r.updated_at.isoformat() if r.updated_at else None}
    finally:
        await session.close()
        await engine.dispose()


@app_config_router.put("/{cfg_key:path}")
async def put_app_config(cfg_key: str, payload: Dict[str, Any] = Body(default={})):
    """新增/覆盖配置。body: {"value": any, "scope": "app", "description": "..."}"""
    if "value" not in payload:
        raise HTTPException(status_code=400, detail="value 必填")
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        r = (await session.execute(
            select(AppConfig).where(AppConfig.key == cfg_key))).scalar_one_or_none()
        created = r is None
        if created:
            r = AppConfig(key=cfg_key, value=payload["value"])
            session.add(r)
        else:
            r.value = payload["value"]
            r.version = (r.version or 1) + 1
        for field in ("scope", "description"):
            if field in payload:
                setattr(r, field, payload[field])
        await session.commit()
        return {"success": True, "created": created, "key": cfg_key,
                "version": r.version}
    finally:
        await session.close()
        await engine.dispose()


@app_config_router.delete("/{cfg_key:path}")
async def delete_app_config(cfg_key: str):
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        res = await session.execute(delete(AppConfig).where(AppConfig.key == cfg_key))
        await session.commit()
        if not res.rowcount:
            raise HTTPException(status_code=404, detail=f"配置不存在：{cfg_key}")
        return {"success": True, "deleted": cfg_key}
    finally:
        await session.close()
        await engine.dispose()
