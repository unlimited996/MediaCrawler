# -*- coding: utf-8 -*-
"""小红书视频贴 → S3 `videos` 桶的转储链路。

与图片备份（tools/media_backup.py）刻意分开：图片那套内容寻址去重 / 表情包
收敛逻辑很复杂，视频不需要也不该去动它，保证现有功能零影响。

流程：
  抓取侧登记 pending（只写一行，不阻塞抓取）→ 后台/接口批量处理：
  流式下载源片 → ffprobe → ffmpeg 统一成 H.264/AAC progressive MP4（moov 前置）
  → 上传到 videos 桶（ContentType=video/mp4）→ head_object 校验大小 → 写回记录。

字段口径（页面展示以此为准）：
  original_size_bytes = **下载完成时源片的真实字节数**（转码前）
  stored_size_bytes   = 最终 S3 对象字节数（转码后）
两者不能混用；HLS 源没有单一原始文件，用转码产物大小并标 ext.source_kind=hls。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import select

from database.models_v2 import VideoAsset
from tools import utils
from tools.v2_sync import _v2_session

#: 桶名只换 Bucket，endpoint / 密钥 / region 完全复用现有 S3Config
VIDEO_BUCKET = os.getenv("S3_VIDEO_BUCKET", "videos")
MAX_VIDEO_BYTES = int(os.getenv("S3_VIDEO_MAX_BYTES", str(500 * 1024 * 1024)))
DOWNLOAD_TIMEOUT_SEC = float(os.getenv("S3_VIDEO_DOWNLOAD_TIMEOUT", "600"))
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_BAD_TYPES = ("text/html", "application/json", "text/plain")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def video_settings(session):
    """复用当前启用的 S3 配置，只把 Bucket 换成 videos（凭据/endpoint 不变）。"""
    from tools.media_backup import active_settings
    from tools.s3_client import S3Settings

    cfg = await active_settings(session)
    if cfg is None:
        raise RuntimeError("未配置可用的 S3 存储")
    base: S3Settings = cfg["settings"]
    return replace(base, bucket=VIDEO_BUCKET), cfg.get("config_id")


async def ensure_bucket(settings) -> None:
    from tools.s3_client import call

    try:
        await call(settings, "head_bucket", Bucket=settings.bucket)
        return
    except Exception:
        pass
    try:
        await call(settings, "create_bucket", Bucket=settings.bucket)
        utils.logger.info(f"[video_store] 已创建桶 {settings.bucket}")
    except Exception as e:
        raise RuntimeError(f"桶 {settings.bucket} 不存在且创建失败：{e}") from e


async def enqueue_video(session, *, platform: str, content_id: str,
                        source_url: str, story_id: Optional[int] = None,
                        backup_urls: Optional[List[str]] = None) -> Optional[int]:
    """登记一条待转储视频。同 (platform, content_id) 幂等：已存在只补候选直链。

    抓取侧在「开关打开」时调用，只写一行、不发任何视频请求，不阻塞抓取。
    """
    content_id, source_url = str(content_id or ""), str(source_url or "").strip()
    if not content_id or not source_url:
        return None
    row = (await session.execute(
        select(VideoAsset).where(VideoAsset.platform == platform,
                                 VideoAsset.content_id == content_id)
    )).scalar_one_or_none()
    if row is None:
        row = VideoAsset(
            platform=platform, content_id=content_id, story_id=story_id,
            source_url=source_url, backup_urls=list(backup_urls or []),
            status="pending", bucket=VIDEO_BUCKET,
        )
        session.add(row)
        await session.flush()
        return row.id
    if story_id and not row.story_id:
        row.story_id = story_id
    urls = list(row.backup_urls or [])
    for u in [source_url, *(backup_urls or [])]:
        if u and u not in urls and u != row.source_url:
            urls.append(u)
    row.backup_urls = urls
    if row.status == "failed":
        row.status = "pending"
        row.last_error = None
        row.retry_count = 0
    await session.flush()
    return row.id


async def _fetch_to_file(client: httpx.AsyncClient, url: str, dst: Path) -> Dict[str, Any]:
    """流式下载到临时文件；返回原始大小 / Content-Type，绝不整包读进内存。"""
    headers = {"User-Agent": USER_AGENT, "Referer": "https://www.xiaohongshu.com/"}
    size = 0
    ctype = ""
    async with client.stream("GET", url, headers=headers,
                             timeout=DOWNLOAD_TIMEOUT_SEC,
                             follow_redirects=True) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"视频源返回 HTTP {resp.status_code}")
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        if any(bad in ctype for bad in _BAD_TYPES):
            raise RuntimeError(f"视频源返回了非媒体内容（{ctype}），可能是防盗链页")
        with open(dst, "wb") as fh:
            async for chunk in resp.aiter_bytes(chunk_size=256 * 1024):
                size += len(chunk)
                if size > MAX_VIDEO_BYTES:
                    raise RuntimeError(
                        f"源片超过 {MAX_VIDEO_BYTES // 1024 // 1024}MB 上限")
                fh.write(chunk)
    if size < 1024:
        raise RuntimeError(f"源片过小（{size} 字节），疑似错误页")
    return {"size": size, "content_type": ctype, "source_kind": "file"}


async def _transcode_hls_url(url: str, dst: Path) -> Dict[str, Any]:
    """HLS 播放列表：ffmpeg 直接读 URL 分片并输出 H.264/AAC MP4。"""
    from media_downloader.ffmpeg import ffmpeg_path

    executable = ffmpeg_path()
    if not executable:
        raise RuntimeError("HLS 源需要 ffmpeg，但镜像内不可用")
    proc = await asyncio.create_subprocess_exec(
        executable, "-hide_banner", "-loglevel", "error", "-y", "-i", url,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", "-f", "mp4", str(dst),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await asyncio.wait_for(proc.communicate(), timeout=DOWNLOAD_TIMEOUT_SEC)
    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size < 1024:
        detail = (err or b"").decode("utf-8", "replace")[-400:]
        raise RuntimeError(f"HLS 转码失败（{proc.returncode}）：{detail}")
    return {"size": dst.stat().st_size, "content_type": "application/vnd.apple.mpegurl",
            "source_kind": "hls"}


async def archive_video(session, row: VideoAsset, settings) -> Dict[str, Any]:
    """单条转储：下载 → 归一化 → 上传 → 校验。成功写回准确的原始大小。"""
    from media_downloader.ffmpeg import (
        probe_video, normalize_video,
    )
    from tools.s3_client import head_object, upload_file

    if row.status == "uploaded" and row.object_key:
        head = await head_object(settings, row.object_key)
        if head is not None:
            return {"ok": True, "skipped": True, "id": row.id}

    row.status = "uploading"
    await session.commit()

    tmp_dir = Path(tempfile.mkdtemp(prefix="mc_video_"))
    try:
        urls = [row.source_url, *(row.backup_urls or [])]
        last_error = ""
        for url in [u for u in urls if u]:
            src = tmp_dir / "source.bin"
            out = tmp_dir / "normalized.mp4"
            try:
                async with httpx.AsyncClient() as client:
                    # 先用 HEAD/首段判断 HLS，避免把播放列表存成假视频
                    is_hls = ".m3u8" in url.lower()
                    if is_hls:
                        info = await _transcode_hls_url(url, src)
                        shutil.move(str(src), str(out))
                    else:
                        info = await _fetch_to_file(client, url, src)
                        probe = await probe_video(src)
                        norm = await normalize_video(src, out, probe)
                        out = Path(norm["path"])
                        info["transcoded"] = bool(norm.get("transcoded"))
                        if not norm.get("available"):
                            utils.logger.warning(
                                f"[video_store] ffmpeg 不可用，降级为原样转储 {row.content_id}")
                            out = src
                        info.update(probe)
                if not out.exists() or out.stat().st_size < 1024:
                    raise RuntimeError("转储产物为空")
                with open(out, "rb") as fh:
                    head = fh.read(64)
                if b"ftyp" not in head:
                    raise RuntimeError("产物不是 MP4（缺少 ftyp box）")

                original_size = int(info["size"])
                stored_size = out.stat().st_size
                checksum = await asyncio.to_thread(_sha256_file, out)
                key = f"{row.platform}/videos/{row.content_id}/{checksum[:16]}.mp4"

                await ensure_bucket(settings)
                await upload_file(settings, str(out), key, "video/mp4")
                obj = await head_object(settings, key)
                if obj is None:
                    raise RuntimeError("上传后 head_object 查无此对象")
                if obj.get("ContentLength") is not None:
                    stored_size = int(obj["ContentLength"])

                row.object_key = key
                row.bucket = settings.bucket
                row.status = "uploaded"
                row.mime_type = "video/mp4"
                row.original_size_bytes = original_size
                row.stored_size_bytes = stored_size
                row.checksum = checksum
                row.codec_v = info.get("codec_v") or "h264"
                row.codec_a = info.get("codec_a") or "aac"
                row.duration_sec = info.get("duration_sec") or None
                row.uploaded_at = _now()
                row.last_error = None
                ext = dict(row.ext or {})
                ext["source_kind"] = info.get("source_kind", "file")
                ext["transcoded"] = bool(info.get("transcoded"))
                row.ext = ext
                await session.commit()
                utils.logger.info(
                    f"[video_store] 已转储 {row.content_id} → {settings.bucket}/{key} "
                    f"原始 {original_size}B / 存储 {stored_size}B")
                return {"ok": True, "id": row.id,
                        "original_size_bytes": original_size,
                        "stored_size_bytes": stored_size}
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                utils.logger.warning(
                    f"[video_store] 候选地址失败 {row.content_id}: {last_error}")
                for p in (src, out):
                    if p.exists():
                        p.unlink(missing_ok=True)
                continue
        raise RuntimeError(last_error or "没有可用的视频源地址")
    except Exception as e:
        row.status = "failed"
        row.retry_count = int(row.retry_count or 0) + 1
        row.last_error = str(e)[:1000]
        await session.commit()
        return {"ok": False, "id": row.id, "error": row.last_error}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


async def run_video_batch(limit: int = 5) -> Dict[str, Any]:
    """处理 pending / 失败待重试的记录；同一时刻只允许一批（进程内锁）。"""
    global _BATCH_LOCK
    if _BATCH_LOCK.locked():
        return {"running": True, "processed": 0}
    async with _BATCH_LOCK:
        async with _v2_session() as session:
            if session is None:
                return {"processed": 0, "error": "配置库不可用"}
            rows = (await session.execute(
                select(VideoAsset).where(
                    VideoAsset.status.in_(("pending", "failed")),
                    VideoAsset.retry_count < VideoAsset.max_retries,
                ).order_by(VideoAsset.id).limit(limit)
            )).scalars().all()
            if not rows:
                return {"processed": 0}
            try:
                settings, _ = await video_settings(session)
            except Exception as e:
                return {"processed": 0, "error": str(e)}
            ok = 0
            for row in rows:
                res = await archive_video(session, row, settings)
                ok += 1 if res.get("ok") else 0
            return {"processed": len(rows), "succeeded": ok}


_BATCH_LOCK = asyncio.Lock()


async def purge_video(session, video_id: int) -> Dict[str, Any]:
    """删除视频：先删 S3 对象，再删记录；对象删失败不阻断记录删除（同 delete_item 风格）。"""
    from tools.s3_client import delete_object

    row = (await session.execute(
        select(VideoAsset).where(VideoAsset.id == video_id)
    )).scalar_one_or_none()
    if row is None:
        return {"success": False, "error": "视频记录不存在"}
    deleted = False
    error = ""
    if row.object_key:
        try:
            settings, _ = await video_settings(session)
            await delete_object(settings, row.object_key)
            deleted = True
        except Exception as e:
            error = f"S3 删除失败：{e}"
    await session.delete(row)
    await session.commit()
    return {"success": True, "id": video_id, "object_deleted": deleted,
            "warning": error or None}


def video_fields(row: VideoAsset) -> Dict[str, Any]:
    """供接口复用的字段序列化（不含拼 URL，URL 由 router 按服务前缀生成）。"""
    return {
        "id": row.id,
        "story_id": row.story_id,
        "content_id": row.content_id,
        "platform": row.platform,
        "source_url": row.source_url,
        "object_key": row.object_key,
        "bucket": row.bucket,
        "status": row.status,
        "mime_type": row.mime_type,
        "original_size_bytes": row.original_size_bytes,
        "stored_size_bytes": row.stored_size_bytes,
        "codec_v": row.codec_v,
        "codec_a": row.codec_a,
        "duration_sec": float(row.duration_sec) if row.duration_sec is not None else None,
        "checksum": row.checksum,
        "retry_count": row.retry_count,
        "last_error": row.last_error,
        "uploaded_at": row.uploaded_at.isoformat() if row.uploaded_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
