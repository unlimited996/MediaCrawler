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
import logging
import os
import contextlib
import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from collections import OrderedDict, deque
from functools import lru_cache
from zoneinfo import ZoneInfo
from decimal import Decimal
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

from fastapi import (APIRouter, Body, Depends, File, HTTPException, Query,
                     Request, UploadFile)
import httpx
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import case, delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from database.models_recipe import (
    SCHEMA, AppConfig, DIFFICULTY_VALUES, RECIPE_STATUS_VALUES, MealLog, Recipe,
    RecipeCategory, RecipeFeedback, RecipeIngredient, RecipeNote, RecipeStep,
    RecipeTag, RecipeTagLink, ShoppingCheck, TablePlanRecord, TakeoutSpot,
    CollabMember, CollabRoom, CollabVote, FeedbackEntry, OcrUsage, UserProfile,
    DeviceCredential, GrowthEvent, MembershipState, MembershipGrant,
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
        # v2：菜品页/收藏榜/发现页需要
        "caloriesKcal": r.calories_kcal,
        "cuisine": r.cuisine,
        "favoriteCount": r.favorite_count or 0,
        "cookedCount": r.cooked_count or 0,
        "source": r.source,
        "publishedAt": r.published_at.isoformat() if r.published_at else None,
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
            "caloriesKcal": r.calories_kcal,
            "cuisine": r.cuisine,
            "favoriteCount": r.favorite_count or 0,
            "cookedCount": r.cooked_count or 0,
            "source": r.source,
            "publishedAt": r.published_at.isoformat() if r.published_at else None,
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
# v2：菜系 / 收藏榜 / 发现 / 反馈
# 注意：这些静态路径必须声明在 /{recipe_id:path} 之前，否则会被详情路由吞掉
# ---------------------------------------------------------------------------
@router.get("/cuisines")
async def list_cuisines():
    """菜系列表 + 数量，供菜品页顶部的筛选 chips。"""
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        rows = (await session.execute(
            select(Recipe.cuisine, func.count())
            .where(Recipe.status == "published")
            .group_by(Recipe.cuisine)
            .order_by(func.count().desc()))).all()
        return {
            "items": [{"name": name or "未分类", "count": n} for name, n in rows],
            "total": sum(n for _, n in rows),
        }
    finally:
        await session.close()
        await engine.dispose()


@router.get("/ranking/favorites")
async def favorite_ranking(limit: int = Query(20, ge=1, le=100)):
    """收藏榜：按真实收藏数排序。

    没有接入用户体系时收藏数会很小（甚至为 0），这里不做任何假数据填充；
    并列时用「做过次数 → 家常度」做次级排序，保证榜单顺序稳定可解释。
    """
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        rows = (await session.execute(
            select(Recipe)
            .where(Recipe.status == "published")
            .order_by(Recipe.favorite_count.desc(),
                      Recipe.cooked_count.desc(),
                      Recipe.sort_order, Recipe.id)
            .limit(limit))).scalars().all()
        cat_map = dict((await session.execute(
            select(RecipeCategory.id, RecipeCategory.name))).all())
        tag_map = await _tags_for(session, [r.id for r in rows])
        items = []
        for index, r in enumerate(rows, start=1):
            item = _recipe_brief(r, cat_map.get(r.category_id), tag_map.get(r.id, []))
            item["rank"] = index
            items.append(item)
        return {"items": items, "total": len(items)}
    finally:
        await session.close()
        await engine.dispose()


@router.get("/discover")
async def discover(
    sort: str = Query("latest", pattern="^(latest|hot)$"),
    limit: int = Query(20, ge=1, le=100),
):
    """发现页：``latest`` 按上新时间，``hot`` 按收藏数。

    「偷个菜」= 把别人贡献的菜复制进自己的餐桌，因此这里会带上 ``source``
    （菜谱来源，如「小厨」「村吕」），App 在卡片上标出来源。
    """
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        stmt = select(Recipe).where(Recipe.status == "published")
        if sort == "latest":
            stmt = stmt.order_by(Recipe.published_at.desc().nullslast(), Recipe.id)
        else:
            stmt = stmt.order_by(Recipe.favorite_count.desc(),
                                 Recipe.cooked_count.desc(), Recipe.id)
        rows = (await session.execute(stmt.limit(limit))).scalars().all()
        cat_map = dict((await session.execute(
            select(RecipeCategory.id, RecipeCategory.name))).all())
        tag_map = await _tags_for(session, [r.id for r in rows])
        return {
            "items": [_recipe_brief(r, cat_map.get(r.category_id), tag_map.get(r.id, []))
                      for r in rows],
            "sort": sort,
        }
    finally:
        await session.close()
        await engine.dispose()


@router.get("/feedback")
async def list_feedback(device_id: str = Query(..., alias="deviceId")):
    """某台设备的收藏 / 不想吃列表，App 启动时同步一次。"""
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        rows = (await session.execute(
            select(RecipeFeedback.recipe_id, RecipeFeedback.kind)
            .where(RecipeFeedback.device_id == device_id))).all()
        favorites = [rid for rid, kind in rows if kind == "favorite"]
        dislikes = [rid for rid, kind in rows if kind == "dislike"]
        return {"favorites": favorites, "dislikes": dislikes}
    finally:
        await session.close()
        await engine.dispose()


@router.post("/{recipe_id:path}/feedback")
async def set_feedback(recipe_id: str, payload: Dict[str, Any] = Body(default={})):
    """收藏 / 取消收藏 / 不想吃。

    body: ``{"deviceId": "...", "kind": "favorite|dislike", "action": "add|remove"}``
    ``favorite_count`` 每次都在同一事务里重算，避免并发下计数漂移。
    """
    device_id = (payload.get("deviceId") or "").strip()
    kind = (payload.get("kind") or "favorite").strip()
    action = (payload.get("action") or "add").strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="deviceId 必填")
    if kind not in ("favorite", "dislike"):
        raise HTTPException(status_code=400, detail="kind 只能是 favorite / dislike")
    if action not in ("add", "remove"):
        raise HTTPException(status_code=400, detail="action 只能是 add / remove")

    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        exists = (await session.execute(
            select(Recipe.id).where(Recipe.id == recipe_id))).scalar()
        if not exists:
            raise HTTPException(status_code=404, detail=f"菜谱不存在：{recipe_id}")

        if action == "add":
            await session.execute(
                text("""
                    INSERT INTO kitchenhelper.recipe_feedback (recipe_id, device_id, kind)
                    VALUES (:r, :d, :k) ON CONFLICT DO NOTHING
                """), {"r": recipe_id, "d": device_id, "k": kind})
        else:
            await session.execute(
                text("""
                    DELETE FROM kitchenhelper.recipe_feedback
                    WHERE recipe_id = :r AND device_id = :d AND kind = :k
                """), {"r": recipe_id, "d": device_id, "k": kind})

        # 同一事务内重算，读到的就是最新值
        count = (await session.execute(
            select(func.count()).select_from(RecipeFeedback)
            .where(RecipeFeedback.recipe_id == recipe_id,
                   RecipeFeedback.kind == "favorite"))).scalar() or 0
        await session.execute(
            text("UPDATE kitchenhelper.recipe SET favorite_count = :c WHERE id = :r"),
            {"c": count, "r": recipe_id})
        await session.commit()
        invalidate_bundle_cache()
        return {"success": True, "recipeId": recipe_id, "kind": kind,
                "action": action, "favoriteCount": int(count)}
    finally:
        await session.close()
        await engine.dispose()


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
async def upsert_recipe(request: Request, payload: Dict[str, Any] = Body(default={})):
    """新增或整体覆盖一个菜谱（需 X-Admin-Token，ENTITLEMENT 批次 4）。"""
    with _admin_guard(request):
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
async def update_recipe(recipe_id: str, request: Request, payload: Dict[str, Any] = Body(default={})):
    """部分更新（需 X-Admin-Token，ENTITLEMENT 批次 4）：只改请求里出现的字段。"""
    with _admin_guard(request):
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
async def delete_recipe(recipe_id: str, request: Request):
    """删除菜谱（需 X-Admin-Token，ENTITLEMENT 批次 4）。"""
    with _admin_guard(request):
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
async def import_all(request: Request, payload: Dict[str, Any] = Body(default={})):
    """整包导入（幂等，需 X-Admin-Token，ENTITLEMENT 批次 4）。body 为 catalog.json 结构。"""
    with _admin_guard(request):
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

        # 本地联调：DB 里存的是 NAS 内网地址（192.168.x.x），从办公网访问不到。
        # 设 S3_ENDPOINT_OVERRIDE=http://<公网主机>:3900 即可临时改走公网，
        # 不动库里的配置，也不影响线上进程。
        override = os.getenv("S3_ENDPOINT_OVERRIDE", "").strip()
        if override:
            from dataclasses import replace as _replace
            settings = _replace(settings, endpoint=override)

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
#: App 可读 scope 白名单（ENTITLEMENT 批次 4 / R7）：
#: 敏感 scope（未来 auth/admin/internal 等）永不下发；管理动作走 X-Admin-Token。
PUBLIC_CONFIG_SCOPES = ("app", "membership", "public")


