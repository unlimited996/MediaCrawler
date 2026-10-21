# -*- coding: utf-8 -*-
"""回复态度标签库（P3）：标签与规则的增删改查。

判定逻辑与代码解耦：标签定义全部存库，改完版本号 +1，判定进程读到新版本
立即重载内存缓存，下次判断即生效。
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from sqlalchemy import select

from database.models_v2 import StanceTag, StanceTagRule, StanceTagVersion
from .story import _session

router = APIRouter(prefix="/stance-tags", tags=["stance-tags"])


async def _bump_version(session) -> int:
    row = (await session.execute(
        select(StanceTagVersion).order_by(StanceTagVersion.id).limit(1)
    )).scalar_one_or_none()
    if row is None:
        row = StanceTagVersion(version=1)
        session.add(row)
        await session.flush()
        return 1
    row.version = int(row.version or 0) + 1
    return int(row.version)


def _tag_out(t: StanceTag, rule_count: int = 0) -> Dict[str, Any]:
    return {
        "id": t.id,
        "code": t.code,
        "name": t.name,
        "name_i18n": t.name_i18n or {},
        "polarity": t.polarity,
        "color": t.color,
        "description": t.description or "",
        "examples": t.examples or [],
        "enabled": bool(t.enabled),
        "sort_order": t.sort_order or 0,
        "priority": t.priority or 100,
        "is_fallback": bool(t.is_fallback),
        "rule_count": rule_count,
    }


def _rule_out(r: StanceTagRule) -> Dict[str, Any]:
    return {
        "id": r.id,
        "tag_id": r.tag_id,
        "match_type": r.match_type,
        "pattern": r.pattern,
        "weight": float(r.weight or 1.0),
        "negatable": bool(r.negatable),
        "enabled": bool(r.enabled),
        "note": r.note or "",
    }


async def _invalidate():
    """改完标签库立即刷新判定进程的内存缓存（下次判断即生效）。"""
    try:
        from tools.stance import load_library
        await load_library(force=True)
    except Exception:
        pass


@router.get("")
async def list_tags(include_disabled: bool = Query(True)):
    """标签列表（含规则条数）。"""
    engine, session = await _session()
    if session is None:
        return []
    try:
        q = select(StanceTag)
        if not include_disabled:
            q = q.where(StanceTag.enabled.is_(True))
        tags = (await session.execute(
            q.order_by(StanceTag.priority, StanceTag.sort_order))).scalars().all()
        from sqlalchemy import func
        counts = {row[0]: int(row[1]) for row in (await session.execute(
            select(StanceTagRule.tag_id, func.count(StanceTagRule.id))
            .group_by(StanceTagRule.tag_id))).all()}
        return [_tag_out(t, counts.get(t.id, 0)) for t in tags]
    finally:
        await session.close()
        await engine.dispose()


@router.post("")
async def create_tag(payload: Dict[str, Any] = Body(default={})):
    """新增标签。"""
    code = str(payload.get("code") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not code or not name:
        raise HTTPException(status_code=400, detail="code 与 name 必填")
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="数据库不可用")
    try:
        exists = (await session.execute(
            select(StanceTag).where(StanceTag.code == code))).scalar_one_or_none()
        if exists:
            raise HTTPException(status_code=400, detail=f"标签编码 {code} 已存在")
        tag = StanceTag(
            code=code, name=name,
            name_i18n=payload.get("name_i18n") or {},
            polarity=payload.get("polarity") or "neutral",
            color=payload.get("color"),
            description=payload.get("description") or "",
            examples=payload.get("examples") or [],
            enabled=bool(payload.get("enabled", True)),
            sort_order=int(payload.get("sort_order") or 0),
            priority=int(payload.get("priority") or 100),
            is_fallback=bool(payload.get("is_fallback", False)),
        )
        session.add(tag)
        await session.flush()
        # 兜底标签唯一
        if tag.is_fallback:
            for other in (await session.execute(
                    select(StanceTag).where(StanceTag.id != tag.id))).scalars().all():
                other.is_fallback = False
        await _bump_version(session)
        await session.commit()
        await _invalidate()
        return _tag_out(tag, 0)
    finally:
        await session.close()
        await engine.dispose()


@router.put("/{tag_id}")
async def update_tag(tag_id: int, payload: Dict[str, Any] = Body(default={})):
    """改标签（名称/描述/示例/颜色/启用/排序/优先级/兜底）。"""
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="数据库不可用")
    try:
        tag = await session.get(StanceTag, tag_id)
        if tag is None:
            raise HTTPException(status_code=404, detail="标签不存在")
        for key in ("name", "polarity", "color", "description", "sort_order",
                    "priority", "examples", "name_i18n", "enabled", "is_fallback"):
            if key in payload and payload[key] is not None:
                setattr(tag, key, payload[key])
        if tag.is_fallback:
            for other in (await session.execute(
                    select(StanceTag).where(StanceTag.id != tag.id))).scalars().all():
                other.is_fallback = False
        await _bump_version(session)
        await session.commit()
        await _invalidate()
        return _tag_out(tag)
    finally:
        await session.close()
        await engine.dispose()


@router.post("/{tag_id}/toggle")
async def toggle_tag(tag_id: int, payload: Dict[str, Any] = Body(default={})):
    """启用 / 停用标签。body: {"enabled": bool}"""
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="数据库不可用")
    try:
        tag = await session.get(StanceTag, tag_id)
        if tag is None:
            raise HTTPException(status_code=404, detail="标签不存在")
        tag.enabled = bool(payload.get("enabled", True))
        await _bump_version(session)
        await session.commit()
        await _invalidate()
        return _tag_out(tag)
    finally:
        await session.close()
        await engine.dispose()


@router.delete("/{tag_id}")
async def delete_tag(tag_id: int):
    """删除标签。已有评论引用时改为停用（保留历史判定结果）。"""
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="数据库不可用")
    try:
        tag = await session.get(StanceTag, tag_id)
        if tag is None:
            raise HTTPException(status_code=404, detail="标签不存在")
        from database.models_v2 import Comment
        used = (await session.execute(
            select(Comment.id).where(Comment.stance_tag_id == tag_id).limit(1))
        ).scalar_one_or_none()
        if used:
            tag.enabled = False
            await _bump_version(session)
            await session.commit()
            return {"status": "disabled", "id": tag_id,
                    "message": "已有评论引用该标签，已改为停用"}
        await session.delete(tag)
        await _bump_version(session)
        await session.commit()
        await _invalidate()
        return {"status": "deleted", "id": tag_id}
    finally:
        await session.close()
        await engine.dispose()


@router.get("/{tag_id}/rules")
async def list_rules(tag_id: int):
    engine, session = await _session()
    if session is None:
        return []
    try:
        rules = (await session.execute(
            select(StanceTagRule).where(StanceTagRule.tag_id == tag_id)
            .order_by(StanceTagRule.id))).scalars().all()
        return [_rule_out(r) for r in rules]
    finally:
        await session.close()
        await engine.dispose()


@router.post("/{tag_id}/rules")
async def create_rule(tag_id: int, payload: Dict[str, Any] = Body(default={})):
    match_type = str(payload.get("match_type") or "keyword")
    pattern = str(payload.get("pattern") or "")
    if match_type not in ("keyword", "regex", "sentiment", "laughter", "popular"):
        raise HTTPException(status_code=400, detail="match_type 非法")
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="数据库不可用")
    try:
        tag = await session.get(StanceTag, tag_id)
        if tag is None:
            raise HTTPException(status_code=404, detail="标签不存在")
        rule = StanceTagRule(
            tag_id=tag_id, match_type=match_type, pattern=pattern,
            weight=float(payload.get("weight") or 1.0),
            negatable=bool(payload.get("negatable", True)),
            enabled=bool(payload.get("enabled", True)),
            note=str(payload.get("note") or ""),
        )
        session.add(rule)
        await session.flush()
        await _bump_version(session)
        await session.commit()
        await _invalidate()
        return _rule_out(rule)
    finally:
        await session.close()
        await engine.dispose()


@router.put("/rules/{rule_id}")
async def update_rule(rule_id: int, payload: Dict[str, Any] = Body(default={})):
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="数据库不可用")
    try:
        rule = await session.get(StanceTagRule, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="规则不存在")
        for key in ("match_type", "pattern", "weight", "negatable", "enabled", "note"):
            if key in payload and payload[key] is not None:
                setattr(rule, key, payload[key])
        await _bump_version(session)
        await session.commit()
        await _invalidate()
        return _rule_out(rule)
    finally:
        await session.close()
        await engine.dispose()


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: int):
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="数据库不可用")
    try:
        rule = await session.get(StanceTagRule, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="规则不存在")
        await session.delete(rule)
        await _bump_version(session)
        await session.commit()
        await _invalidate()
        return {"status": "deleted", "id": rule_id}
    finally:
        await session.close()
        await engine.dispose()


@router.post("/refresh")
async def refresh_cache():
    """强制刷新判定进程的内存缓存（改完标签库立即生效）。"""
    from tools.stance import load_library
    lib = await load_library(force=True)
    return {"status": "ok", "version": lib.get("version"),
            "tags": len(lib.get("tags") or [])}


@router.get("/version")
async def get_version():
    from tools.stance import library_version
    return {"version": library_version()}


@router.post("/preview")
async def preview(payload: Dict[str, Any] = Body(default={})):
    """试判定：给一句话，看命中哪个标签（维护时调试用）。"""
    from tools.stance import (extract_post_stance, judge_with_library,
                              load_library)
    text = str(payload.get("text") or "")
    post_title = str(payload.get("post_title") or "")
    post_content = str(payload.get("post_content") or "")
    await load_library()
    ps = extract_post_stance(title=post_title, content=post_content)
    return {"post_stance": ps, "result": judge_with_library(text, ps)}
