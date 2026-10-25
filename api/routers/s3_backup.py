# -*- coding: utf-8 -*-
"""S3 备份管理 API（P4）。

配置侧（s3_config）：
    GET    /s3-backup/configs            列表（Secret 掩码）
    POST   /s3-backup/configs            新增
    PUT    /s3-backup/configs/{id}       修改（secret 留空=不变）
    DELETE /s3-backup/configs/{id}       删除
    POST   /s3-backup/configs/{id}/toggle {enabled} 启用状态管理
    POST   /s3-backup/configs/{id}/test  连通性测试（落库探测结果）
    POST   /s3-backup/configs/test       保存前试用（不落库）

备份侧（media_backup）：
    GET    /s3-backup/stats              总览
    POST   /s3-backup/run                立即跑一批（后台）
    POST   /s3-backup/retry              重试失败项 {ids?}
    POST   /s3-backup/enqueue-missing    历史媒体入队
    GET    /s3-backup/items              备份记录（筛选/分页）
    POST   /s3-backup/items/{id}/retry   单条重试
    DELETE /s3-backup/items/{id}          删除记录（连同 S3 对象）
    POST   /s3-backup/stories/{id}/backup   把该帖媒体全部入队并立即执行
    GET    /s3-backup/stories/{id}/status   该帖备份进度

访问侧：
    GET    /s3-backup/object/{key}       代理 S3 对象（访问链第三级）
"""
import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select

from database.models_v2 import MediaAttachment, MediaBackup, S3Config
from .story import _session

router = APIRouter(prefix="/s3-backup", tags=["s3-backup"])

#: 手动触发的批处理任务（同一时刻只跑一个）
_RUN_TASK: Optional[asyncio.Task] = None


# ---------------------------------------------------------------------------
# 配置 CRUD
# ---------------------------------------------------------------------------

def _mask_secret(plain: str) -> str:
    if not plain:
        return ""
    if len(plain) <= 8:
        return plain[:2] + "****"
    return f"{plain[:4]}****{plain[-4:]}"


def _config_out(c: S3Config, secret_plain: str = "") -> Dict[str, Any]:
    return {
        "id": c.id,
        "name": c.name,
        "endpoint": c.endpoint,
        "access_key_id": c.access_key_id,
        "secret_masked": _mask_secret(secret_plain),
        "has_secret": bool(c.secret_cipher),
        "region": c.region,
        "bucket": c.bucket,
        "addressing_style": c.addressing_style or "path",
        "enabled": bool(c.enabled),
        "last_check_at": c.last_check_at.isoformat() if c.last_check_at else None,
        "last_check_ok": c.last_check_ok,
        "last_error": c.last_error,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


@router.get("/configs")
async def list_configs():
    """配置列表（Secret 只回掩码，明文永不出现在响应里）。"""
    engine, session = await _session()
    if session is None:
        return {"items": []}
    try:
        from account.crypto import decrypt_text

        rows = (await session.execute(
            select(S3Config).order_by(S3Config.id))).scalars().all()
        return {"items": [_config_out(c, decrypt_text(c.secret_cipher or ""))
                          for c in rows]}
    finally:
        await session.close()
        await engine.dispose()


@router.post("/configs")
async def create_config(payload: Dict[str, Any] = Body(default={})):
    """新增配置。"""
    name = str(payload.get("name") or "").strip()
    endpoint = str(payload.get("endpoint") or "").strip()
    akid = str(payload.get("access_key_id") or "").strip()
    secret = str(payload.get("secret_key") or "").strip()
    if not name or not endpoint or not akid or not secret:
        raise HTTPException(status_code=400,
                            detail="name / endpoint / access_key_id / secret_key 必填")
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        dup = (await session.execute(
            select(S3Config).where(S3Config.name == name))).scalar_one_or_none()
        if dup is not None:
            raise HTTPException(status_code=409, detail=f"配置名 {name} 已存在")
        from account.crypto import encrypt_text

        row = S3Config(
            name=name, endpoint=endpoint, access_key_id=akid,
            secret_cipher=encrypt_text(secret),
            region=str(payload.get("region") or "garage"),
            bucket=str(payload.get("bucket") or "photos"),
            addressing_style=str(payload.get("addressing_style") or "path"),
            enabled=bool(payload.get("enabled", True)),
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        from tools.media_backup import invalidate_config_cache

        invalidate_config_cache()
        return _config_out(row, secret)
    finally:
        await session.close()
        await engine.dispose()


@router.put("/configs/{config_id}")
async def update_config(config_id: int, payload: Dict[str, Any] = Body(default={})):
    """修改配置；secret_key 传空串表示不变。"""
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        row = (await session.execute(
            select(S3Config).where(S3Config.id == config_id))).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="配置不存在")
        if "name" in payload:
            new_name = str(payload.get("name") or "").strip()
            if new_name and new_name != row.name:
                dup = (await session.execute(
                    select(S3Config).where(S3Config.name == new_name,
                                           S3Config.id != config_id)
                )).scalar_one_or_none()
                if dup is not None:
                    raise HTTPException(status_code=409,
                                        detail=f"配置名 {new_name} 已存在")
                row.name = new_name
        for field in ("endpoint", "access_key_id", "region", "bucket",
                      "addressing_style"):
            if field in payload and payload.get(field):
                setattr(row, field, str(payload.get(field)).strip())
        secret = str(payload.get("secret_key") or "").strip()
        plain = ""
        if secret:
            from account.crypto import encrypt_text, decrypt_text

            row.secret_cipher = encrypt_text(secret)
            plain = secret
        else:
            from account.crypto import decrypt_text

            plain = decrypt_text(row.secret_cipher or "")
        if "enabled" in payload:
            row.enabled = bool(payload.get("enabled"))
        await session.commit()
        await session.refresh(row)
        from tools.media_backup import invalidate_config_cache

        invalidate_config_cache()
        return _config_out(row, plain)
    finally:
        await session.close()
        await engine.dispose()


@router.delete("/configs/{config_id}")
async def delete_config(config_id: int):
    """删除配置（不删除已上传的 S3 对象与备份记录）。"""
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        row = (await session.execute(
            select(S3Config).where(S3Config.id == config_id))).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="配置不存在")
        await session.delete(row)
        await session.commit()
        from tools.media_backup import invalidate_config_cache

        invalidate_config_cache()
        return {"success": True, "deleted_id": config_id}
    finally:
        await session.close()
        await engine.dispose()