@app_config_router.get("")
async def list_app_config(scope: Optional[str] = None):
    """App 配置列表。只下发白名单 scope；指定敏感 scope 直接 403。"""
    if scope is not None and scope not in PUBLIC_CONFIG_SCOPES:
        raise HTTPException(status_code=403, detail={
            "code": "CONFIG_SCOPE_LOCKED", "message": f"scope 不可读：{scope}"})
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        stmt = select(AppConfig)
        if scope:
            stmt = stmt.where(AppConfig.scope == scope)
        else:
            # 无 scope 参数 → 只下发白名单（catalog.meta 等存量配置 scope='app' 不受影响）
            stmt = stmt.where(AppConfig.scope.in_(PUBLIC_CONFIG_SCOPES))
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
        if r is None or r.scope not in PUBLIC_CONFIG_SCOPES:
            # 敏感 scope 单 key 也读不到（404，不泄露存在性）
            raise HTTPException(status_code=404, detail=f"配置不存在：{cfg_key}")
        return {"key": r.key, "value": r.value, "scope": r.scope,
                "version": r.version, "description": r.description,
                "updatedAt": r.updated_at.isoformat() if r.updated_at else None}
    finally:
        await session.close()
        await engine.dispose()


# 管理动作封口（ENTITLEMENT 批次 4 / R7）：X-Admin-Token。
# token 从环境变量 ADMIN_TOKEN 读；未配置时拒绝全部管理调用（fail-closed）。


@lru_cache(maxsize=1)
def _admin_token() -> str:
    return os.environ.get("ADMIN_TOKEN", "").strip()


@contextlib.contextmanager
def _admin_guard(request: Request):
    """管理写接口统一守卫：校验 X-Admin-Token；未配置则 fail-closed。"""
    expected = _admin_token()
    provided = (request.headers.get("X-Admin-Token") or "").strip()
    if not expected or not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=403, detail={"code": "ADMIN_LOCKED",
                                                     "message": "需要有效的 X-Admin-Token"})
    yield


@app_config_router.put("/{cfg_key:path}")
async def put_app_config(cfg_key: str, request: Request, payload: Dict[str, Any] = Body(default={})):
    """新增/覆盖配置（ENTITLEMENT 批次 4：需 X-Admin-Token）。body: {"value": any, ...}"""
    with _admin_guard(request):
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
async def delete_app_config(cfg_key: str, request: Request):
    # ENTITLEMENT 批次 4：管理动作需 X-Admin-Token
    with _admin_guard(request):
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


# ===========================================================================
# v2：生活流（记食 / 我的餐桌）
# ===========================================================================

# ---------------------------------------------------------------------------
# 存量端点灰度鉴权（ENTITLEMENT 批次 4 剩余 / R2：伪造 deviceId 冒名读写）
#
# 灰度语义（§7 兼容与灰度）：
# - 无签名头（老包）→ 放行并打日志（灰度观察老包占比）；
# - 带签名头 → 验签（复用批次 2 verify_device：时间窗/nonce/HMAC），
#   失败 401；验签通过后做**身份绑定**——签名身份必须与请求声称的
#   deviceId（query 或 JSON body）一致，不一致 401 FORGED_IDENTITY，
#   拿自己凭证冒名他人数据的通道就此关闭；
# - multipart（OCR 上传）暂跳过验签：依赖层读 body 后 multipart 解析的
#   二次消费兼容性需真机实测再开，OCR 次数由批次 1 的服务端配额兜底。
# - 强制开关（app_config: auth.enforce）留待灰度观察 1~2 个版本后启用。
# ---------------------------------------------------------------------------
_auth_log = logging.getLogger("recipe.gray")


async def gradual_auth(request: Request) -> None:
    """Router 级灰度鉴权依赖：带签名则验+绑定身份，不带放行（老包兼容）。"""
    if "x-signature" not in request.headers:
        _auth_log.info("gray-auth pass-through %s %s", request.method, request.url.path)
        return
    ctype = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in ctype:
        _auth_log.info("gray-auth skip-multipart %s %s", request.method, request.url.path)
        return
    verified = await verify_device(request)  # 验签失败/重放/超窗在此抛 401
    # 身份绑定：比对签名身份与请求声称的 deviceId
    claimed = request.query_params.get("deviceId")
    if not claimed:
        body_bytes = await request.body()  # verify_device 已缓存 body，此处命中缓存
        if "application/json" in ctype and body_bytes:
            try:
                claimed = (json.loads(body_bytes) or {}).get("deviceId")
            except Exception:
                claimed = None
    if claimed and claimed != verified:
        _auth_log.warning("gray-auth FORGED_IDENTITY sig=%s claimed=%s %s",
                          verified, claimed, request.url.path)
        raise HTTPException(status_code=401, detail={
            "code": "FORGED_IDENTITY",
            "message": "签名身份与 deviceId 不一致"})


life_router = APIRouter(prefix="", tags=["life"],
                        dependencies=[Depends(gradual_auth)])


def _meal_log_json(row: MealLog) -> Dict[str, Any]:
    return {
        "id": row.id,
        "deviceId": row.device_id,
        "eatenOn": row.eaten_on.isoformat() if row.eaten_on else None,
        "meal": row.meal,
        "note": row.note,
        "imageKey": row.image_key,
        "imageUrl": _media_url(row.image_key),
        "recipeIds": row.recipe_ids or [],
        "createdAt": row.created_at.isoformat() if row.created_at else None,
    }


@life_router.get("/meal-log")
async def list_meal_log(
    device_id: str = Query(..., alias="deviceId"),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}$"),
    day: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """记食列表。

    - 传 ``day``：返回那一天的记录（首页「今天的饭桌」用）
    - 传 ``month``：返回整月 + 按天聚合的计数（日历上打点用）
    """
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        stmt = select(MealLog).where(MealLog.device_id == device_id)
        if day:
            stmt = stmt.where(MealLog.eaten_on == date.fromisoformat(day))
        elif month:
            start = date.fromisoformat(f"{month}-01")
            end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
            stmt = stmt.where(MealLog.eaten_on >= start, MealLog.eaten_on < end)
        rows = (await session.execute(
            stmt.order_by(MealLog.eaten_on.desc(), MealLog.id.desc()))).scalars().all()

        by_day: Dict[str, int] = {}
        for r in rows:
            key = r.eaten_on.isoformat()
            by_day[key] = by_day.get(key, 0) + 1

        return {
            "items": [_meal_log_json(r) for r in rows],
            "count": len(rows),
            "byDay": by_day,
            "mealSlots": 3,  # 早/午/晚，截图里的「已记 0 / 3 条」
        }
    finally:
        await session.close()
        await engine.dispose()


@life_router.post("/meal-log")
async def create_meal_log(payload: Dict[str, Any] = Body(default={})):
    """记一顿：照片/一句话 + 可选关联菜谱。

    关联了菜谱时，把这些菜的 ``cooked_count`` +1 ——「做过多少次」是真实行为数据，
    收藏榜与发现的「最热」都靠它。

    ENTITLEMENT 批次 3：同时做服务端发分——
    - clientLogId（App 生成的 UUID 幂等键）防重放刷分（R5）；
    - meal_logged：+10 成长值 / +2 柴豆（同 clientLogId 只发一次）；
    - recipeIds 中每道菜首次记 recipe_cooked：+5 / +1（同菜终身只记一次，R4）；
    - clientLogId 缺失时降级：以 (deviceId, eatenOn, meal, note, 菜单) 现有去重手段
      无法可靠区分重放，为安全起见**不发分**（灰度期可容忍，老包不丢功能只丢分）。
    """
    device_id = (payload.get("deviceId") or "").strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="deviceId 必填")
    eaten_on = payload.get("eatenOn") or date.today().isoformat()
    meal = (payload.get("meal") or "dinner").strip()
    if meal not in ("breakfast", "lunch", "dinner", "snack"):
        raise HTTPException(status_code=400, detail="meal 只能是 breakfast/lunch/dinner/snack")
    note = payload.get("note")
    if not note and not payload.get("imageKey") and not payload.get("recipeIds"):
        raise HTTPException(status_code=400, detail="至少要写一句、传一张图或选一道菜")
    client_log_id = (payload.get("clientLogId") or "").strip()[:120]
    recipe_ids = list(payload.get("recipeIds") or [])

    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        row = MealLog(
            device_id=device_id,
            eaten_on=date.fromisoformat(str(eaten_on)),
            meal=meal,
            note=note,
            image_key=payload.get("imageKey"),
            recipe_ids=recipe_ids,
        )
        session.add(row)
        await session.flush()
        for recipe_id in row.recipe_ids:
            await session.execute(
                text(f'UPDATE "{SCHEMA}".recipe SET cooked_count = cooked_count + 1 '
                     f'WHERE id = :r'), {"r": recipe_id})

        # ---- 服务端发分（ENTITLEMENT 批次 3） ----
        awarded = {"points": 0, "coins": 0}
        if client_log_id:
            if await _growth_award(session, device_id, "meal_logged",
                                    client_log_id, POINTS_PER_MEAL_LOGGED, COINS_PER_MEAL_LOGGED):
                awarded["points"] += POINTS_PER_MEAL_LOGGED
                awarded["coins"] += COINS_PER_MEAL_LOGGED
            for recipe_id in recipe_ids:
                if await _growth_award(session, device_id, "recipe_cooked",
                                        str(recipe_id), POINTS_PER_NEW_DISH, COINS_PER_NEW_DISH):
                    awarded["points"] += POINTS_PER_NEW_DISH
                    awarded["coins"] += COINS_PER_NEW_DISH

        await session.commit()
        await session.refresh(row)
        invalidate_bundle_cache()
        result = _meal_log_json(row)
        result["awarded"] = awarded
        return result
    finally:
        await session.close()
        await engine.dispose()


