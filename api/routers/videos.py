# -*- coding: utf-8 -*-
"""视频转储管理 API（S3 `videos` 桶）。

风格与 api/routers/s3_backup.py 保持一致：_session() 取库、HTTPException 报错、
返回扁平 dict、列表统一 {items,total,page,page_size}。

    POST   /videos                 新增（登记 + 默认立即转储，返回原始文件大小）
    GET    /videos                 查询列表（可按 story/content/status 过滤）
    GET    /videos/{id}            查询详情
    DELETE /videos/{id}            删除记录（连同 S3 对象）
    POST   /videos/run             批量处理待转储记录（后台）
    GET    /videos/{id}/stream     代理播放（支持 Range / 206，进度条可拖动）

所有返回都带 ``original_size_bytes``（源片真实大小，转码前）与
``stored_size_bytes``（S3 对象大小），页面展示用前者。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from database.models_v2 import VideoAsset
from tools import video_store as store
from .story import _session

from ..service_paths import library_api

router = APIRouter(prefix="/videos", tags=["videos"])

#: 手动触发 / 详情页懒触发的批处理任务（同一时刻只跑一个）
_RUN_TASK: Optional[asyncio.Task] = None


def _schedule_batch(limit: int = 5) -> bool:
    """后台跑一批待转储视频；已有任务在跑就直接跳过。"""
    global _RUN_TASK

    async def _run():
        try:
            await store.run_video_batch(limit=limit)
        except Exception as e:
            from tools import utils

            utils.logger.warning(f"[videos] 批量转储失败: {e}")

    if _RUN_TASK is None or _RUN_TASK.done():
        _RUN_TASK = asyncio.create_task(_run())
        return True
    return False


class VideoCreate(BaseModel):
    content_id: str = Field(description="平台原始帖子 ID")
    source_url: str = Field(description="视频直链（优先无水印源片）")
    platform: str = "xhs"
    story_id: Optional[int] = None
    backup_urls: List[str] = []
    run: bool = Field(default=True, description="false=只登记，等批量处理")


def _out(row: VideoAsset) -> Dict[str, Any]:
    data = store.video_fields(row)
    data["object_url"] = (library_api(f"videos/{row.id}/stream")
                          if row.object_key else None)
    return data


@router.post("")
async def create_video(payload: VideoCreate):
    """新增视频：同 (platform, content_id) 幂等；run=true 时立即下载转储。"""
    if not payload.content_id.strip() or not payload.source_url.strip():
        raise HTTPException(status_code=422, detail="content_id 与 source_url 不能为空")

    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        vid = await store.enqueue_video(
            session, platform=payload.platform or "xhs",
            content_id=payload.content_id.strip(),
            source_url=payload.source_url.strip(),
            story_id=payload.story_id, backup_urls=payload.backup_urls)
        await session.commit()
        if vid is None:
            raise HTTPException(status_code=422, detail="视频登记失败")
        row = (await session.execute(
            select(VideoAsset).where(VideoAsset.id == vid)
        )).scalar_one_or_none()
        if payload.run and row.status != "uploaded":
            try:
                settings, _ = await store.video_settings(session)
            except Exception as e:
                raise HTTPException(status_code=503, detail=f"S3 存储不可用：{e}") from e
            result = await store.archive_video(session, row, settings)
            if not result.get("ok"):
                raise HTTPException(
                    status_code=502, detail=result.get("error") or "视频转储失败")
        return _out(row)
    finally:
        await session.close()
        await engine.dispose()


@router.get("")
async def list_videos(
    status: Optional[str] = None,
    platform: Optional[str] = None,
    content_id: Optional[str] = None,
    story_id: Optional[int] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
):
    """视频列表（管理页 / 详情页按 content_id 查一条）。"""
    engine, session = await _session()
    if session is None:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}
    try:
        q = select(VideoAsset)
        if status:
            q = q.where(VideoAsset.status == status)
        if platform:
            q = q.where(VideoAsset.platform == platform)
        if content_id:
            q = q.where(VideoAsset.content_id == content_id)
        if story_id:
            q = q.where(VideoAsset.story_id == story_id)
        total = (await session.execute(
            select(func.count()).select_from(q.subquery()))).scalar() or 0
        rows = (await session.execute(
            q.order_by(VideoAsset.id.desc())
            .offset((page - 1) * page_size).limit(page_size)
        )).scalars().all()
        # 详情页打开即懒触发批处理：抓取侧只登记 pending，这里负责把队列跑起来，
        # 无需额外的常驻 worker，也不会让 GET 请求被大文件下载拖住。
        pending = (await session.execute(
            select(func.count()).select_from(VideoAsset)
            .where(VideoAsset.status == "pending")
        )).scalar() or 0
        if pending:
            _schedule_batch()
        return {"items": [_out(r) for r in rows], "total": total,
                "page": page, "page_size": page_size}
    finally:
        await session.close()
        await engine.dispose()


@router.post("/run")
async def run_batch(limit: int = Query(5, ge=1, le=50)):
    """立即跑一批待转储视频（后台，避免请求被大文件下载拖住）。"""
    started = _schedule_batch(limit=limit)
    return {"started": started,
            **({} if started else {"message": "批处理已在运行"})}


@router.get("/{video_id}")
async def get_video(video_id: int):
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        row = (await session.execute(
            select(VideoAsset).where(VideoAsset.id == video_id)
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="视频记录不存在")
        return _out(row)
    finally:
        await session.close()
        await engine.dispose()


@router.delete("/{video_id}")
async def delete_video(video_id: int):
    """删除记录并清除 S3 对象；对象删除失败不阻断（与 delete_item 一致）。"""
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        result = await store.purge_video(session, video_id)
        if not result.get("success"):
            raise HTTPException(status_code=404, detail=result.get("error") or "删除失败")
        return result
    finally:
        await session.close()
        await engine.dispose()


@router.get("/{video_id}/stream")
async def stream_video(video_id: int, request: Request):
    """代理 S3 视频对象，支持 Range / 206 —— <video> 拖动进度条的必要条件。"""
    from tools.s3_client import call

    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        row = (await session.execute(
            select(VideoAsset).where(VideoAsset.id == video_id)
        )).scalar_one_or_none()
        if row is None or not row.object_key:
            raise HTTPException(status_code=404, detail="视频尚未转储")
        try:
            settings, _ = await store.video_settings(session)
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"S3 存储不可用：{e}") from e

        kwargs: Dict[str, Any] = {"Bucket": settings.bucket, "Key": row.object_key}
        range_header = request.headers.get("range")
        if range_header:
            kwargs["Range"] = range_header
        try:
            resp = await call(settings, "get_object", **kwargs)
        except Exception as e:
            code = ""
            try:
                code = str(e.response.get("Error", {}).get("Code", ""))
            except Exception:
                pass
            if code in ("404", "NoSuchKey"):
                raise HTTPException(status_code=404, detail="视频对象不存在")
            raise HTTPException(status_code=502, detail=f"S3 读取失败: {e}") from e

        body = resp.get("Body")
        if body is None:
            raise HTTPException(status_code=502, detail="S3 返回空对象体")
        headers = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=3600",
        }
        if resp.get("ContentRange"):
            headers["Content-Range"] = str(resp["ContentRange"])
        if resp.get("ContentLength") is not None:
            headers["Content-Length"] = str(resp["ContentLength"])
        status = 206 if range_header and resp.get("ContentRange") else 200
        content_type = resp.get("ContentType") or "video/mp4"

        async def _chunks():
            while True:
                chunk = await asyncio.to_thread(body.read, 64 * 1024)
                if not chunk:
                    break
                yield chunk

        return StreamingResponse(_chunks(), status_code=status,
                                 headers=headers, media_type=content_type)
    finally:
        await session.close()
        await engine.dispose()