@router.post("/configs/{config_id}/toggle")
async def toggle_config(config_id: int, payload: Dict[str, Any] = Body(default={})):
    """启用 / 停用。启用后自动把历史媒体补入备份队列。"""
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        row = (await session.execute(
            select(S3Config).where(S3Config.id == config_id))).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="配置不存在")
        row.enabled = bool(payload.get("enabled", not row.enabled))
        await session.commit()
        from tools.media_backup import invalidate_config_cache

        invalidate_config_cache()
        if row.enabled:
            # 启用即开始补历史：后台入队 + 立即跑一批
            asyncio.create_task(_enqueue_then_run())
        return {"success": True, "id": config_id, "enabled": row.enabled}
    finally:
        await session.close()
        await engine.dispose()


async def _enqueue_then_run() -> None:
    """历史媒体入队后立刻跑一批（启用配置 / 手动入队共用）。"""
    try:
        from tools.media_backup import enqueue_missing, run_backup_batch

        await enqueue_missing(limit=500)
        await run_backup_batch()
    except Exception as e:
        print(f"[s3-backup] 入队/执行失败: {e}")


@router.post("/configs/{config_id}/test")
async def test_saved_config(config_id: int):
    """测试已保存配置的连通性，结果落库（last_check_ok / last_error）。"""
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        row = (await session.execute(
            select(S3Config).where(S3Config.id == config_id))).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="配置不存在")
        result = await _test_with_row(row)
        from datetime import datetime, timezone

        row.last_check_at = datetime.now(timezone.utc)
        row.last_check_ok = bool(result.get("ok"))
        row.last_error = None if result.get("ok") else str(result.get("error"))[:500]
        await session.commit()
        return result
    finally:
        await session.close()
        await engine.dispose()


@router.post("/configs/test")
async def test_unsaved_config(payload: Dict[str, Any] = Body(default={})):
    """保存前试用：用表单里的值直接试连，不落库。"""
    from account.crypto import decrypt_text
    from tools.s3_client import S3Settings, test_connection

    secret = str(payload.get("secret_key") or "").strip()
    config_id = payload.get("config_id")
    if not secret and config_id:
        engine, session = await _session()
        try:
            row = (await session.execute(
                select(S3Config).where(S3Config.id == int(config_id))
            )).scalar_one_or_none()
            if row is not None:
                secret = decrypt_text(row.secret_cipher or "")
        finally:
            await session.close()
            await engine.dispose()
    if not secret:
        raise HTTPException(status_code=400, detail="secret_key 必填（或带 config_id 取已存值）")
    settings = S3Settings(
        endpoint=str(payload.get("endpoint") or "").strip(),
        access_key_id=str(payload.get("access_key_id") or "").strip(),
        secret_access_key=secret,
        region=str(payload.get("region") or "garage"),
        bucket=str(payload.get("bucket") or "photos"),
        addressing_style=str(payload.get("addressing_style") or "path"),
    )
    if not settings.endpoint or not settings.access_key_id:
        raise HTTPException(status_code=400, detail="endpoint / access_key_id 必填")
    return await test_connection(settings)