@life_router.delete("/meal-log/{log_id}")
async def delete_meal_log(log_id: int, device_id: str = Query(..., alias="deviceId")):
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        res = await session.execute(
            delete(MealLog).where(MealLog.id == log_id, MealLog.device_id == device_id))
        await session.commit()
        if not res.rowcount:
            raise HTTPException(status_code=404, detail="记录不存在")
        return {"success": True, "deleted": log_id}
    finally:
        await session.close()
        await engine.dispose()


@life_router.patch("/meal-log/{log_id}")
async def update_meal_log(log_id: int, payload: Dict[str, Any] = Body(default={})):
    """改一顿（餐次 / 日期 / 心情 / 这桌菜）。

    App 的编辑页（图5）会走这里；只传变了的字段即可。
    菜谱列表变化时**不重复累加** cooked_count —— 计数只认「新建」那一刻，
    编辑只修正内容，避免改个错别字把「做过几次」刷上去。
    """
    device_id = (payload.get("deviceId") or "").strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="deviceId 必填")

    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        row = (await session.execute(
            select(MealLog).where(MealLog.id == log_id,
                                  MealLog.device_id == device_id))).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")

        if "meal" in payload:
            meal = (payload.get("meal") or "").strip()
            if meal not in ("breakfast", "lunch", "dinner", "snack"):
                raise HTTPException(status_code=400, detail="meal 只能是 breakfast/lunch/dinner/snack")
            row.meal = meal
        if "eatenOn" in payload:
            row.eaten_on = date.fromisoformat(str(payload["eatenOn"]))
        if "note" in payload:
            row.note = payload.get("note")
        if "imageKey" in payload:
            row.image_key = payload.get("imageKey")
        if "recipeIds" in payload:
            row.recipe_ids = list(payload.get("recipeIds") or [])

        await session.commit()
        await session.refresh(row)
        invalidate_bundle_cache()
        return _meal_log_json(row)
    finally:
        await session.close()
        await engine.dispose()


#: 「我的餐桌」上限，与截图上「最多 20 桌」一致
MAX_TABLE_PLANS = 20


def _table_plan_json(row: TablePlanRecord) -> Dict[str, Any]:
    return {
        "id": row.id,
        "title": row.title,
        "people": row.people,
        "taste": row.taste,
        "dishes": row.recipe_ids or [],
        "isFavorite": bool(row.is_favorite),
        "createdAt": row.created_at.isoformat() if row.created_at else None,
    }


@life_router.get("/table-plans")
async def list_table_plans(
    device_id: str = Query(..., alias="deviceId"),
    favorite: Optional[bool] = None,
):
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        stmt = select(TablePlanRecord).where(TablePlanRecord.device_id == device_id)
        if favorite is not None:
            stmt = stmt.where(TablePlanRecord.is_favorite == favorite)
        rows = (await session.execute(
            stmt.order_by(TablePlanRecord.is_favorite.desc(),
                          TablePlanRecord.created_at.desc()))).scalars().all()
        return {"items": [_table_plan_json(r) for r in rows],
                "limit": MAX_TABLE_PLANS, "count": len(rows)}
    finally:
        await session.close()
        await engine.dispose()


@life_router.post("/table-plans")
async def create_table_plan(payload: Dict[str, Any] = Body(default={})):
    """保存一桌（「帮我整一桌」之后点保存）。超过 20 桌返回 409，让用户先删。"""
    device_id = (payload.get("deviceId") or "").strip()
    dishes = payload.get("dishes") or []
    if not device_id:
        raise HTTPException(status_code=400, detail="deviceId 必填")
    if not dishes:
        raise HTTPException(status_code=400, detail="dishes 不能为空")

    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        used = (await session.execute(
            select(func.count()).select_from(TablePlanRecord)
            .where(TablePlanRecord.device_id == device_id))).scalar() or 0
        if used >= MAX_TABLE_PLANS:
            raise HTTPException(
                status_code=409,
                detail=f"最多保存 {MAX_TABLE_PLANS} 桌，请先删掉一些")
        row = TablePlanRecord(
            device_id=device_id,
            title=payload.get("title"),
            people=int(payload.get("people") or 2),
            taste=(payload.get("taste") or "SAVORY"),
            recipe_ids=dishes,
            is_favorite=bool(payload.get("isFavorite")),
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return _table_plan_json(row)
    finally:
        await session.close()
        await engine.dispose()


@life_router.delete("/table-plans/{plan_id}")
async def delete_table_plan(plan_id: int, device_id: str = Query(..., alias="deviceId")):
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        res = await session.execute(
            delete(TablePlanRecord)
            .where(TablePlanRecord.id == plan_id, TablePlanRecord.device_id == device_id))
        await session.commit()
        if not res.rowcount:
            raise HTTPException(status_code=404, detail="餐桌不存在")
        return {"success": True, "deleted": plan_id}
    finally:
        await session.close()
        await engine.dispose()


# ===========================================================================
# v3：做饭页备注 / 采购勾选 / 我的常点
# ===========================================================================
@life_router.get("/recipe-notes/{recipe_id:path}")
async def get_recipe_note(recipe_id: str, device_id: str = Query(..., alias="deviceId")):
    """取这道菜在这个设备上的备注；没有过就返回 ``note: null``。"""
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        row = (await session.execute(
            select(RecipeNote).where(RecipeNote.recipe_id == recipe_id,
                                     RecipeNote.device_id == device_id))).scalar_one_or_none()
        return {
            "recipeId": recipe_id,
            "note": row.content if row else None,
            "updatedAt": row.updated_at.isoformat() if row and row.updated_at else None,
        }
    finally:
        await session.close()
        await engine.dispose()


@life_router.put("/recipe-notes/{recipe_id:path}")
async def put_recipe_note(recipe_id: str, payload: Dict[str, Any] = Body(default={})):
    """保存/覆盖备注（一个人对一道菜只有一条，再存即覆盖）。"""
    device_id = (payload.get("deviceId") or "").strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="deviceId 必填")
    content = (payload.get("content") or "").strip()
    if len(content) > 2000:
        raise HTTPException(status_code=400, detail="备注最多 2000 字")

    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        row = (await session.execute(
            select(RecipeNote).where(RecipeNote.recipe_id == recipe_id,
                                     RecipeNote.device_id == device_id))).scalar_one_or_none()
        if row is None:
            row = RecipeNote(recipe_id=recipe_id, device_id=device_id, content=content)
            session.add(row)
        else:
            row.content = content
        await session.commit()
        await session.refresh(row)
        return {"recipeId": recipe_id, "note": row.content,
                "updatedAt": row.updated_at.isoformat() if row.updated_at else None}
    finally:
        await session.close()
        await engine.dispose()


# ---------------------------------------------------------------------------
# plan_key 服务端校验（ENTITLEMENT 批次 4 / R8 尾巴）：客户端自算的
# planKey 形如 "2026-11-14-1a2b3c4d"（日期-菜集hash），无法绑设备签名前
# 先挡脏数据：长度与字符集校验，防 storage 膨胀/注入式垃圾。
# ---------------------------------------------------------------------------
_PLAN_KEY_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")


def _validate_plan_key(plan_key: str) -> None:
    if not plan_key or not _PLAN_KEY_RE.match(plan_key):
        raise HTTPException(status_code=400, detail={
            "code": "INVALID_PLAN_KEY",
            "message": "planKey 格式非法（1~64 位字母数字与 . : _ -）"})


@life_router.get("/shopping-checks")
async def list_shopping_checks(
    device_id: str = Query(..., alias="deviceId"),
    plan_key: str = Query(..., alias="planKey"),
):
    """这一桌已经买齐的食材名。客户端拿它把勾选状态恢复出来。"""
    _validate_plan_key(plan_key)
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        rows = (await session.execute(
            select(ShoppingCheck.item_name).where(
                ShoppingCheck.device_id == device_id,
                ShoppingCheck.plan_key == plan_key,
                ShoppingCheck.checked.is_(True)))).scalars().all()
        return {"planKey": plan_key, "items": list(rows), "count": len(rows)}
    finally:
        await session.close()
        await engine.dispose()


