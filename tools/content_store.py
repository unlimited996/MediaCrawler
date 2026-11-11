# -*- coding: utf-8 -*-
"""内容寻址的图片仓库：同一份内容在 S3 里只保留一个对象。

与「事后去重」(tools/media_dedup.py) 的差别：本模块服务于**写入时**。
备份/归档链路先算内容 sha256，再查已入库的规范对象：

    EmojiAsset(表情包库)  >  已 uploaded 的 MediaBackup  >  上传新对象

命中就不产生新的 S3 对象，只把 MediaBackup.object_key 指向规范键。
因为详情页的 backup_url 直接由 object_key 拼出，帖子里的图片备份会
自动「导向我们自己已入库的表情包」，无需前端改动。

规范键采用内容寻址 ``emoji/{media_type}/{sha256}{ext}``：
- 同一 checksum 永远同一个键，天然幂等；
- 与业务键 (``xhs/image/...``) 分离，删帖联动不会误删公共对象；
- 由服务端 copy_object 固化，不下载字节、不耗带宽。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import select

from database.models_v2 import EmojiAsset, MediaBackup

#: 内容寻址对象的键前缀（表情包库即规范对象库）
EMOJI_PREFIX = "emoji"
#: 与 media_dedup 的历史规范键兼容（老数据可能指向 dedup/）
LEGACY_PREFIX = "dedup"
#: 写入时校验只覆盖图片且不超过该体积，避免大视频拖慢抓取主流程
MAX_INLINE_HASH_BYTES = 8 * 1024 * 1024

_MIME_EXT = {
    "image/webp": ".webp", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/png": ".png", "image/gif": ".gif", "image/avif": ".avif",
    "video/mp4": ".mp4", "audio/mpeg": ".mp3",
}

#: 同一 checksum 的写入串行化，避免并发批次里 8 个 worker 各自上传同一张图
_CHECKSUM_LOCKS: Dict[str, asyncio.Lock] = {}
_LOCK_GUARD = asyncio.Lock()


async def checksum_lock(checksum: str) -> asyncio.Lock:
    """取该内容的进程内锁（同 checksum 串行，不同 checksum 并行）。"""
    async with _LOCK_GUARD:
        if len(_CHECKSUM_LOCKS) > 4096:
            _CHECKSUM_LOCKS.clear()
        lock = _CHECKSUM_LOCKS.get(checksum)
        if lock is None:
            lock = asyncio.Lock()
            _CHECKSUM_LOCKS[checksum] = lock
        return lock


def canonical_object_key(checksum: str, media_type: str = "image",
                         mime_type: Optional[str] = None) -> str:
    """内容寻址键，与内容一一对应（幂等）。"""
    mt = media_type if media_type in ("image", "video", "audio") else "image"
    ext = _MIME_EXT.get((mime_type or "").lower(), ".bin")
    return f"{EMOJI_PREFIX}/{mt}/{checksum}{ext}"


def is_canonical_key(key: str) -> bool:
    return str(key or "").startswith((f"{EMOJI_PREFIX}/", f"{LEGACY_PREFIX}/"))


async def find_canonical(session, checksum: str,
                         exclude_media_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """查已入库的规范对象。优先表情包库，其次任意已上传的备份记录。

    返回 {"object_key", "source", "size_bytes", "mime_type", "asset_id"}；
    没有可复用对象返回 None。EmojiAsset 表未建好时静默降级到 MediaBackup。
    """
    if not checksum:
        return None

    # 1) 表情包库：用户明确归档过的对象，优先级最高
    try:
        asset = (await session.execute(
            select(EmojiAsset)
            .where(EmojiAsset.checksum == checksum,
                   EmojiAsset.deleted_at.is_(None))
            .order_by(EmojiAsset.id).limit(1)
        )).scalar_one_or_none()
        if asset is not None and asset.object_key:
            return {
                "object_key": asset.object_key,
                "source": "emoji",
                "asset_id": asset.id,
                "size_bytes": int(asset.size_bytes or 0),
                "mime_type": asset.mime_type or "",
            }
    except Exception:
        pass  # 表还没建（startup 异步建表），不影响后续备份

    # 2) 已成功上传的备份记录：优先指向规范键的那条，避免引用一个即将被
    #    事后去重删掉的业务键
    try:
        conds = [MediaBackup.checksum == checksum,
                 MediaBackup.status == "uploaded",
                 MediaBackup.object_key.isnot(None)]
        if exclude_media_id is not None:
            conds.append(MediaBackup.media_id != exclude_media_id)
        q = (select(MediaBackup).where(*conds)
             .order_by(MediaBackup.id).limit(100))
        rows = (await session.execute(q)).scalars().all()
    except Exception:
        return None
    if not rows:
        return None
    pick = next((r for r in rows if is_canonical_key(r.object_key)), rows[0])
    return {
        "object_key": pick.object_key,
        "source": "backup",
        "asset_id": None,
        "size_bytes": int(pick.size_bytes or 0),
        "mime_type": pick.mime_type or "",
    }


async def find_canonical_by_url(session, source_url: str,
                                exclude_media_id: Optional[int] = None
                                ) -> Optional[Dict[str, Any]]:
    """同一 CDN 地址已备份过时直接复用（避免重复下载远端字节）。

    只有已 uploaded、有 checksum 的记录才可信；签名 URL 过期不影响判断，
    因为这里比较的是当初入库时记录的原始地址。
    """
    if not source_url:
        return None
    try:
        conds = [MediaBackup.source_url == source_url,
                 MediaBackup.status == "uploaded",
                 MediaBackup.checksum.isnot(None)]
        if exclude_media_id is not None:
            conds.append(MediaBackup.media_id != exclude_media_id)
        q = (select(MediaBackup).where(*conds)
             .order_by(MediaBackup.id).limit(1))
        row = (await session.execute(q)).scalar_one_or_none()
    except Exception:
        return None
    if row is None or not row.object_key:
        return None
    return {
        "object_key": row.object_key,
        "source": "url",
        "asset_id": None,
        "size_bytes": int(row.size_bytes or 0),
        "mime_type": row.mime_type or "",
        "checksum": row.checksum,
    }


async def object_exists(settings, key: str) -> bool:
    """确认规范对象确实还在 S3（防止引用一个已被删除的对象）。

    head_object 对 404 返回 None；其它异常一律按「不可信」处理，让调用方
    回退到正常上传——宁可多传一份，也不产出 404 引用。
    """
    if settings is None or not key:
        return False
    try:
        from tools.s3_client import head_object

        return await head_object(settings, key) is not None
    except Exception:
        return False


async def ensure_canonical_object(settings, checksum: str, src_key: str,
                                  media_type: str = "image",
                                  mime_type: Optional[str] = None) -> Optional[str]:
    """把已有对象固化到内容寻址规范键，返回规范键；失败返回 None。

    服务端 copy_object，不下载字节。规范键已存在时直接复用（幂等）。
    """
    canon = canonical_object_key(checksum, media_type, mime_type)
    if canon == src_key:
        return canon if await object_exists(settings, canon) else None
    try:
        from tools.s3_client import copy_object, head_object

        head = await head_object(settings, canon)
        if head is None:
            await copy_object(settings, src_key, canon)
            head = await head_object(settings, canon)
        return canon if head is not None else None
    except Exception:
        return None


async def converge_to_canonical(session, settings, checksum: str,
                                rows: List[MediaBackup],
                                media_type: str = "image",
                                mime_type: Optional[str] = None,
                                delete_redundant: bool = True
                                ) -> Dict[str, Any]:
    """把同 checksum 的备份行统一指向规范对象，并删除不再被引用的旧对象。

    这是「归档」的实质动作：帖子里的图片备份 URL 由 MediaBackup.object_key
    拼出，改完引用后所有帖子都指向同一份表情包对象，冗余对象随即删除。
    幂等：已指向规范键的行不再处理。

    :returns: {"canonical_key", "linked_rows", "objects_deleted",
               "delete_errors", "src_key"}
    """
    out: Dict[str, Any] = {
        "canonical_key": None, "linked_rows": 0, "objects_deleted": 0,
        "delete_errors": [], "src_key": None,
    }
    uploaded = [r for r in rows if r.status == "uploaded" and r.object_key]
    if not uploaded:
        return out

    src = next((r.object_key for r in uploaded
                if is_canonical_key(r.object_key)), uploaded[0].object_key)
    out["src_key"] = src
    canon = await ensure_canonical_object(
        settings, checksum, src, media_type, mime_type)
    if not canon:
        return out
    out["canonical_key"] = canon

    old_keys: Set[str] = set()
    # pending/failed 行没有自己的对象，但同样指向规范键，避免它们仍引用旧键
    # 导致下面的「无人引用」检查误判、旧对象删不掉
    for r in [x for x in rows if x.object_key]:
        if r.object_key == canon:
            continue
        if r.status == "uploaded":
            old_keys.add(r.object_key)   # 只有上传成功过的键才有对象可删
        ext = dict(r.ext or {})
        ext["dedup_ref"] = True
        ext["emoji_ref"] = True
        ext["original_key"] = r.object_key
        r.ext = ext
        r.object_key = canon
        out["linked_rows"] += 1

    if not old_keys:
        return out
    # 让下面的引用查询看到刚改过的键（AsyncSession 可能关闭了 autoflush）
    try:
        await session.flush()
    except Exception:
        pass
    if not delete_redundant or settings is None:
        return out

    # 删除前二次确认：旧键已无任何备份记录引用，才动 S3
    try:
        still = set((await session.execute(
            select(MediaBackup.object_key)
            .where(MediaBackup.object_key.in_(list(old_keys)))
        )).scalars().all())
    except Exception:
        still = set(old_keys)  # 查不动就一个都别删

    from tools.s3_client import delete_object

    for key in old_keys - still:
        if is_canonical_key(key):
            # emoji/、dedup/ 是公共规范对象，宁可留着也不误删（可能被别处引用）
            continue
        try:
            await delete_object(settings, key)
            out["objects_deleted"] += 1
        except Exception as e:
            out["delete_errors"].append(
                {"key": key, "error": f"{type(e).__name__}: {str(e)[:160]}"})
    return out