async def _test_with_row(row: S3Config) -> Dict[str, Any]:
    from account.crypto import decrypt_text
    from tools.s3_client import S3Settings, test_connection

    return await test_connection(S3Settings(
        endpoint=row.endpoint or "",
        access_key_id=row.access_key_id or "",
        secret_access_key=decrypt_text(row.secret_cipher or ""),
        region=row.region or "garage",
        bucket=row.bucket or "photos",
        addressing_style=row.addressing_style or "path",
    ))


# ---------------------------------------------------------------------------
# 备份任务
# ---------------------------------------------------------------------------

@router.get("/stats")
async def stats():
    """备份总览（各状态条数 / 已备份体积 / 最近错误）。"""
    from tools.media_backup import backup_stats

    return await backup_stats()


@router.post("/run")
async def run_now(limit: int = Query(20, ge=1, le=200)):
    """立即执行一批备份（后台跑，锁互斥防重入）。"""
    global _RUN_TASK
    if _RUN_TASK is not None and not _RUN_TASK.done():
        return {"started": False, "note": "已有备份任务在运行"}

    async def _bg():
        from tools.media_backup import run_backup_batch

        await run_backup_batch(limit=limit)

    _RUN_TASK = asyncio.create_task(_bg())
    return {"started": True, "limit": limit}


@router.post("/retry")
async def retry(payload: Dict[str, Any] = Body(default={})):
    """重试失败项：{ids: [..]} 指定单条，不带 ids 则全部 failed 重置。"""
    from tools.media_backup import retry_failed, run_backup_batch

    ids = payload.get("ids") if isinstance(payload, dict) else None
    if ids is not None and not isinstance(ids, list):
        raise HTTPException(status_code=400, detail="ids 必须是数组")
    n = await retry_failed(ids)
    if n:
        asyncio.create_task(run_backup_batch())
    return {"reset": n}


@router.post("/enqueue-missing")
async def enqueue_missing_api():
    """历史媒体入队（把没有备份记录的媒体补成 pending），后台执行。"""
    asyncio.create_task(_enqueue_then_run())
    return {"started": True}


@router.get("/items")
async def list_items(
    status: Optional[str] = None,
    platform: Optional[str] = None,
    story_id: Optional[int] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
):
    """备份记录列表（管理页：待备份 / 失败明细 / 重试入口）。"""
    engine, session = await _session()
    if session is None:
        return {"items": [], "total": 0}
    try:
        from sqlalchemy import func

        q = select(MediaBackup)
        if status:
            q = q.where(MediaBackup.status == status)
        if platform:
            q = q.where(MediaBackup.platform == platform)
        if story_id:
            q = q.where(MediaBackup.story_id == story_id)
        total = (await session.execute(
            select(func.count()).select_from(q.subquery()))).scalar() or 0
        rows = (await session.execute(
            q.order_by(MediaBackup.id.desc())
            .offset((page - 1) * page_size).limit(page_size)
        )).scalars().all()
        return {
            "items": [_item_out(r) for r in rows],
            "total": total, "page": page, "page_size": page_size,
        }
    finally:
        await session.close()
        await engine.dispose()