@life_router.put("/shopping-checks")
async def put_shopping_check(payload: Dict[str, Any] = Body(default={})):
    """勾/取消勾一个食材。同一 (device, plan, item) 只留一行。"""
    device_id = (payload.get("deviceId") or "").strip()
    plan_key = (payload.get("planKey") or "").strip()
    item = (payload.get("itemName") or "").strip()
    if not device_id or not plan_key or not item:
        raise HTTPException(status_code=400, detail="deviceId / planKey / itemName 必填")
    _validate_plan_key(plan_key)
    checked = bool(payload.get("checked", True))

    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        row = (await session.execute(
            select(ShoppingCheck).where(
                ShoppingCheck.device_id == device_id,
                ShoppingCheck.plan_key == plan_key,
                ShoppingCheck.item_name == item))).scalar_one_or_none()
        if row is None:
            if not checked:
                return {"itemName": item, "checked": False}
            row = ShoppingCheck(device_id=device_id, plan_key=plan_key,
                                item_name=item, checked=True)
            session.add(row)
        else:
            row.checked = checked
        await session.commit()
        return {"itemName": item, "checked": checked}
    finally:
        await session.close()
        await engine.dispose()


@life_router.get("/takeout-spots")
async def list_takeout_spots(device_id: str = Query(..., alias="deviceId")):
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        rows = (await session.execute(
            select(TakeoutSpot).where(TakeoutSpot.device_id == device_id)
            .order_by(TakeoutSpot.created_at.desc()))).scalars().all()
        return {"items": [{"id": r.id, "name": r.name, "category": r.category,
                           "note": r.note} for r in rows], "count": len(rows)}
    finally:
        await session.close()
        await engine.dispose()


@life_router.post("/takeout-spots")
async def create_takeout_spot(payload: Dict[str, Any] = Body(default={})):
    device_id = (payload.get("deviceId") or "").strip()
    name = (payload.get("name") or "").strip()
    if not device_id or not name:
        raise HTTPException(status_code=400, detail="deviceId / name 必填")

    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        exists = (await session.execute(
            select(TakeoutSpot.id).where(TakeoutSpot.device_id == device_id,
                                        TakeoutSpot.name == name))).scalar_one_or_none()
        if exists:
            raise HTTPException(status_code=409, detail="这个常点已经有了")
        row = TakeoutSpot(device_id=device_id, name=name,
                          category=(payload.get("category") or "全部"),
                          note=payload.get("note"))
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return {"id": row.id, "name": row.name, "category": row.category, "note": row.note}
    finally:
        await session.close()
        await engine.dispose()


@life_router.delete("/takeout-spots/{spot_id}")
async def delete_takeout_spot(spot_id: int, device_id: str = Query(..., alias="deviceId")):
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        res = await session.execute(
            delete(TakeoutSpot).where(TakeoutSpot.id == spot_id,
                                      TakeoutSpot.device_id == device_id))
        await session.commit()
        if not res.rowcount:
            raise HTTPException(status_code=404, detail="常点不存在")
        return {"success": True, "deleted": spot_id}
    finally:
        await session.close()
        await engine.dispose()


# ===========================================================================
# 协作配餐（v4）：用户档案 + 房间（创建/加入/投票/定稿）
#
# 身份沿用 device_id（无密码轻量账号）；档案只补昵称+头像，
# 协作房间靠 6 位邀请码拉人，每人对每道菜一个表态（加/去），房主定稿。
# ===========================================================================

#: 邀请码字符集：去掉 0/O/1/I 这些肉眼易混字符
_ROOM_CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _gen_room_code() -> str:
    return "".join(secrets.choice(_ROOM_CODE_CHARS) for _ in range(6))


# ---------------------------------------------------------------------------
# collab 防爆破（ENTITLEMENT 批次 4 / R8）：邀请码空间 32^6≈10.7 亿靠频控兜底。
# 内存实现（单实例服务，进程重启失效可接受）：
# - join 失败（房间不存在）按 device|IP 滑动窗计数，10 次/分钟 → 封 10 分钟；
# - 建房每设备每日 ≤20。
# ---------------------------------------------------------------------------
JOIN_FAIL_LIMIT = 10
JOIN_WINDOW_SECONDS = 60.0
JOIN_BAN_SECONDS = 600.0
ROOM_DAILY_CREATE_LIMIT = 20

_JOIN_FAILS: "OrderedDict[str, Deque[float]]" = OrderedDict()
_JOIN_BANNED: Dict[str, float] = {}
_ROOM_CREATES: Dict[str, Tuple[str, int]] = {}


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for") or ""
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _join_gate(key: str, now: float) -> float:
    """封禁中返回剩余秒数，否则 0。顺手清理过期封禁。"""
    for k in [k for k, until in _JOIN_BANNED.items() if until <= now]:
        _JOIN_BANNED.pop(k, None)
    until = _JOIN_BANNED.get(key, 0.0)
    return (until - now) if until > now else 0.0


def _join_fail_note(key: str, now: float) -> None:
    """记录一次 join 失败；窗口内超限 → 封禁。"""
    wins = _JOIN_FAILS.setdefault(key, deque())
    wins.append(now)
    while wins and wins[0] <= now - JOIN_WINDOW_SECONDS:
        wins.popleft()
    if len(wins) >= JOIN_FAIL_LIMIT:
        _JOIN_BANNED[key] = now + JOIN_BAN_SECONDS
        _JOIN_FAILS.pop(key, None)


def _join_fail_clear(key: str) -> None:
    """成功加入：清失败窗并解封（成功即证明非爆破）。"""
    _JOIN_FAILS.pop(key, None)
    _JOIN_BANNED.pop(key, None)


def _room_create_allow(device_id: str, today: str) -> bool:
    prev = _ROOM_CREATES.get(device_id)
    return not (prev and prev[0] == today and prev[1] >= ROOM_DAILY_CREATE_LIMIT)


def _room_create_note(device_id: str, today: str) -> None:
    prev = _ROOM_CREATES.get(device_id)
    if prev and prev[0] == today:
        _ROOM_CREATES[device_id] = (today, prev[1] + 1)
    else:
        # 顺手清理跨日旧项（防 dict 无界膨胀）
        for k in [k for k, v in _ROOM_CREATES.items() if v[0] != today]:
            _ROOM_CREATES.pop(k, None)
        _ROOM_CREATES[device_id] = (today, 1)


def _profile_json(row: UserProfile) -> Dict[str, Any]:
    return {"deviceId": row.device_id, "nickname": row.nickname, "avatar": row.avatar}


@life_router.get("/profile")
async def get_profile(device_id: str = Query(..., alias="deviceId")):
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        row = (await session.execute(
            select(UserProfile).where(UserProfile.device_id == device_id))).scalar_one_or_none()
        if row is None:
            return {"deviceId": device_id, "nickname": "", "avatar": ""}
        return _profile_json(row)
    finally:
        await session.close()
        await engine.dispose()


@life_router.post("/profile")
async def save_profile(payload: Dict[str, Any] = Body(default={})): 
    device_id = (payload.get("deviceId") or "").strip()
    nickname = (payload.get("nickname") or "").strip()[:24]
    avatar = (payload.get("avatar") or "🙂")[:8]
    if not device_id:
        raise HTTPException(status_code=400, detail="deviceId 必填")
    if not nickname:
        raise HTTPException(status_code=400, detail="nickname 必填")

    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        row = (await session.execute(
            select(UserProfile).where(UserProfile.device_id == device_id))).scalar_one_or_none()
        if row is None:
            row = UserProfile(device_id=device_id, nickname=nickname, avatar=avatar)
            session.add(row)
        else:
            row.nickname = nickname
            row.avatar = avatar
        await session.commit()
        await session.refresh(row)
        return _profile_json(row)
    finally:
        await session.close()
        await engine.dispose()


async def _load_room(session: AsyncSession, code: str) -> CollabRoom:
    room = (await session.execute(
        select(CollabRoom).where(CollabRoom.code == code.upper()))).scalar_one_or_none()
    if room is None:
        raise HTTPException(status_code=404, detail="房间不存在，检查一下邀请码")
    return room


def _room_json(room: CollabRoom, members: List[CollabMember],
               votes: List[CollabVote], my_device_id: str) -> Dict[str, Any]:
    tally: Dict[str, Dict[str, Any]] = {
        rid: {"recipeId": rid, "add": 0, "remove": 0, "mine": None}
        for rid in (room.recipe_ids or [])
    }
    for v in votes:
        slot = tally.get(v.recipe_id)
        if slot is None:
            continue
        slot["add" if v.stance == "add" else "remove"] += 1
        if v.device_id == my_device_id:
            slot["mine"] = v.stance
    return {
        "code": room.code,
        "title": room.title,
        "status": room.status,
        "people": room.people,
        "isHost": room.host_device_id == my_device_id,
        "members": [{
            "deviceId": m.device_id,
            "nickname": m.nickname,
            "avatar": m.avatar,
            "isHost": m.device_id == room.host_device_id,
        } for m in members],
        "dishes": list(tally.values()),
    }


