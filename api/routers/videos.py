# -*- coding: utf-8 -*-
"""视频转储管理 API（S3 `videos` 桶）。

风格与 api/routers/s3_backup.py 保持一致：_session() 取库、HTTPException 报错、
返回扁平 dict、列表统一 {items,total,page,page_size}。

    POST   /videos                 新增（登记 + 默认立即转储，返回原始文件大小）
    GET    /videos                 查询列表（可按 story/content/status 过滤）
    GET    /videos/{id}            查询详情
    DELETE /videos/{id}            删除记录（连同 S3 对象）
    POST   /videos/run             批量处理待转储记录（后台）
    GET    /videos/{id}/stream     播放（默认 307 → S3 预签名 URL，Range/206 由 S3 直出）

所有返回都带 ``original_size_bytes``（源片真实大小，转码前）与
``stored_size_bytes``（S3 对象大小），页面展示用前者。
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, StreamingResponse
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
    # 前端「执行记录」要用：排队/处理中显示已耗时，完成显示总耗时
    elapsed = None
    try:
        from datetime import datetime, timezone

        created = row.created_at
        if created is not None:
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            end = row.uploaded_at or datetime.now(timezone.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            elapsed = max(0.0, (end - created).total_seconds())
    except Exception:
        elapsed = None
    data["elapsed_sec"] = elapsed
    data["running"] = row.status in ("uploading",)
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
                settings, prefix = await store.video_target(session)
            except Exception as e:
                raise HTTPException(status_code=503, detail=f"S3 存储不可用：{e}") from e
            result = await store.archive_video(session, row, settings, prefix)
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


@router.get("/{video_id}/logs")
async def video_logs(video_id: int, limit: int = Query(200, ge=1, le=1000)):
    """某条转储的过程日志（下载/归一化/上传/完成/失败/终止）。"""
    return {"items": store.read_events(video_id, limit=limit)}


@router.post("/{video_id}/cancel")
async def cancel_video(video_id: int):
    """终止一条转储。排队中的直接取消；处理中的会中断协程。"""
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        result = await store.cancel_video(session, video_id)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error") or "终止失败")
        return result
    finally:
        await session.close()
        await engine.dispose()


@router.post("/cancel-pending")
async def cancel_pending():
    """把所有排队中/处理中的转储全部终止。"""
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        return await store.cancel_pending(session)
    finally:
        await session.close()
        await engine.dispose()


@router.get("/diag/s3")
async def diag_s3():
    """探测对象存储连通性（内网 / 公网 endpoint 各测一次 head_bucket）。

    排查「上传卡住」的入口：内网 endpoint 若在此处就报错/超时，说明容器
    根本连不上，上传每次都要先失败再回退公网。放在 `/{video_id}` 之前定义，
    否则会被当成 video_id 解析。
    """
    import time as _time

    from tools.s3_client import call as s3_call

    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        try:
            public, _ = await store.video_settings(session)
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"S3 配置不可用：{e}") from e
        internal = public
        internal_cfg_error = None
        try:
            internal, _ = await store.video_settings(session, internal=True)
        except Exception as e:
            internal_cfg_error = str(e)[:200]

        probes = []
        for name, st in (("internal", internal), ("public", public)):
            item = {"name": name, "endpoint": st.endpoint,
                    "fast_fail": bool(getattr(st, "fast_fail", False))}
            t0 = _time.monotonic()
            try:
                await s3_call(st, "head_bucket", Bucket=st.bucket)
                item["ok"] = True
            except Exception as e:
                item["ok"] = False
                item["error"] = f"{type(e).__name__}: {str(e)[:160]}"
            item["ms"] = int((_time.monotonic() - t0) * 1000)
            probes.append(item)

        result = {
            "bucket": public.bucket,
            "same_endpoint": internal.endpoint == public.endpoint,
            "configured_internal": store.internal_endpoint() or "",
            "probes": probes,
        }
        if internal_cfg_error:
            result["internal_config_error"] = internal_cfg_error
        return result
    finally:
        await session.close()
        await engine.dispose()


@router.post("/run")
async def run_batch(limit: int = Query(5, ge=1, le=50)):
    """立即跑一批待转储视频（后台，避免请求被大文件下载拖住）。"""
    started = _schedule_batch(limit=limit)
    return {"started": started,
            **({} if started else {"message": "批处理已在运行"})}


class VideoBackfill(BaseModel):
    story_id: int = Field(description="要补建视频转储的内容（story）ID")


@router.post("/backfill")
async def backfill_video(payload: VideoBackfill):
    """按内容补建转储：用抓取时已存的直链（缺失时免登录重拉详情解析）。

    存量视频帖抓取时没开转储开关，库里没有视频记录；这个入口让详情页
    可以就地补齐，不必重新抓一遍帖子。
    """
    engine, session = await _session()
    if session is None:
        raise HTTPException(status_code=503, detail="配置库不可用")
    try:
        result = await store.backfill_story(session, payload.story_id)
        if not result.get("ok"):
            raise HTTPException(status_code=422, detail=result.get("error") or "登记失败")
        row = (await session.execute(
            select(VideoAsset).where(VideoAsset.id == result["id"])
        )).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=500, detail="登记后读取记录失败")
        _schedule_batch()
        return _out(row)
    finally:
        await session.close()
        await engine.dispose()


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


def _is_private_host(host: str) -> bool:
    """Host 是否是私网/回环地址（内网用户就是这样访问的）。"""
    import ipaddress

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(ip.is_private or ip.is_loopback)


def _browser_reachable(host: str) -> bool:
    """浏览器能否解析这个主机名。

    IP 一定可以；含点的域名交给局域网 DNS；**容器名（无点）只有 docker
    内部能解析**，给浏览器就是个打不开的地址（曾导致内网视频无声失败）。
    """
    import ipaddress

    host = (host or "").strip().lower()
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return "." in host


def presign_settings_for(request: Request, public, internal):
    """按访问来源挑预签名用的 S3 endpoint。

    内网用户拿到的如果是公网域名，浏览器会去连自家公网 IP（NAT 回流），
    多数家用路由/群晖不支持 hairpin，表现为「外网能播、内网播不了」；
    反过来外网用户拿到内网地址也打不开。所以跟着 Host 走：
      Host 是私网 IP（或与内网 endpoint 同主机）→ 内网地址，其余 → 公网。
    可用 `S3_PRESIGN_FORCE=internal|public` 强制。
    """
    from urllib.parse import urlparse

    force = os.getenv("S3_PRESIGN_FORCE", "auto").strip().lower()
    if force in ("internal", "intranet", "lan"):
        return internal
    if force in ("public", "wan", "external"):
        return public
    if internal.endpoint == public.endpoint:
        return public
    host = (request.headers.get("host") or "").split(":")[0].strip().lower()
    if not host:
        return public
    internal_host = (urlparse(internal.endpoint).hostname or "").lower()
    if not _browser_reachable(internal_host):
        # 容器名只对容器有意义，宁可回退公网也别给浏览器一个解析不了的地址
        logging.getLogger(__name__).warning(
            "[videos] S3_INTERNAL_ENDPOINT=%s 是容器名，浏览器无法解析，"
            "播放改用公网地址；需要内网直连请改成局域网 IP（如 "
            "http://192.168.10.38:3900）", internal.endpoint)
        return public
    if host == internal_host or _is_private_host(host):
        return internal
    return public


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
            settings, _ = await store.video_settings(session, bucket=row.bucket)
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"S3 存储不可用：{e}") from e

        # 默认让浏览器直连 S3：部分部署里 library 容器回源对象存储会被
        # NAT 回流/大包限制卡住（实测 1KB 秒回、64KB 起 0 字节，播放器显示 0kb/s）。
        # 307 保留 GET 与 Range 头，浏览器跟随重定向后由 S3 直接回 206。
        # 设 VIDEO_STREAM_MODE=proxy 可退回容器内字节代理（签名不可用时自动兜底）。
        if os.getenv("VIDEO_STREAM_MODE", "presign").strip().lower() != "proxy":
            try:
                from tools.s3_client import get_client

                # 预签名地址要跟访问来源匹配：内网访问用内网 S3 地址，
                # 外网访问用公网地址（否则内网走 NAT 回流，浏览器连不上）
                sign_settings = settings
                try:
                    internal_settings, _ = await store.video_settings(
                        session, bucket=row.bucket, internal=True)
                    sign_settings = presign_settings_for(
                        request, settings, internal_settings)
                except Exception as e:
                    logging.getLogger(__name__).debug(
                        "[videos] 内网 endpoint 不可用，按公网签名：%s", e)

                client = await get_client(sign_settings)
                play_url = await asyncio.to_thread(
                    lambda: client.generate_presigned_url(
                        "get_object",
                        Params={"Bucket": sign_settings.bucket, "Key": row.object_key},
                        ExpiresIn=3600,
                    )
                )
                # 不缓存重定向，否则浏览器可能复用已过期的签名 URL
                return RedirectResponse(
                    url=play_url, status_code=307,
                    headers={"Cache-Control": "no-store"})
            except Exception as e:
                logging.getLogger(__name__).warning(
                    "[videos] 预签名失败，回退容器内代理：%s", e)

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