def _item_out(r: MediaBackup) -> Dict[str, Any]:
    return {
        "id": r.id,
        "media_id": r.media_id,
        "story_id": r.story_id,
        "platform": r.platform,
        "content_id": r.content_id,
        "media_type": r.media_type,
        "object_key": r.object_key,
        "object_url": f"/api/s3-backup/object/{r.object_key}",
        "source_url": r.source_url,
        "mime_type": r.mime_type,
        "size_bytes": r.size_bytes,
        "status": r.status,
        "retry_count": r.retry_count,
        "max_retries": r.max_retries,
        "next_retry_at": r.next_retry_at.isoformat() if r.next_retry_at else None,
        "last_error": r.last_error,
        "uploaded_at": r.uploaded_at.isoformat() if r.uploaded_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.post("/items/{item_id}/retry")
async def retry_item(item_id: int):
    """单条重试：重置后立即执行。"""
    from tools.media_backup import retry_failed, run_backup_batch

    n = await retry_failed([item_id])
    if n:
        asyncio.create_task(run_backup_batch())
    return {"reset": n}


@router.post("/items/{item_id}/purge")
async def purge_item(item_id: int):
    """手动清理单条备份：删 S3 对象 + 删记录。

    主要用于 orphaned（删帖时 S3 删除失败）的兜底；
    对象仍被其它记录引用时只删记录、保留对象。
    """
    from tools.media_backup import purge_backup_item

    result = await purge_backup_item(item_id)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error") or "清理失败")
    return result


@router.delete("/items/{item_id}")
async def delete_item(item_id: int):
    """删除单条备份记录：连同 S3 对象一起清除。

    对象仍被其它记录引用时只删记录、保留对象；S3 删除失败不阻断
    （记录仍被删除，对象成为桶内孤儿，可重新上传覆盖同键）。
    """
    from tools.media_backup import purge_backup_item

    result = await purge_backup_item(item_id)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error") or "删除失败")
    return result


@router.post("/purge-orphaned")
async def purge_orphaned():
    """清理全部孤立备份（删帖时 S3 删除失败转来的那批）。"""
    from tools.media_backup import retry_orphaned_purges

    return await retry_orphaned_purges(limit=500)


@router.post("/stories/{story_id}/backup")
async def backup_story(story_id: int, run: bool = True):
    """把该帖全部媒体加入备份队列；run=true 时立即执行一批。"""
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        from sqlalchemy import or_

        from database.models_v2 import SourceSite, StoryChapter
        from tools.media_backup import enqueue_backup, run_backup_batch

        chapters = (await session.execute(
            select(StoryChapter).where(StoryChapter.story_id == story_id)
        )).scalars().all()
        chapter_ids = [c.id for c in chapters]
        site_ids = {c.source_site_id for c in chapters if c.source_site_id}
        site_code: Dict[int, str] = {}
        if site_ids:
            site_code = {s.id: s.code for s in (await session.execute(
                select(SourceSite).where(SourceSite.id.in_(site_ids))
            )).scalars().all()}
        conds = [MediaAttachment.target_type == "story",
                 MediaAttachment.target_id == story_id]
        if chapter_ids:
            conds.append(MediaAttachment.target_type == "chapter")
            conds.append(MediaAttachment.target_id.in_(chapter_ids))
        media = (await session.execute(
            select(MediaAttachment).where(or_(*conds))
        )).scalars().all()
        media_ids = [m.id for m in media]
        # 已入队的不重复计数
        already = {r[0] for r in (await session.execute(
            select(MediaBackup.media_id).where(MediaBackup.media_id.in_(media_ids))
        )).all()} if media_ids else set()
        ch_by_id = {c.id: c for c in chapters}
        queued = 0
        for m in media:
            if m.target_type == "chapter" and m.target_id in ch_by_id:
                c = ch_by_id[m.target_id]
                platform = site_code.get(c.source_site_id) or "unknown"
                content_id = str(c.source_content_id or "")
            else:
                platform, content_id = "story", str(story_id)
            row = await enqueue_backup(session, m, platform, content_id, story_id)
            if row is not None and m.id not in already:
                queued += 1
        await session.commit()
        if run and queued:
            asyncio.create_task(run_backup_batch())
        return {"queued": queued, "media_total": len(media)}
    finally:
        await session.close()
        await engine.dispose()


@router.get("/stories/{story_id}/status")
async def story_status(story_id: int):
    """单帖备份进度（详情页徽标 / 「备份此帖」按钮状态）。"""
    engine, session = await _session()
    if session is None:
        return {"total": 0, "uploaded": 0, "complete": False}
    try:
        from tools.media_backup import story_backup_detail

        return await story_backup_detail(session, story_id)
    finally:
        await session.close()
        await engine.dispose()


# ---------------------------------------------------------------------------
# S3 对象代理（访问链第三级）
# ---------------------------------------------------------------------------

@router.get("/object/{key:path}")
async def serve_backup_object(key: str):
    """代理 S3 备份对象。

    本地缓存没了、平台外链挂了之后走这里；帖子删除不影响（对象与
    备份记录都不随 story 级联删除）。大文件边下边吐，不整包读内存。
    """
    from tools.media_backup import active_settings
    from tools.s3_client import call

    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        cfg = await active_settings(session)
        if cfg is None:
            raise HTTPException(status_code=503, detail="未配置可用的 S3 存储")
        settings = cfg["settings"]
        try:
            resp = await call(settings, "get_object",
                              Bucket=settings.bucket, Key=key)
        except Exception as e:
            code = ""
            try:
                code = str(e.response.get("Error", {}).get("Code", ""))
            except Exception:
                pass
            if code in ("404", "NoSuchKey"):
                raise HTTPException(status_code=404, detail="备份对象不存在")
            raise HTTPException(status_code=502, detail=f"S3 读取失败: {e}")

        body = resp.get("Body")
        ctype = resp.get("ContentType") or "application/octet-stream"
        if body is None:
            raise HTTPException(status_code=404, detail="备份对象为空")

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

        return StreamingResponse(
            _iter(), media_type=ctype,
            headers={"Cache-Control": "public, max-age=86400",
                     "X-Backup-Source": "s3"})
    finally:
        await session.close()
        await engine.dispose()