async def _room_detail(session: AsyncSession, room: CollabRoom, my_device_id: str):
    members = list((await session.execute(
        select(CollabMember).where(CollabMember.room_id == room.id)
        .order_by(CollabMember.joined_at))).scalars())
    votes = list((await session.execute(
        select(CollabVote).where(CollabVote.room_id == room.id))).scalars())
    return _room_json(room, members, votes, my_device_id)


@life_router.post("/collab/rooms")
async def create_collab_room(payload: Dict[str, Any] = Body(default={}),
                            request: Request = None):
    device_id = (payload.get("deviceId") or "").strip()
    dish_ids = payload.get("dishIds") or []
    title = (payload.get("title") or "这一桌").strip()[:48]
    people = int(payload.get("people") or 2)
    if not device_id:
        raise HTTPException(status_code=400, detail="deviceId 必填")
    if not dish_ids:
        raise HTTPException(status_code=400, detail="先配好一桌菜再邀请")

    # 防爆破（批次 4 / R8）：每设备每日建房上限
    today = datetime.now(tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    if not _room_create_allow(device_id, today):
        raise HTTPException(status_code=429, detail={
            "code": "ROOM_CREATE_LIMIT",
            "message": f"今天建得够多了（上限 {ROOM_DAILY_CREATE_LIMIT} 个），明天再开桌"})

    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        profile = (await session.execute(
            select(UserProfile).where(UserProfile.device_id == device_id))).scalar_one_or_none()
        nickname = (profile.nickname if profile else "") or f"房主{device_id[-4:]}"
        avatar = (profile.avatar if profile else "") or "👨‍🍳"

        # 邀请码去重：撞了就重摇，表里码基数很小，重摇即可
        for _ in range(8):
            code = _gen_room_code()
            clash = (await session.execute(
                select(CollabRoom.id).where(CollabRoom.code == code))).scalar_one_or_none()
            if clash is None:
                break
        room = CollabRoom(code=code, host_device_id=device_id, title=title,
                          recipe_ids=list(dict.fromkeys(dish_ids)), people=people)
        session.add(room)
        await session.flush()
        session.add(CollabMember(room_id=room.id, device_id=device_id,
                                 nickname=nickname, avatar=avatar))
        await session.commit()
        _room_create_note(device_id, today)  # 建房成功计数（批次 4 / R8）
        await session.refresh(room)
        return await _room_detail(session, room, device_id)
    finally:
        await session.close()
        await engine.dispose()


@life_router.get("/collab/rooms/{code}")
async def get_collab_room(code: str, device_id: str = Query(..., alias="deviceId")):
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        room = await _load_room(session, code)
        return await _room_detail(session, room, device_id)
    finally:
        await session.close()
        await engine.dispose()


@life_router.post("/collab/rooms/{code}/join")
async def join_collab_room(code: str, payload: Dict[str, Any] = Body(default={}),
                           request: Request = None):
    device_id = (payload.get("deviceId") or "").strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="deviceId 必填")

    # 防爆破（批次 4 / R8）：失败滑动窗 + 临时封禁，key = device|IP
    gate_key = f"{device_id}|{_client_ip(request)}"
    now = time.time()
    left = _join_gate(gate_key, now)
    if left:
        raise HTTPException(status_code=429, detail={
            "code": "RATE_LIMITED",
            "message": f"尝试太频繁，{int(left) + 1} 秒后再试"})

    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        try:
            room = await _load_room(session, code)
        except HTTPException as e:
            if e.status_code == 404:
                _join_fail_note(gate_key, time.time())  # 房间不存在 = 一次爆破尝试
            raise
        if room.status != "open":
            raise HTTPException(status_code=409, detail="这一桌已经定稿了")
        exists = (await session.execute(
            select(CollabMember.id).where(CollabMember.room_id == room.id,
                                          CollabMember.device_id == device_id))).scalar_one_or_none()
        if exists is None:
            profile = (await session.execute(
                select(UserProfile).where(UserProfile.device_id == device_id))).scalar_one_or_none()
            nickname = (payload.get("nickname")
                        or (profile.nickname if profile else "")
                        or f"饭友{device_id[-4:]}")[:24]
            avatar = (payload.get("avatar")
                      or (profile.avatar if profile else "") or "🙂")[:8]
            session.add(CollabMember(room_id=room.id, device_id=device_id,
                                     nickname=nickname, avatar=avatar))
            await session.commit()
        _join_fail_clear(gate_key)  # 加入成功清失败窗（批次 4 / R8）
        return await _room_detail(session, room, device_id)
    finally:
        await session.close()
        await engine.dispose()


@life_router.post("/collab/rooms/{code}/vote")
async def vote_collab_dish(code: str, payload: Dict[str, Any] = Body(default={})):
    device_id = (payload.get("deviceId") or "").strip()
    recipe_id = (payload.get("recipeId") or "").strip()
    stance = (payload.get("stance") or "").strip().lower()
    if not device_id or not recipe_id:
        raise HTTPException(status_code=400, detail="deviceId / recipeId 必填")
    if stance not in ("add", "remove", "none"):
        raise HTTPException(status_code=400, detail="stance 须是 add/remove/none")

    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        room = await _load_room(session, code)
        if room.status != "open":
            raise HTTPException(status_code=409, detail="这一桌已经定稿了")
        member = (await session.execute(
            select(CollabMember).where(CollabMember.room_id == room.id,
                                       CollabMember.device_id == device_id))).scalar_one_or_none()
        if member is None:
            raise HTTPException(status_code=403, detail="先加入这一桌才能投票")
        if recipe_id not in (room.recipe_ids or []):
            raise HTTPException(status_code=404, detail="桌上没有这道菜")

        vote = (await session.execute(
            select(CollabVote).where(CollabVote.room_id == room.id,
                                     CollabVote.device_id == device_id,
                                     CollabVote.recipe_id == recipe_id))).scalar_one_or_none()
        if stance == "none":
            if vote is not None:
                await session.delete(vote)
        elif vote is None:
            session.add(CollabVote(room_id=room.id, device_id=device_id,
                                   recipe_id=recipe_id, stance=stance))
        elif vote.stance != stance:
            vote.stance = stance
        await session.commit()
        return await _room_detail(session, room, device_id)
    finally:
        await session.close()
        await engine.dispose()


@life_router.post("/collab/rooms/{code}/decide")
async def decide_collab_room(code: str, payload: Dict[str, Any] = Body(default={})):
    device_id = (payload.get("deviceId") or "").strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="deviceId 必填")

    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        room = await _load_room(session, code)
        if room.host_device_id != device_id:
            raise HTTPException(status_code=403, detail="只有房主能定稿")
        if room.status != "open":
            raise HTTPException(status_code=409, detail="这一桌已经定稿了")
        room.status = "decided"
        await session.commit()
        return await _room_detail(session, room, device_id)
    finally:
        await session.close()
        await engine.dispose()


@life_router.delete("/collab/rooms/{code}/leave")
async def leave_collab_room(code: str, device_id: str = Query(..., alias="deviceId")):
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        room = await _load_room(session, code)
        if room.host_device_id == device_id:
            # 房主退出 = 解散房间（成员与选票级联清掉）
            room.status = "closed"
            await session.execute(
                delete(CollabMember).where(CollabMember.room_id == room.id))
            await session.execute(
                delete(CollabVote).where(CollabVote.room_id == room.id))
            await session.commit()
            return {"success": True, "closed": True}
        res = await session.execute(
            delete(CollabMember).where(CollabMember.room_id == room.id,
                                       CollabMember.device_id == device_id))
        await session.execute(
            delete(CollabVote).where(CollabVote.room_id == room.id,
                                     CollabVote.device_id == device_id))
        await session.commit()
        if not res.rowcount:
            raise HTTPException(status_code=404, detail="你不在这一桌里")
        return {"success": True, "closed": False}
    finally:
        await session.close()
        await engine.dispose()


# ===========================================================================
# 反馈（v4.1）：提交 / 我的反馈
# ===========================================================================

def _feedback_json(row: FeedbackEntry) -> Dict[str, Any]:
    return {
        "id": row.id,
        "content": row.content,
        "status": row.status,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
    }


@life_router.post("/feedback")
async def submit_feedback(payload: Dict[str, Any] = Body(default={})):
    device_id = (payload.get("deviceId") or "").strip()
    content = (payload.get("content") or "").strip()
    if not device_id or not content:
        raise HTTPException(status_code=400, detail="deviceId / content 必填")
    if len(content) > 500:
        raise HTTPException(status_code=400, detail="反馈最多 500 字")

    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        row = FeedbackEntry(device_id=device_id, content=content)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return _feedback_json(row)
    finally:
        await session.close()
        await engine.dispose()


@life_router.get("/feedback")
async def list_feedback(device_id: str = Query(..., alias="deviceId")):
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        rows = list((await session.execute(
            select(FeedbackEntry).where(FeedbackEntry.device_id == device_id)
            .order_by(FeedbackEntry.created_at.desc()).limit(50))).scalars())
        return {"items": [_feedback_json(r) for r in rows]}
    finally:
        await session.close()
        await engine.dispose()


