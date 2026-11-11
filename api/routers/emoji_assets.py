# -*- coding: utf-8 -*-
"""表情包库：归档重复出现、疑似表情包的图片，并从 S3 去重链路中豁免。

判定依据（内容 + 使用场景）：
- 同一 sha256 出现在多个不同 content_id（帖子/评论）→ 复用型图片
- 关联的 media_attachment 主要来自评论 → 表情包概率高
- 体积偏小 → 加分（表情包通常 < 200KB）

归档后该 checksum 不再出现在去重分组里，也不会被删除，避免误清理。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from database.models_v2 import EmojiAsset, MediaAttachment, MediaBackup, Story
from ..service_paths import library_api

router = APIRouter(prefix="/emoji-assets", tags=["emoji-assets"])

#: 至少出现在几个不同内容里才算「复用型图片」
MIN_USES = 3
#: 表情包体积上限（超过仍可因复用次数多而入选）
EMOJI_MAX_BYTES = 200 * 1024


async def _engine():
    from config.db_config import resolve_config_db_type
    from database.db_session import get_async_engine

    return get_async_engine(resolve_config_db_type())


async def _session():
    from sqlalchemy.ext.asyncio import AsyncSession

    engine = await _engine()
    if engine is None:
        return None, None
    return engine, AsyncSession(engine, expire_on_commit=False)


async def ensure_table() -> None:
    """幂等建表（首次请求兜底，正常由 startup 建好）。"""
    from database.models_v2 import BaseV2

    engine = await _engine()
    if engine is None:
        return
    try:
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: EmojiAsset.__table__.create(
                    bind=sync_conn, checkfirst=True))
    finally:
        await engine.dispose()


def _asset_out(r: EmojiAsset, story_titles: Optional[Dict[int, str]] = None) -> Dict[str, Any]:
    sids = list(r.story_ids or [])
    return {
        "id": r.id,
        "checksum": r.checksum,
        "object_key": r.object_key,
        "preview_url": library_api(f"s3-backup/object/{r.object_key}"),
        "name": r.name or "",
        "source": r.source,
        "use_count": int(r.use_count or 0),
        "size_bytes": int(r.size_bytes or 0),
        "mime_type": r.mime_type or "",
        "origin_keys": list(r.origin_keys or []),
        "story_ids": sids,
        "story_titles": [story_titles[s] for s in sids
                         if story_titles and s in story_titles],
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.get("")
async def list_assets(
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=200),
    keyword: Optional[str] = None,
):
    """表情包库列表（按使用次数倒序）。"""
    engine, session = await _session()
    if session is None:
        return {"items": [], "total": 0}
    try:
        q = select(EmojiAsset).where(EmojiAsset.deleted_at.is_(None))
        if keyword:
            q = q.where(or_(EmojiAsset.name.ilike(f"%{keyword}%"),
                            EmojiAsset.checksum.ilike(f"%{keyword}%")))
        total = (await session.execute(
            select(func.count()).select_from(q.subquery()))).scalar() or 0
        rows = (await session.execute(
            q.order_by(EmojiAsset.use_count.desc(), EmojiAsset.id.desc())
            .offset((page - 1) * page_size).limit(page_size)
        )).scalars().all()
        sids = {s for r in rows for s in (r.story_ids or [])}
        titles: Dict[int, str] = {}
        if sids:
            titles = {sid: (title or "") for sid, title in (await session.execute(
                select(Story.id, Story.title).where(Story.id.in_(sids)))).all()}
        return {"items": [_asset_out(r, titles) for r in rows],
                "total": total, "page": page, "page_size": page_size}
    finally:
        await session.close()
        await engine.dispose()


async def _backup_rows(session, keys, item_ids, checksums) -> List[MediaBackup]:
    conds = []
    if keys:
        conds.append(MediaBackup.object_key.in_(list(keys)))
    if item_ids:
        conds.append(MediaBackup.id.in_(list(item_ids)))
    if checksums:
        conds.append(MediaBackup.checksum.in_(list(checksums)))
    if not conds:
        return []
    return (await session.execute(
        select(MediaBackup).where(
            or_(*conds),
            MediaBackup.checksum.isnot(None),
            MediaBackup.checksum != "",
        )
    )).scalars().all()


async def archive_rows(session, rows: List[MediaBackup], source: str = "manual",
                       name: Optional[str] = None) -> Dict[str, Any]:
    """按 checksum 归档（幂等）。不改 MediaBackup 的 object_key，避免破坏引用链。"""
    groups: Dict[str, List[MediaBackup]] = {}
    for r in rows:
        if r.checksum:
            groups.setdefault(r.checksum, []).append(r)

    archived = updated = 0
    for csum, items in groups.items():
        origin = list(dict.fromkeys([i.object_key for i in items]))
        sids = list(dict.fromkeys(
            [int(i.story_id) for i in items if i.story_id]))
        canon = next((k for k in origin
                      if k.startswith(("emoji/", "dedup/"))), origin[0])
        existing = (await session.execute(
            select(EmojiAsset).where(EmojiAsset.checksum == csum)
        )).scalar_one_or_none()
        if existing is not None:
            existing.object_key = canon
            existing.origin_keys = list(dict.fromkeys(
                list(existing.origin_keys or []) + origin))
            existing.story_ids = list(dict.fromkeys(
                list(existing.story_ids or []) + sids))
            existing.use_count = len(existing.origin_keys or [])
            existing.deleted_at = None
            if name and not existing.name:
                existing.name = name
            updated += 1
        else:
            session.add(EmojiAsset(
                checksum=csum,
                object_key=canon,
                name=name or "",
                source=source,
                use_count=len(origin),
                size_bytes=int(items[0].size_bytes or 0),
                mime_type=items[0].mime_type or "",
                origin_keys=origin,
                story_ids=sids,
            ))
            archived += 1
    await session.commit()
    return {"archived": archived, "updated": updated,
            "matched_rows": len(rows), "groups": len(groups)}


class ArchivePayload(BaseModel):
    keys: List[str] = Field(default_factory=list)
    item_ids: List[int] = Field(default_factory=list)
    checksums: List[str] = Field(default_factory=list)
    name: str = ""
    source: str = "manual"


@router.post("/archive")
async def archive(payload: ArchivePayload = Body(default=ArchivePayload())):
    """把指定对象键 / 备份记录 / checksum 归档进表情包库（批量、幂等）。"""
    await ensure_table()
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        rows = await _backup_rows(session, payload.keys, payload.item_ids,
                                  payload.checksums)
        if not rows:
            return {"archived": 0, "updated": 0, "matched_rows": 0,
                    "groups": 0, "note": "没有匹配到可归档的备份记录"}
        return await archive_rows(session, rows, payload.source,
                                  payload.name or None)
    finally:
        await session.close()
        await engine.dispose()


async def detect_candidates(session, min_uses: int = MIN_USES,
                            limit: int = 200) -> List[Dict[str, Any]]:
    """扫描备份，找出疑似表情包（复用次数多 + 主要出现在评论里）。"""
    rows = (await session.execute(
        select(MediaBackup).where(
            MediaBackup.status == "uploaded",
            MediaBackup.checksum.isnot(None),
            MediaBackup.checksum != "",
        )
    )).scalars().all()
    by_csum: Dict[str, List[MediaBackup]] = {}
    for r in rows:
        by_csum.setdefault(r.checksum, []).append(r)

    media_ids = {r.media_id for r in rows if r.media_id}
    comment_media: set = set()
    if media_ids:
        comment_media = set((await session.execute(
            select(MediaAttachment.id).where(
                MediaAttachment.id.in_(media_ids),
                MediaAttachment.target_type == "comment",
            )
        )).scalars().all())

    out: List[Dict[str, Any]] = []
    for csum, items in by_csum.items():
        cids = {i.content_id for i in items if i.content_id}
        uses = len(cids)
        if uses < min_uses:
            continue
        hits = sum(1 for i in items if i.media_id in comment_media)
        ratio = hits / max(1, len(items))
        size = int(items[0].size_bytes or 0)
        score = uses * 10 + ratio * 50 + (20 if 0 < size < EMOJI_MAX_BYTES else 0)
        # 评论占比过半，或复用特别多，才判定为表情包
        if ratio < 0.5 and uses < 5:
            continue
        out.append({
            "checksum": csum,
            "use_count": uses,
            "rows": len(items),
            "comment_ratio": round(ratio, 2),
            "size_bytes": size,
            "score": round(score, 1),
            "keys": list(dict.fromkeys([i.object_key for i in items])),
            "story_ids": list(dict.fromkeys(
                [int(i.story_id) for i in items if i.story_id])),
        })
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:limit]


class DetectPayload(BaseModel):
    min_uses: int = MIN_USES
    limit: int = 200
    auto_archive: bool = False


@router.post("/detect")
async def detect(payload: DetectPayload = Body(default=DetectPayload())):
    """识别评论中的表情包候选；auto_archive=true 时直接归档。"""
    await ensure_table()
    engine, session = await _session()
    if session is None:
        return {"items": [], "total": 0, "archived": 0}
    try:
        items = await detect_candidates(session, payload.min_uses, payload.limit)
        archived = 0
        if payload.auto_archive and items:
            rows = await _backup_rows(session, [], [],
                                      [i["checksum"] for i in items])
            archived = (await archive_rows(session, rows, source="auto"))["archived"]
        return {"items": items, "total": len(items), "archived": archived}
    finally:
        await session.close()
        await engine.dispose()


@router.delete("/{asset_id}")
async def remove(asset_id: int):
    """移出表情包库（软删，图片本身不动）。"""
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        row = (await session.execute(
            select(EmojiAsset).where(EmojiAsset.id == asset_id)
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        row.deleted_at = datetime.now(timezone.utc)
        await session.commit()
        return {"ok": True, "id": asset_id}
    finally:
        await session.close()
        await engine.dispose()
