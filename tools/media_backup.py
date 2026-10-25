# -*- coding: utf-8 -*-
"""媒体自动备份服务（P4）：把帖子关联的图片/视频推到 S3 兼容存储。

访问链（与 API 层约定，顺序不可变）：
    1. 本地缓存   /api/data/raw/...（已下载到 DATA_DIR 的文件）
    2. 平台远程    source_url（CDN 防盗链/签名过期会裂图）
    3. S3 备份    /api/s3-backup/object/{object_key}

对象键保留本地目录语义：``{platform}/{media_type}/{content_id}/{filename}``，
即按平台、文件类型分文件夹，帖子删除后仍可按 key 取回。

可靠性：
- 每张媒体一行 media_backup（uq media_id 幂等），pending → uploaded / failed
- 失败指数退避（60s × 2^n，最多 max_retries 次），耗尽转 failed 待手动重试
- 本地文件没了自动从 source_url 直取（httpx，带平台 Referer）
- 上传后 head_object 校验大小，不一致按失败处理
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select

from database.models_v2 import MediaAttachment, MediaBackup, S3Config

#: 启动时种子的默认配置（用户提供的 garage 实例）
SEED_S3_CONFIG: Dict[str, str] = {
    "name": "garage",
    "endpoint": "http://www.joyeuse.cn:3900",
    "access_key_id": "GK563d29829d5df54f3b0980b1",
    "secret_key": "7662debca53fa26135b49c172ed78029b7857f742287dd3bb37aa825dea76dbd",
    "region": "garage",
    "bucket": "photos",
    "addressing_style": "path",
}

#: 远端直取的体积上限（视频偏大，给 300MB）
_MAX_REMOTE_BYTES = 300 * 1024 * 1024
#: 单批处理数量 / 批内并发
_BATCH_LIMIT = 20
_BATCH_CONCURRENCY = 4
#: 退避基数（秒）：60 × 2^retry_count
_RETRY_BACKOFF_SEC = 60

#: 配置缓存（10s TTL）：v2_sync 每个媒体都会问一次「备份开着吗」
_CFG_CACHE: Dict[str, Any] = {"at": 0.0, "settings": None, "config_id": None}
#: 同一时刻只允许一个批处理跑（定时循环与手动触发互斥）
_RUN_LOCK = asyncio.Lock()


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

async def active_settings(session, force: bool = False) -> Optional[Dict[str, Any]]:
    """当前生效的 S3 连接参数（enabled 第一条，10s TTL 缓存）。

    返回 {"config_id": int, "settings": S3Settings, "name": str}；无启用配置返回 None。
    """
    global _CFG_CACHE
    now = time.monotonic()
    if not force and _CFG_CACHE["settings"] is not None and now - _CFG_CACHE["at"] < 10:
        return {"config_id": _CFG_CACHE["config_id"],
                "settings": _CFG_CACHE["settings"],
                "name": _CFG_CACHE.get("name")}
    row = (await session.execute(
        select(S3Config).where(S3Config.enabled.is_(True))
        .order_by(S3Config.id).limit(1)
    )).scalar_one_or_none()
    if row is None:
        _CFG_CACHE = {"at": now, "settings": None, "config_id": None, "name": None}
        return None
    from account.crypto import decrypt_text
    from tools.s3_client import S3Settings, invalidate_cache

    secret = decrypt_text(row.secret_cipher or "")
    settings = S3Settings(
        endpoint=row.endpoint or "",
        access_key_id=row.access_key_id or "",
        secret_access_key=secret,
        region=row.region or "garage",
        bucket=row.bucket or "photos",
        addressing_style=row.addressing_style or "path",
    )
    _CFG_CACHE = {"at": now, "settings": settings,
                  "config_id": row.id, "name": row.name}
    return {"config_id": row.id, "settings": settings, "name": row.name}


def invalidate_config_cache() -> None:
    global _CFG_CACHE
    _CFG_CACHE = {"at": 0.0, "settings": None, "config_id": None, "name": None}
    try:
        from tools.s3_client import invalidate_cache
        invalidate_cache()
    except Exception:
        pass


async def backup_enabled(session) -> bool:
    """备份功能是否可用（有启用配置且密钥解得出来）。"""
    try:
        cfg = await active_settings(session)
        return bool(cfg and cfg["settings"].secret_access_key)
    except Exception:
        return False


async def ensure_seed_config(session) -> bool:
    """库为空时写入种子配置（幂等，按 name 判重）。"""
    exists = (await session.execute(
        select(S3Config).where(S3Config.name == SEED_S3_CONFIG["name"])
    )).scalar_one_or_none()
    if exists is not None:
        return False
    from account.crypto import encrypt_text

    session.add(S3Config(
        name=SEED_S3_CONFIG["name"],
        endpoint=SEED_S3_CONFIG["endpoint"],
        access_key_id=SEED_S3_CONFIG["access_key_id"],
        secret_cipher=encrypt_text(SEED_S3_CONFIG["secret_key"]),
        region=SEED_S3_CONFIG["region"],
        bucket=SEED_S3_CONFIG["bucket"],
        addressing_style=SEED_S3_CONFIG["addressing_style"],
        enabled=True,
    ))
    await session.commit()
    invalidate_config_cache()
    return True


# ---------------------------------------------------------------------------
# 对象键
# ---------------------------------------------------------------------------

def object_key_for(platform: str, media_type: str, content_id: str,
                   filename: str) -> str:
    """对象键：{platform}/{media_type}/{content_id}/{filename}。

    与本地 data/{platform}/media/{content_id}/ 布局对齐，按平台与
    文件类型分文件夹；片段全部过 sanitize_component 防路径穿越。
    """
    from media_downloader.paths import sanitize_component

    p = sanitize_component(platform or "unknown", "unknown")
    mt = media_type if media_type in ("image", "video", "audio") else "image"
    cid = sanitize_component(str(content_id or ""), "unknown")
    fn = sanitize_component(filename or "media", "media")
    return f"{p}/{mt}/{cid}/{fn}"


def _fallback_filename(media: MediaAttachment) -> str:
    """无本地文件时的对象文件名：序号 + 从 URL 推断的扩展名。

    CDN 直链常带 `!nd_dft_wlteh_webp_3` 这类处理参数（无普通后缀），
    guess_extension 认不出来时会退 .bin；这里对 URL 尾部补一层格式
    关键词匹配，尽量保住真实扩展名。
    """
    from media_downloader.paths import guess_extension

    ext = guess_extension(media.source_url or "", media.mime_type)
    if ext == ".bin":
        tail = (media.source_url or "").lower()
        for marker, real in (("_webp_", ".webp"), ("_jpg_", ".jpg"),
                             ("_jpeg_", ".jpeg"), ("_png_", ".png"),
                             ("_gif_", ".gif"), ("_mp4_", ".mp4")):
            if marker in tail:
                ext = real
                break
    return f"{(media.sort_order or 0) + 1:03d}{ext}"


def backup_key_for_media(media: MediaAttachment, platform: str,
                         content_id: str) -> str:
    filename = (Path(media.local_path).name if media.local_path
                else _fallback_filename(media))
    return object_key_for(platform, media.media_type or "image",
                          content_id, filename)


# ---------------------------------------------------------------------------
# 入队
# ---------------------------------------------------------------------------

async def enqueue_backup(session, media: MediaAttachment, platform: str,
                         content_id: str, story_id: Optional[int] = None) -> Optional[MediaBackup]:
    """把一条媒体加入备份队列（幂等；无启用配置时跳过，不产生 pending 垃圾）。"""
    if not await backup_enabled(session):
        return None
    exists = (await session.execute(
        select(MediaBackup).where(MediaBackup.media_id == media.id)
    )).scalar_one_or_none()
    if exists is not None:
        return exists
    cfg = await active_settings(session)
    row = MediaBackup(
        media_id=media.id,
        story_id=story_id,
        s3_config_id=cfg["config_id"] if cfg else None,
        platform=platform,
        content_id=str(content_id or ""),
        media_type=media.media_type or "image",
        object_key=backup_key_for_media(media, platform, content_id),
        source_url=media.source_url,
        mime_type=media.mime_type,
        status="pending",
    )
    session.add(row)
    return row


async def enqueue_missing(session=None, limit: int = 500) -> int:
    """历史媒体入队：扫出没有备份记录的 media_attachment，补建 pending 行。

    平台/内容 ID 按 target_type 回查（chapter 用章节的，story 用主帖 ID，
    comment 用评论 ID）；只增不删，重复跑安全。
    不传 session 时自建（后台循环 / API 触发用）。
    """
    if session is not None:
        return await _enqueue_missing(session, limit)
    from config.db_config import resolve_config_db_type
    from database.db_session import get_async_engine
    from sqlalchemy.ext.asyncio import AsyncSession

    engine = get_async_engine(resolve_config_db_type())
    if engine is None:
        return 0
    try:
        async with AsyncSession(engine, expire_on_commit=False) as s:
            return await _enqueue_missing(s, limit)
    finally:
        await engine.dispose()


async def _enqueue_missing(session, limit: int = 500) -> int:
    if not await backup_enabled(session):
        return 0
    from database.models_v2 import Comment, SourceSite, Story, StoryChapter

    done_ids = {r[0] for r in (await session.execute(
        select(MediaBackup.media_id))).all()}

    site_code = {s.id: s.code for s in (await session.execute(
        select(SourceSite))).scalars().all()}

    rows = (await session.execute(
        select(MediaAttachment).where(
            MediaAttachment.media_type.in_(("image", "video", "audio"))
        ).order_by(MediaAttachment.id.desc()).limit(limit * 4)
    )).scalars().all()

    chapter_map: Dict[int, Tuple[str, str, int]] = {}
    chapter_ids = [m.target_id for m in rows if m.target_type == "chapter"]
    if chapter_ids:
        for cid, sid, ccid, story_id in (await session.execute(
            select(StoryChapter.id, StoryChapter.source_site_id,
                   StoryChapter.source_content_id, StoryChapter.story_id)
            .where(StoryChapter.id.in_(chapter_ids))
        )).all():
            chapter_map[cid] = (site_code.get(sid) or "unknown",
                                str(ccid or ""), story_id)

    story_platform: Dict[int, str] = {}
    story_ids = [m.target_id for m in rows if m.target_type == "story"]
    if story_ids:
        for sid, psid in (await session.execute(
            select(Story.id, Story.primary_site_id).where(Story.id.in_(story_ids))
        )).all():
            story_platform[sid] = site_code.get(psid) or "unknown"

    comment_map: Dict[int, Tuple[str, str, int]] = {}
    comment_ids = [m.target_id for m in rows if m.target_type == "comment"]
    if comment_ids:
        for cid, sid, scid, story_id in (await session.execute(
            select(Comment.id, Comment.source_site_id,
                   Comment.source_comment_id, Comment.story_id)
            .where(Comment.id.in_(comment_ids))
        )).all():
            comment_map[cid] = (site_code.get(sid) or "unknown",
                                str(scid or ""), story_id)

    added = 0
    for m in rows:
        if m.id in done_ids:
            continue
        if m.target_type == "chapter" and m.target_id in chapter_map:
            platform, content_id, story_id = chapter_map[m.target_id]
        elif m.target_type == "story":
            platform = story_platform.get(m.target_id, "unknown")
            content_id, story_id = str(m.target_id), m.target_id
        elif m.target_type == "comment" and m.target_id in comment_map:
            platform, content_id, story_id = comment_map[m.target_id]
        else:
            continue
        row = MediaBackup(
            media_id=m.id, story_id=story_id,
            s3_config_id=None,
            platform=platform, content_id=content_id,
            media_type=m.media_type or "image",
            object_key=backup_key_for_media(m, platform, content_id),
            source_url=m.source_url, mime_type=m.mime_type,
            status="pending",
        )
        session.add(row)
        added += 1
        if added >= limit:
            break
    if added:
        await session.commit()
    return added


# ---------------------------------------------------------------------------
# 上传执行
# ---------------------------------------------------------------------------

def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


_PLATFORM_REFERER = {
    "xhs": "https://www.xiaohongshu.com/",
    "dy": "https://www.douyin.com/",
    "ks": "https://www.kuaishou.com/",
    "bili": "https://www.bilibili.com/",
    "wb": "https://weibo.com/",
    "tieba": "https://tieba.baidu.com/",
    "zhihu": "https://www.zhihu.com/",
}


async def _fetch_remote(url: str, platform: str) -> bytes:
    """本地文件缺失时，从平台远程地址直取（带头 Referer 过防盗链）。"""
    import httpx

    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36"),
        "Referer": _PLATFORM_REFERER.get(platform or "", "https://www.baidu.com/"),
    }
    async with httpx.AsyncClient(timeout=60, follow_redirects=True,
                                 trust_env=False) as client:
        resp = await client.get(url, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"远端返回 {resp.status_code}")
    if len(resp.content) > _MAX_REMOTE_BYTES:
        raise RuntimeError(f"远端文件超过 {_MAX_REMOTE_BYTES // 1024 // 1024}MB 上限")
    return resp.content


async def backup_one(session, row: MediaBackup, settings) -> Tuple[bool, str]:
    """备份单条媒体。返回 (成功?, 错误信息)。"""
    from tools.s3_client import head_object, upload_bytes, upload_file

    media = (await session.execute(
        select(MediaAttachment).where(MediaAttachment.id == row.media_id)
    )).scalar_one_or_none()

    row.status = "uploading"
    await session.commit()

    checksum = ""
    try:
        if media is not None and media.local_path and Path(media.local_path).is_file():
            # 1) 本地副本优先
            result = await upload_file(settings, media.local_path,
                                       row.object_key, row.mime_type)
            checksum = _sha256_file(media.local_path)
            size = result["size"]
        elif row.source_url:
            # 2) 本地没了（如帖子删过本地文件）→ 从平台远程直取
            data = await _fetch_remote(row.source_url, row.platform or "")
            result = await upload_bytes(settings, data, row.object_key,
                                        row.mime_type)
            checksum = hashlib.sha256(data).hexdigest()
            size = result["size"]
        else:
            raise RuntimeError("无本地文件且无远端地址，无法备份")

        # 3) 上传后校验：对象必须存在且大小一致
        head = await head_object(settings, row.object_key)
        if head is None:
            raise RuntimeError("上传后 head_object 查无此对象")
        if head.get("ContentLength") is not None and int(head["ContentLength"]) != int(size):
            raise RuntimeError(
                f"大小不一致：本地 {size} / 远端 {head.get('ContentLength')}")

        row.status = "uploaded"
        row.size_bytes = size
        row.checksum = checksum or (head.get("ETag") or "").strip('"')
        row.uploaded_at = _now()
        row.last_error = None
        row.next_retry_at = None
        await session.commit()
        return True, ""
    except Exception as e:
        return await _mark_failure(session, row, e)


def _now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


async def _mark_failure(session, row: MediaBackup, exc: Exception) -> Tuple[bool, str]:
    """失败登记：指数退避；次数耗尽转 failed（保留记录待手动重试）。"""
    err = f"{type(exc).__name__}: {exc}"
    row.retry_count = int(row.retry_count or 0) + 1
    row.last_error = err[:500]
    if row.retry_count >= int(row.max_retries or 3):
        row.status = "failed"
        row.next_retry_at = None
    else:
        row.status = "pending"
        row.next_retry_at = _now() + _backoff(row.retry_count)
    try:
        await session.commit()
    except Exception:
        await session.rollback()
    return False, err


def _backoff(retry_count: int):
    from datetime import timedelta

    return timedelta(seconds=_RETRY_BACKOFF_SEC * (2 ** max(0, retry_count - 1)))


async def _due_rows(session, limit: int):
    """选出到期该处理的条目：pending 且（无退避约束 或 已到重试时间）。"""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    q = (select(MediaBackup)
         .where(MediaBackup.status.in_(("pending", "uploading")))
         .where((MediaBackup.next_retry_at.is_(None))
                | (MediaBackup.next_retry_at <= now))
         .order_by(MediaBackup.id.asc()).limit(limit))
    return (await session.execute(q)).scalars().all()


async def run_backup_batch(limit: int = _BATCH_LIMIT,
                           concurrency: int = _BATCH_CONCURRENCY) -> Dict[str, int]:
    """跑一批备份（定时循环与手动触发共用，锁互斥）。

    自管 session（与 story.py 的 _session 同模式）。每条目在独立 session 里
    按 id 重取后再处理——ORM 对象不能跨 session 改，否则提交直接被回滚，
    状态会永远停在 pending（实测踩坑）。
    """
    async with _RUN_LOCK:
        from config.db_config import resolve_config_db_type
        from database.db_session import get_async_engine
        from sqlalchemy.ext.asyncio import AsyncSession

        engine = get_async_engine(resolve_config_db_type())
        if engine is None:
            return {"picked": 0, "ok": 0, "fail": 0, "skipped": "db_unavailable"}
        stats = {"picked": 0, "ok": 0, "fail": 0}
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                cfg = await active_settings(session)
                if cfg is None or not cfg["settings"].secret_access_key:
                    return {"picked": 0, "ok": 0, "fail": 0, "skipped": "no_config"}
                rows = await _due_rows(session, limit)
                stats["picked"] = len(rows)
                row_ids = [r.id for r in rows]
                sem = asyncio.Semaphore(concurrency)

                async def _work(rid: int):
                    async with sem:
                        # 独立 session + 按 id 重取：避免跨 session 改对象被回滚
                        async with AsyncSession(engine, expire_on_commit=False) as s2:
                            r = (await s2.execute(
                                select(MediaBackup).where(MediaBackup.id == rid)
                            )).scalar_one_or_none()
                            if r is None:
                                return False
                            try:
                                ok, _err = await backup_one(s2, r, cfg["settings"])
                                return ok
                            except Exception:
                                return False

                results = await asyncio.gather(
                    *[_work(rid) for rid in row_ids], return_exceptions=True)
                for res in results:
                    if res is True:
                        stats["ok"] += 1
                    else:
                        stats["fail"] += 1
        except Exception as e:
            stats["error"] = f"{type(e).__name__}: {e}"[:300]
        finally:
            await engine.dispose()
        return stats


async def retry_failed(ids: Optional[List[int]] = None) -> int:
    """把 failed（或指定）条目重置为 pending 立即重跑。返回重置条数。"""
    from config.db_config import resolve_config_db_type
    from database.db_session import get_async_engine
    from sqlalchemy.ext.asyncio import AsyncSession

    engine = get_async_engine(resolve_config_db_type())
    if engine is None:
        return 0
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            q = select(MediaBackup).where(MediaBackup.status == "failed")
            if ids:
                q = select(MediaBackup).where(MediaBackup.id.in_(ids))
            rows = (await session.execute(q)).scalars().all()
            for r in rows:
                r.status = "pending"
                r.retry_count = 0
                r.next_retry_at = None
                r.last_error = None
            await session.commit()
            return len(rows)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 联动清除：原贴硬删除 → S3 备份对象一并删除
# ---------------------------------------------------------------------------

def purge_with_story() -> bool:
    """删帖时是否联动删 S3 备份（默认开，S3_BACKUP_PURGE_WITH_STORY 可关）。"""
    import config

    return bool(getattr(config, "S3_BACKUP_PURGE_WITH_STORY", True))


async def purge_media_backups(session, media_ids: List[int],
                              concurrency: int = 4) -> Dict[str, int]:
    """删除指定媒体的 S3 备份对象与记录（删帖联动，不自行 commit）。

    语义：
    - pending / failed → 从未上传成功，无对象，直接删记录；
    - uploaded / uploading / orphaned → 尝试删 S3 对象，成功才删记录；
    - object_key 仍被本次范围之外的备份记录引用（跨帖同键）→ 保留对象，
      只删自己的记录，避免误删别人还在用的备份；
    - S3 删除失败 → 记录转 orphaned（原帖已删、对象待清理），不阻断删帖，
      由后台循环重试或管理页手动清理。

    返回 {"purged": 已删对象+记录, "orphaned": 待清理, "kept_ref": 保留对象,
          "skipped": 无配置/无记录}
    """
    stats = {"purged": 0, "orphaned": 0, "kept_ref": 0, "skipped": 0}
    if not media_ids:
        return stats

    from sqlalchemy import func as sa_func

    from tools.s3_client import delete_object

    rows = (await session.execute(
        select(MediaBackup).where(MediaBackup.media_id.in_(media_ids))
    )).scalars().all()
    if not rows:
        return stats

    row_ids = [r.id for r in rows]
    # 交叉引用保护：这些 object_key 有没有被未删除的备份记录指着
    ref_counts = (await session.execute(
        select(MediaBackup.object_key, sa_func.count(MediaBackup.id))
        .where(MediaBackup.object_key.in_([r.object_key for r in rows]),
               MediaBackup.id.not_in(row_ids))
        .group_by(MediaBackup.object_key)
    )).all()
    ref_keys = {k for k, n in ref_counts if (n or 0) > 0}

    cfg = await active_settings(session)

    # S3 删除是网络调用，并发执行；session 的写操作全部串行在 gather 之后，
    # 避免 AsyncSession 被并发使用
    sem = asyncio.Semaphore(max(1, concurrency))
    need_s3 = [r for r in rows
               if r.status in ("uploaded", "uploading", "orphaned")
               and r.object_key not in ref_keys]

    async def _try(r: MediaBackup):
        async with sem:
            try:
                await delete_object(cfg["settings"], r.object_key)
                return r, True, ""
            except Exception as e:
                return r, False, f"{type(e).__name__}: {str(e)[:200]}"

    s3_results: Dict[int, tuple] = {}
    if need_s3 and cfg is not None and cfg["settings"].secret_access_key:
        for res in await asyncio.gather(*[_try(r) for r in need_s3],
                                        return_exceptions=True):
            if isinstance(res, Exception):
                continue
            r, ok, err = res
            s3_results[r.id] = (ok, err)
    elif need_s3:
        # 没有可用配置：对象动不了，全部转 orphaned 留线索
        for r in need_s3:
            s3_results[r.id] = (False, "无可用 S3 配置，对象待手动清理")

    for r in rows:
        if r.object_key in ref_keys:
            # 对象仍被其它媒体引用：保留对象，只删自己的记录
            await session.delete(r)
            stats["kept_ref"] += 1
        elif r.status in ("uploaded", "uploading", "orphaned"):
            ok, err = s3_results.get(r.id, (False, "未执行"))
            if ok:
                await session.delete(r)
                stats["purged"] += 1
            else:
                r.status = "orphaned"
                r.last_error = err[:500]
                r.next_retry_at = None
                stats["orphaned"] += 1
        else:
            # pending / failed：没有对象，直接删记录
            await session.delete(r)
            stats["purged"] += 1
    return stats


async def retry_orphaned_purges(limit: int = 20) -> Dict[str, int]:
    """重试孤立备份的 S3 删除（删帖时删失败的那批）。

    后台循环每次 tick 调用；成功即删记录。自管 session。
    """
    from config.db_config import resolve_config_db_type
    from database.db_session import get_async_engine
    from sqlalchemy.ext.asyncio import AsyncSession

    engine = get_async_engine(resolve_config_db_type())
    if engine is None:
        return {"picked": 0, "purged": 0, "still": 0}
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            cfg = await active_settings(session)
            if cfg is None or not cfg["settings"].secret_access_key:
                return {"picked": 0, "purged": 0, "still": 0, "skipped": "no_config"}
            rows = (await session.execute(
                select(MediaBackup).where(MediaBackup.status == "orphaned")
                .order_by(MediaBackup.id.asc()).limit(limit)
            )).scalars().all()
            if not rows:
                return {"picked": 0, "purged": 0, "still": 0}
            stat = await purge_media_backups(
                session, [r.media_id for r in rows])
            await session.commit()
            return {"picked": len(rows), "purged": stat["purged"],
                    "still": stat["orphaned"]}
    except Exception as e:
        return {"picked": 0, "purged": 0, "still": 0, "error": str(e)[:200]}
    finally:
        await engine.dispose()


async def purge_backup_item(item_id: int) -> Dict[str, Any]:
    """手动清理单条备份记录（管理页用）：删 S3 对象 + 删记录。

    对 orphaned / uploaded 都适用（上传错的也能手动撤）。
    """
    from config.db_config import resolve_config_db_type
    from database.db_session import get_async_engine
    from sqlalchemy.ext.asyncio import AsyncSession

    from tools.s3_client import delete_object

    engine = get_async_engine(resolve_config_db_type())
    if engine is None:
        return {"success": False, "error": "配置库不可用"}
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            row = (await session.execute(
                select(MediaBackup).where(MediaBackup.id == item_id)
            )).scalar_one_or_none()
            if row is None:
                return {"success": False, "error": "记录不存在"}
            object_key = row.object_key
            # 对象仍被其它记录引用时不删对象
            from sqlalchemy import func as sa_func

            refs = (await session.execute(
                select(sa_func.count(MediaBackup.id)).where(
                    MediaBackup.object_key == object_key,
                    MediaBackup.id != item_id)
            )).scalar() or 0
            cfg = await active_settings(session)
            s3_ok = None
            if refs == 0 and cfg is not None and cfg["settings"].secret_access_key:
                try:
                    await delete_object(cfg["settings"], object_key)
                    s3_ok = True
                except Exception as e:
                    s3_ok = False
                    err = f"{type(e).__name__}: {str(e)[:200]}"
            await session.delete(row)
            await session.commit()
            return {"success": True, "object_deleted": s3_ok,
                    "kept_for_refs": refs > 0,
                    "error": None if s3_ok is not False else err}
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

async def backup_stats() -> Dict[str, Any]:
    """备份总览：各状态条数、已备份体积、最近错误。"""
    from config.db_config import resolve_config_db_type
    from database.db_session import get_async_engine
    from sqlalchemy import func as sa_func
    from sqlalchemy.ext.asyncio import AsyncSession

    engine = get_async_engine(resolve_config_db_type())
    if engine is None:
        return {"configured": False}
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            rows = (await session.execute(
                select(MediaBackup.status, sa_func.count(MediaBackup.id),
                       sa_func.coalesce(sa_func.sum(MediaBackup.size_bytes), 0))
                .group_by(MediaBackup.status)
            )).all()
            by_status = {r[0]: {"count": int(r[1]), "bytes": int(r[2] or 0)}
                         for r in rows}
            last_err = (await session.execute(
                select(MediaBackup.last_error, MediaBackup.updated_at)
                .where(MediaBackup.last_error.isnot(None))
                .order_by(MediaBackup.updated_at.desc()).limit(1)
            )).first()
            cfg = await active_settings(session)
            return {
                "configured": cfg is not None,
                "config_name": (cfg or {}).get("name"),
                "by_status": by_status,
                "pending": by_status.get("pending", {}).get("count", 0),
                "uploading": by_status.get("uploading", {}).get("count", 0),
                "uploaded": by_status.get("uploaded", {}).get("count", 0),
                "failed": by_status.get("failed", {}).get("count", 0),
                # orphaned：原帖已删但 S3 对象删除失败，待重试/手动清理
                "orphaned": by_status.get("orphaned", {}).get("count", 0),
                "uploaded_bytes": by_status.get("uploaded", {}).get("bytes", 0),
                "last_error": (last_err[0] if last_err else None),
                "last_error_at": (last_err[1].isoformat() if last_err and last_err[1] else None),
            }
    finally:
        await engine.dispose()


async def story_backup_state(session, story_ids: List[int]) -> Dict[int, Dict[str, int]]:
    """批量查帖子的备份进度（列表页徽标用）：{story_id: {total, uploaded}}。

    total   = 该帖全部媒体（图/视频/音频，经章节关联）
    uploaded= 其中已上传 S3 的条数
    """
    if not story_ids:
        return {}
    from sqlalchemy import and_, func as sa_func

    from database.models_v2 import StoryChapter

    out: Dict[int, Dict[str, int]] = {}
    total_rows = (await session.execute(
        select(StoryChapter.story_id, sa_func.count(MediaAttachment.id))
        .join(MediaAttachment, and_(
            MediaAttachment.target_id == StoryChapter.id,
            MediaAttachment.target_type == "chapter"))
        .where(StoryChapter.story_id.in_(story_ids))
        .group_by(StoryChapter.story_id)
    )).all()
    for sid, n in total_rows:
        out[sid] = {"total": int(n or 0), "uploaded": 0}

    up_rows = (await session.execute(
        select(StoryChapter.story_id, sa_func.count(MediaBackup.id))
        .join(MediaAttachment, and_(
            MediaAttachment.target_id == StoryChapter.id,
            MediaAttachment.target_type == "chapter"))
        .join(MediaBackup, MediaBackup.media_id == MediaAttachment.id)
        .where(StoryChapter.story_id.in_(story_ids),
               MediaBackup.status == "uploaded")
        .group_by(StoryChapter.story_id)
    )).all()
    for sid, n in up_rows:
        if sid in out:
            out[sid]["uploaded"] = int(n or 0)
    return out


async def story_backup_detail(session, story_id: int) -> Dict[str, Any]:
    """单帖备份明细（详情页/手动触发用）。"""
    from sqlalchemy import and_, func as sa_func

    from database.models_v2 import StoryChapter

    total = (await session.execute(
        select(sa_func.count(MediaAttachment.id))
        .join(StoryChapter, and_(
            MediaAttachment.target_id == StoryChapter.id,
            MediaAttachment.target_type == "chapter"))
        .where(StoryChapter.story_id == story_id)
    )).scalar() or 0
    uploaded = (await session.execute(
        select(sa_func.count(MediaBackup.id))
        .join(MediaAttachment, MediaBackup.media_id == MediaAttachment.id)
        .join(StoryChapter, and_(
            MediaAttachment.target_id == StoryChapter.id,
            MediaAttachment.target_type == "chapter"))
        .where(StoryChapter.story_id == story_id,
               MediaBackup.status == "uploaded")
    )).scalar() or 0
    return {"total": int(total), "uploaded": int(uploaded),
            "complete": int(total) > 0 and int(uploaded) >= int(total)}