# ===========================================================================
# 拍小票 OCR 代理（ENTITLEMENT 批次 1：key 收回服务端，配额服务端记账）
#
# - App 不再直连 uapis.cn：key 从环境变量 OCR_API_KEY 读，永不进 APK；
# - 配额按 deviceId 当日计数（ocr_usage 表，东八区自然日）；
#   批次 3 之前暂按「未注册用户 = Lv1（3 次/日）」；
# - 供应商调用成功才 +1（失败不扣次数）；超限返回 429 OCR_QUOTA_EXCEEDED；
# - 图片 ≤8MB、仅 jpeg/png，转发后即弃（不落盘原图）。
# ===========================================================================

#: 每日 OCR 次数上限（批次 1 固定 Lv1；批次 3 后改由 membership/entitlements 下发）
OCR_DAILY_LIMIT = 3
#: 允许的图片 MIME 与大小
OCR_ALLOWED_TYPES = {"image/jpeg", "image/png"}
OCR_MAX_BYTES = 8 * 1024 * 1024
#: 供应商转发超时：连接 5s / 读取 20s
OCR_TIMEOUT = httpx.Timeout(20.0, connect=5.0)


def _ocr_today() -> date:
    """东八区自然日（与 App 本地按天清零语义一致）。

    Windows 下无系统 tzdata 时回退固定 +8 偏移（中国无夏令时，固定偏移安全）。
    """
    try:
        tz = ZoneInfo("Asia/Shanghai")
    except Exception:
        tz = timezone(timedelta(hours=8))
    return datetime.now(tz=tz).date()


def _parse_receipt_line(raw: str) -> Optional[Dict[str, Any]]:
    """小票一行 → {name, detail}；垃圾行（价格行/地址/空行）返回 None。

    与 App 端 OcrApi.parseLine 同规则：量词宽松匹配（0-3 个汉字/字母），
    支持空格 / 紧贴 / 乘号 x×* 三种写法。
    """
    s = raw.strip()
    if not s:
        return None
    # 纯数字/价格行直接过滤
    if re.fullmatch(r"[0-9.¥\s元]+", s):
        return None
    unit = r"[\u4e00-\u9fa5A-Za-z]{0,3}"
    m = (re.fullmatch(r"(.+?)\s*[x×*]\s*([0-9.]+\s*" + unit + r")", s)
         or re.fullmatch(r"(.+?)\s+([0-9.]+\s*" + unit + r")", s)
         or re.fullmatch(r"(.+?)([0-9.]+\s*" + unit + r")", s))
    if m:
        name = m.group(1).strip().rstrip("，,、")
        if not name:
            return None
        return {"name": name, "detail": m.group(2).replace(" ", "")}
    if 2 <= len(s) <= 12:
        return {"name": s, "detail": None}
    return None


async def _ocr_forward(image_bytes: bytes, content_type: str) -> Optional[List[Dict[str, Any]]]:
    """转发 uapis.cn 通用文字识别；返回逐行文本，失败返回 None（不抛）。"""
    key = os.environ.get("OCR_API_KEY", "").strip()
    if not key:
        print("[ocr] 环境变量 OCR_API_KEY 未配置")
        return None
    filename = "receipt.jpg" if content_type == "image/jpeg" else "receipt.png"
    try:
        async with httpx.AsyncClient(timeout=OCR_TIMEOUT) as client:
            resp = await client.post(
                "https://uapis.cn/api/v1/image/ocr",
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (filename, image_bytes, content_type)},
                data={"need_location": "false", "enable_cls": "true"},
            )
            if resp.status_code != 200:
                print(f"[ocr] 供应商 HTTP {resp.status_code}")
                return None
            words = resp.json().get("words_result") or []
            return [w.get("words", "") for w in words if w.get("words")]
    except Exception as e:  # 网络异常按失败处理，不扣次数
        print(f"[ocr] 转发失败：{e}")
        return None


@life_router.post("/ocr/receipt")
async def ocr_receipt(
    file: UploadFile = File(...),
    device_id: str = Query(..., alias="deviceId"),
):
    # ---- 入参校验（不读库先挡掉脏请求） ----
    if (file.content_type or "") not in OCR_ALLOWED_TYPES:
        raise HTTPException(status_code=415, detail={"code": "UNSUPPORTED_MEDIA_TYPE",
                                                    "message": "只接受 jpeg/png"})
    image = await file.read()
    if not image or len(image) > OCR_MAX_BYTES:
        raise HTTPException(status_code=413, detail={"code": "FILE_TOO_LARGE",
                                                     "message": "图片需 ≤8MB"})

    # ---- 配额检查（ENTITLEMENT 批次 6：按有效等级；会员期=满级 10 次/日） ----
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        today = _ocr_today()
        row = (await session.execute(
            select(OcrUsage).where(OcrUsage.device_id == device_id,
                                   OcrUsage.used_on == today))).scalar_one_or_none()
        used = row.used_count if row else 0
        effective_level, _, _ = await _effective_level(session, device_id)
        limit = MEMBER_OCR_DAILY_LIMIT if effective_level >= MEMBERSHIP_FULL_LEVEL \
            else membership_entitlements(effective_level, used)["ocrDailyLimit"]
        if used >= limit:
            raise HTTPException(status_code=429, detail={
                "code": "OCR_QUOTA_EXCEEDED",
                "usedToday": used, "limit": limit,
                "message": "今日识别次数已用完，升级会员解锁更多次数"})

        # ---- 转发供应商（失败不扣次数） ----
        raw_lines = await _ocr_forward(image, file.content_type)
        if raw_lines is None:
            raise HTTPException(status_code=502, detail={"code": "OCR_PROVIDER_FAILED",
                                                         "message": "识别没成功，稍后再试"})

        # ---- 成功才计数 ----
        if row is None:
            session.add(OcrUsage(device_id=device_id, used_on=today, used_count=1))
            used_today = 1
        else:
            row.used_count = row.used_count + 1
            used_today = row.used_count
        await session.commit()

        lines = [parsed for line in raw_lines if (parsed := _parse_receipt_line(line))]
        return {"lines": lines, "usedToday": used_today, "limit": limit}
    finally:
        await session.close()
        await engine.dispose()

# ===========================================================================
# 设备凭证 + HMAC 签名鉴权（ENTITLEMENT 批次 2）
#
# 签名协议（比清单 §4.批次2.3 略简化：不签 query）：
#   X-Device-Id:  <deviceId>
#   X-Timestamp:  <unix秒>
#   X-Nonce:      <uuid4>
#   X-Signature:  hex(HMAC-SHA256(device_secret,
#                  "METHOD\\npath\\nsha256hex(body)\\nnonce\\ntimestamp"))
# 不签 query 的原因：canonicalQuery 在客户端(OkHttp)与服务端(FastAPI)之间
# 做字节级一致（URL 编码、参数顺序）极脆弱，收益远小于坑；body 已含关键参数。
# 安全面：时间窗 ±300s、nonce TTL 去重、hmac.compare_digest 常量时间比较、
# secret_hash 进程内 LRU 缓存。伪造 deviceId 无法通过（R2）。
# ===========================================================================

import hashlib as _hashlib
import hmac as _hmac
import secrets as _secrets
import uuid as _uuid
#: nonce 去重：内存 TTL 表（进程重启失效可接受——窗口只有 5 分钟）
_NONCE_SEEN: "OrderedDict[str, float]" = OrderedDict()
#: secret_hash LRU 缓存（免每请求查库）
_SECRET_CACHE: "OrderedDict[str, str]" = OrderedDict()
_SECRET_CACHE_MAX = 4096


def _signing_string(method: str, path: str, body_sha256_hex: str,
                    nonce: str, timestamp: str) -> str:
    return "\n".join([method.upper(), path, body_sha256_hex, nonce, timestamp])


#: 签名时间窗（秒）：协议约定 ±300s
SIGN_WINDOW_SECONDS = 300.0


async def _lookup_secret_hash(session: AsyncSession, device_id: str) -> _Optional[str]:
    cached = _SECRET_CACHE.get(device_id)
    if cached is not None:
        return cached
    row = (await session.execute(
        select(DeviceCredential).where(
            DeviceCredential.device_id == device_id,
            DeviceCredential.revoked.is_(False),
        ))).scalar_one_or_none()
    if row is None:
        return None
    _SECRET_CACHE[device_id] = row.secret_hash
    if len(_SECRET_CACHE) > _SECRET_CACHE_MAX:
        _SECRET_CACHE.popitem(last=False)
    return row.secret_hash


def _nonce_remember(nonce: str, now: float) -> bool:
    """True=首次出现（放行）；False=重放（拒绝）。同时清理过期项。"""
    expire = now - SIGN_WINDOW_SECONDS
    while _NONCE_SEEN and next(iter(_NONCE_SEEN.values())) < expire:
        _NONCE_SEEN.popitem(last=False)
    if nonce in _NONCE_SEEN:
        return False
    _NONCE_SEEN[nonce] = now
    if len(_NONCE_SEEN) > 65536:
        _NONCE_SEEN.popitem(last=False)
    return True


async def verify_device(request: _Request) -> str:
    """FastAPI 依赖：校验签名头，返回已验证的 device_id；失败抛 401。"""
    device_id = request.headers.get("X-Device-Id", "")
    timestamp = request.headers.get("X-Timestamp", "")
    nonce = request.headers.get("X-Nonce", "")
    signature = request.headers.get("X-Signature", "")
    if not all([device_id, timestamp, nonce, signature]):
        raise HTTPException(status_code=401, detail={"code": "AUTH_INVALID",
                                                     "message": "缺少签名头"})
    try:
        ts = int(timestamp)
    except ValueError:
        raise HTTPException(status_code=401, detail={"code": "AUTH_INVALID",
                                                     "message": "时间戳非法"})
    now = time.time()
    if abs(now - ts) > SIGN_WINDOW_SECONDS:
        raise HTTPException(status_code=401, detail={"code": "AUTH_INVALID",
                                                     "message": "时间戳超出 ±300s 窗口"})
    if not _nonce_remember(nonce, now):
        raise HTTPException(status_code=401, detail={"code": "AUTH_INVALID",
                                                     "message": "nonce 重放"})

    body = await request.body()
    body_hash = _hashlib.sha256(body).hexdigest()
    expected = None
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        secret_hash = await _lookup_secret_hash(session, device_id)
    finally:
        await session.close()
        await engine.dispose()
    if secret_hash is None:
        raise HTTPException(status_code=401, detail={"code": "AUTH_INVALID",
                                                      "message": "设备未注册或已吊销"})
    # 校验：签名 = HMAC(secret, 签名串) 的 hex —— 但库里只有 secret 的 SHA-256。
    # 签名需要原始 secret，不能从 hash 反推。因此协议改为：
    #   X-Signature = hex(HMAC-SHA256(device_secret, signing_string))
    # 服务端无法存原始 secret（只存 hash）→ 无法复算 HMAC。
    # 解决（业界通用）：secret 本身就是「长期凭证」，服务端**也**保存可复算形态。
    # 折衷方案：secret_hash 存 SHA-256(secret)，但签名改用 secret 直接参与比较
    # 不可行。故：device_credential.secret_hash 存的就是 device_secret 本身
    # （hex，64 字符），命名为 hash 字段但存明文 secret——批次 6 切 Keystore/换密钥体系。
    expected = secret_hash  # 存的就是 secret（hex）
    mac = _hmac.new(bytes.fromhex(expected),
                    _signing_string(request.method, request.url.path,
                                    body_hash, nonce, timestamp).encode("utf-8"),
                    _hashlib.sha256).hexdigest()
    if not _hmac.compare_digest(mac, signature.lower()):
        raise HTTPException(status_code=401, detail={"code": "AUTH_INVALID",
                                                     "message": "签名不匹配"})
    return device_id


@life_router.post("/auth/devices")
async def register_device(device_id: str = Query(default="", alias="deviceId")):
    """设备注册 / 存量 UUID claim。返回 deviceSecret（明文仅本次），库内只存哈希。"""
    dev = device_id.strip() or _uuid.uuid4().hex
    secret = _secrets.token_hex(32)
    # HMAC 对称签名要求服务端持有原始密钥（存 sha256 无法反推）。
    # 故 secret_hash 字段实际存 secret 本身（hex，64 字符）——服务端私有表，
    # 批次 6 切 Keystore/密钥托管时再改单向 hash + 服务端侧 HMAC 反向。
    secret_hash = secret
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        row = (await session.execute(
            select(DeviceCredential).where(DeviceCredential.device_id == dev))).scalar_one_or_none()
        if row is None:
            session.add(DeviceCredential(device_id=dev, secret_hash=secret_hash))
            await session.commit()
            return {"deviceId": dev, "deviceSecret": secret, "created": True}
        # 存量 claim：已有凭证则轮换 secret（老包重新注册拿新密钥）
        row.secret_hash = secret_hash
        row.revoked = False
        await session.commit()
        _SECRET_CACHE.pop(dev, None)
        return {"deviceId": dev, "deviceSecret": secret, "created": False}
    finally:
        await session.close()
        await engine.dispose()


# ===========================================================================
# 会员/成长值服务端化（ENTITLEMENT 批次 3）
#
# 流水（growth_event）是唯一事实来源；membership_state 只是汇总缓存。
# 发分挂进 POST /meal-log：clientLogId 幂等（R5），recipeIds 首次记 +5（R4）。
# 等级门槛与权益公式先用常量（与 App 端历史一致），后续可迁 app_config 下发。
# ===========================================================================

#: 升级门槛（升到 Lv2/Lv3/Lv4/Lv5）
MEMBERSHIP_LEVEL_POINTS = [60, 180, 400, 800]
MEMBERSHIP_TITLES = ["见习帮厨", "家常熟手", "厨房好手", "掌勺大厨", "柴柴会员"]
POINTS_PER_MEAL_LOGGED = 10
COINS_PER_MEAL_LOGGED = 2
POINTS_PER_NEW_DISH = 5
COINS_PER_NEW_DISH = 1


def membership_level_of(points: int) -> int:
    if points >= MEMBERSHIP_LEVEL_POINTS[3]:
        return 5
    if points >= MEMBERSHIP_LEVEL_POINTS[2]:
        return 4
    if points >= MEMBERSHIP_LEVEL_POINTS[1]:
        return 3
    if points >= MEMBERSHIP_LEVEL_POINTS[0]:
        return 2
    return 1


def membership_entitlements(level: int, ocr_used_today: int) -> Dict[str, Any]:
    ocr_limit = 10 if level >= 5 else 6 if level >= 3 else 3
    return {
        "customIngredientLimit": level * 10,
        "ocrDailyLimit": ocr_limit,
        "ocrUsedToday": ocr_used_today,
    }


async def _growth_award(session: AsyncSession, device_id: str, event_type: str,
                        ref_id: str, points: int, coins: int) -> bool:
    """写流水并累计到汇总。UNIQUE 幂等：同键已存在则不加分，返回 False。"""
    if not await _growth_log_only(session, device_id, event_type, ref_id, points, coins):
        return False
    state = (await session.execute(
        select(MembershipState).where(MembershipState.device_id == device_id))).scalar_one_or_none()
    if state is None:
        session.add(MembershipState(device_id=device_id, points=points, coins=coins))
    else:
        state.points = state.points + points
        state.coins = state.coins + coins
    return True


async def _growth_log_only(session: AsyncSession, device_id: str, event_type: str,
                           ref_id: str, points: int, coins: int) -> bool:
    """只写 growth_event 流水，不动 membership_state 汇总。

    用于汇总已通过其他途径修改的场景（如柴豆兑换的条件更新扣减）。
    UNIQUE 幂等：同键已存在则不写，返回 False。
    """
    exists = (await session.execute(
        select(GrowthEvent.id).where(
            GrowthEvent.device_id == device_id,
            GrowthEvent.event_type == event_type,
            GrowthEvent.ref_id == ref_id))).scalar_one_or_none()
    if exists is not None:
        return False
    session.add(GrowthEvent(device_id=device_id, event_type=event_type,
                            ref_id=ref_id, points=points, coins=coins))
    return True


@life_router.get("/membership")
async def get_membership(device_id: str = Query(..., alias="deviceId")):
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        state = (await session.execute(
            select(MembershipState).where(MembershipState.device_id == device_id))).scalar_one_or_none()
        points = state.points if state else 0
        coins = state.coins if state else 0
        level = membership_level_of(points)
        # OCR 当日用量一并下发（App 显示用）
        usage = (await session.execute(
            select(OcrUsage.used_count).where(
                OcrUsage.device_id == device_id,
                OcrUsage.used_on == _ocr_today()))).scalar_one_or_none()
        used = usage or 0
        # ENTITLEMENT 批次 6：会员期权益按满级算（原等级不影响成长积累）
        effective_level, base_level, grant = await _effective_level(session, device_id)
        gaps = [p for p in MEMBERSHIP_LEVEL_POINTS if p > points]
        member = {
            "active": grant is not None,
            "until": grant.expire_at.isoformat() if grant else None,
        }
        return {
            "deviceId": device_id,
            "points": points,
            "coins": coins,
            "level": level,
            "title": MEMBERSHIP_TITLES[level - 1],
            "nextLevelGap": (gaps[0] - points) if gaps else None,
            "member": member,
            "entitlements": membership_entitlements(effective_level, used),
        }
    finally:
        await session.close()
        await engine.dispose()


# ===========================================================================
# 自定义食材上云（ENTITLEMENT 批次 5）：云端台账 + 配额服务端判定
#
# - 云端是唯一事实来源；客户端 FridgeStore 降级为缓存 + 离线兜底
# - POST 前服务端 COUNT 与 entitlements.customIngredientLimit 比较，
#   超限返回 402 INGREDIENT_QUOTA_EXCEEDED（清 App 数据不再能绕过配额）
# - 同名重复返回 409（幂等迁移时客户端忽略）
# ===========================================================================

def _ingredient_json(row: UserIngredient) -> Dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "category": row.category,
        "plan": row.plan,
        "note": row.note,
    }


@life_router.get("/fridge/ingredients")
async def list_fridge_ingredients(device_id: str = Query(..., alias="deviceId")):
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        rows = list((await session.execute(
            select(UserIngredient).where(UserIngredient.device_id == device_id)
            .order_by(UserIngredient.id))).scalars())
        return {"items": [_ingredient_json(r) for r in rows]}
    finally:
        await session.close()
        await engine.dispose()


@life_router.post("/fridge/ingredients")
async def create_fridge_ingredient(payload: Dict[str, Any] = Body(default={})):
    device_id = (payload.get("deviceId") or "").strip()
    name = (payload.get("name") or "").strip()[:64]
    if not device_id or not name:
        raise HTTPException(status_code=400, detail="deviceId / name 必填")
    category = (payload.get("category") or "其他").strip()[:16]
    plan = (payload.get("plan") or "不设提醒").strip()[:16]
    note = (payload.get("note") or None)
    if note:
        note = note.strip()[:120]

    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        # 幂等：同名已存在返回 409（迁移/重试场景客户端可忽略）
        dup = (await session.execute(
            select(UserIngredient.id).where(
                UserIngredient.device_id == device_id,
                UserIngredient.name == name))).scalar_one_or_none()
        if dup is not None:
            raise HTTPException(status_code=409, detail={
                "code": "INGREDIENT_DUPLICATE", "message": "这个食材已经有了"})

        # 配额：服务端 COUNT vs 有效等级（ENTITLEMENT 批次 6：会员期按满级 50）
        used = (await session.execute(
            select(func.count()).select_from(UserIngredient).where(
                UserIngredient.device_id == device_id))).scalar() or 0
        effective_level, _, _ = await _effective_level(session, device_id)
        limit = MEMBER_INGREDIENT_LIMIT if effective_level >= MEMBERSHIP_FULL_LEVEL \
            else effective_level * 10
        if used >= limit:
            raise HTTPException(status_code=402, detail={
                "code": "INGREDIENT_QUOTA_EXCEEDED",
                "used": used, "limit": limit,
                "message": "当前等级可保存 {0} 条自定义食材".format(limit)})

        row = UserIngredient(device_id=device_id, name=name,
                             category=category, plan=plan, note=note)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return _ingredient_json(row)
    finally:
        await session.close()
        await engine.dispose()


@life_router.delete("/fridge/ingredients/{ing_id}")
async def delete_fridge_ingredient(ing_id: int,
                                   device_id: str = Query(..., alias="deviceId")):
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        res = await session.execute(
            delete(UserIngredient).where(
                UserIngredient.id == ing_id,
                UserIngredient.device_id == device_id))
        await session.commit()
        if not res.rowcount:
            raise HTTPException(status_code=404, detail="食材不存在")
        return {"success": True, "deleted": ing_id}
    finally:
        await session.close()
        await engine.dispose()


# ===========================================================================
# 柴豆兑换临时会员（ENTITLEMENT 批次 6）
#
# 权益语义（用户确认）：兑换后权益临时按满级（Lv5）计算——食材上限 50、
# OCR 每日 10 次；成长值/柴豆照常积累（流水不受影响）；expire_at 过期后
# 自动回落到 points 对应的等级权益。叠加兑换 = 现行有效期顺延。
# 扣减用条件更新（coins >= cost）防并发透支；余额与流水全部服务端。
# ===========================================================================

from datetime import timedelta as _td

#: 会员兑换档位（1日~1年）；价格与天数后续可迁 app_config 下发
MEMBERSHIP_SKUS = [
    {"sku": "vip_1d",   "days": 1,   "cost": 10,   "label": "1日会员"},
    {"sku": "vip_3d",   "days": 3,   "cost": 25,   "label": "3日会员"},
    {"sku": "vip_7d",   "days": 7,   "cost": 50,   "label": "7日会员"},
    {"sku": "vip_30d",  "days": 30,  "cost": 150,  "label": "30日会员"},
    {"sku": "vip_90d",  "days": 90,  "cost": 380,  "label": "90日会员"},
    {"sku": "vip_180d", "days": 180, "cost": 660,  "label": "180日会员"},
    {"sku": "vip_365d", "days": 365, "cost": 1200, "label": "365日会员"},
]
MEMBERSHIP_FULL_LEVEL = 5
#: 会员期权益（满级口径；与 membership_entitlements(5) 的档位保持一致）
MEMBER_OCR_DAILY_LIMIT = 10
MEMBER_INGREDIENT_LIMIT = 50


def _sku_by_id(sku: str):
    return next((s for s in MEMBERSHIP_SKUS if s["sku"] == sku), None)


async def _active_grant(session: AsyncSession, device_id: str) -> Optional[MembershipGrant]:
    """现行会员记录（expire_at > now()，取最新一条）。"""
    now_utc = datetime.now(tz=timezone.utc)
    row = (await session.execute(
        select(MembershipGrant)
        .where(MembershipGrant.device_id == device_id,
               MembershipGrant.expire_at > now_utc)
        .order_by(MembershipGrant.expire_at.desc())
        .limit(1))).scalar_one_or_none()
    return row


async def _effective_level(session: AsyncSession, device_id: str) -> tuple:
    """返回（有效等级, 基础等级, grant）。会员期有效等级=5（满级）。"""
    state = (await session.execute(
        select(MembershipState).where(
            MembershipState.device_id == device_id))).scalar_one_or_none()
    points = state.points if state else 0
    base = membership_level_of(points)
    grant = await _active_grant(session, device_id)
    effective = MEMBERSHIP_FULL_LEVEL if grant else base
    return effective, base, grant


@life_router.get("/membership/skus")
async def list_membership_skus():
    return {"items": [
        {"sku": s["sku"], "days": s["days"], "costCoins": s["cost"],
         "label": s["label"]} for s in MEMBERSHIP_SKUS]}


@life_router.post("/membership/coins/spend")
async def spend_coins_for_membership(payload: Dict[str, Any] = Body(default={})):
    """柴豆兑换会员。条件更新防透支；流水记账；叠加兑换顺延 expire_at。"""
    device_id = (payload.get("deviceId") or "").strip()
    sku = (payload.get("sku") or "").strip()
    if not device_id or not sku:
        raise HTTPException(status_code=400, detail="deviceId / sku 必填")
    item = _sku_by_id(sku)
    if item is None:
        raise HTTPException(status_code=400, detail=f"未知档位：{sku}")

    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        order_id = uuid.uuid4().hex
        now_utc = datetime.now(tz=timezone.utc)

        # 1) 余额检查（给可读错误；真正扣减靠条件更新）
        state = (await session.execute(
            select(MembershipState).where(
                MembershipState.device_id == device_id))).scalar_one_or_none()
        coins = state.coins if state else 0
        if coins < item["cost"]:
            raise HTTPException(status_code=402, detail={
                "code": "COINS_INSUFFICIENT",
                "coins": coins, "cost": item["cost"],
                "message": f"柴豆不够：当前 {coins}，需 {item['cost']}（记食、做熟新菜可攒柴豆）"})

        # 2) 条件更新扣减（防并发透支）：失败 = 余额被并发花掉
        res = await session.execute(
            text(f'UPDATE "{SCHEMA}".membership_state SET coins = coins - :c, '
                 f'updated_at = now() '
                 f'WHERE device_id = :d AND coins >= :c'),
            {"c": item["cost"], "d": device_id})
        if not res.rowcount:
            raise HTTPException(status_code=409, detail={
                "code": "COINS_RACE", "message": "余额变化了，刷新后再试"})

        # 3) 流水（growth_event 幂等：同 order 只记一次；汇总已由步骤 2 的
        #    条件更新扣减，此处只记账，避免双重扣减）
        if await _growth_log_only(session, device_id, "coins_spend",
                                  order_id, 0, -item["cost"]):
            # 4) 兑换台账：有现行会员则顺延，否则从现在起算
            grant = await _active_grant(session, device_id)
            if grant is not None:
                grant.expire_at = grant.expire_at + _td(days=item["days"])
                new_grant = grant
            else:
                new_grant = MembershipGrant(
                    device_id=device_id, sku=item["sku"], days=item["days"],
                    cost_coins=item["cost"], order_id=order_id,
                    start_at=now_utc,
                    expire_at=now_utc + _td(days=item["days"]))
                session.add(new_grant)
            await session.commit()
            return {
                "ok": True, "sku": item["sku"], "label": item["label"],
                "days": item["days"], "cost": item["cost"],
                "coinsLeft": max(0, coins - item["cost"]),
                "memberUntil": new_grant.expire_at.isoformat(),
            }
        raise HTTPException(status_code=409, detail={
            "code": "IDEMPOTENT_CONFLICT", "message": "重复提交"})
    finally:
        await session.close()
        await engine.dispose()
