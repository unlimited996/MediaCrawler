# -*- coding: utf-8 -*-
"""S3 备份去重（P4 扩展）：把字节完全相同的重复图片合并为一份。

背景
----
``media_backup`` 的对象键是 ``{platform}/{media_type}/{content_id}/{filename}``，
同一张图只要出现在不同帖子/评论里，就会被存成 N 个内容一致、键不同的 S3 对象，
白白占用存储。

去重策略（按内容，而非按链接）：
- 以 ``media_backup.checksum``（上传时算的 sha256）为分组键——相同 checksum 即
  相同字节，物理去重 100% 安全（sha256 碰撞可忽略）。
- 每组选一个**规范键**作为唯一留存副本：优先复用组内已有的 ``dedup/`` 规范键
  （上次去重留下的），否则直接取组内首行的现有键（``{platform}/{type}/{cid}/...``）
  作为规范键。把组内所有行的 ``object_key`` 改指向它，删除其余冗余对象，腾出空间。
  不新建额外对象，因此「保留 1 份、删除 行数-1 份」与报告完全对应。
- 规范键被多帖共享；删帖联动清除（purge_media_backups）仍按 object_key 交叉
  引用保护，最后一个引用者删除时才真正删对象，不会误删。

为什么不改其它键形状：前端主 S3 路径是 ``/api/s3-backup/object/{object_key}``
（直接读 DB 键），任意键都可用；本地媒体路由 ``/api/data/media/...`` 的 S3 回退
已放宽为按文件名后缀匹配（见 api/routers/data.py），规范键也能命中。

安全
----
- 只读 ``uploaded`` 且 checksum 非空的行；pending/failed/无校验和的行不纳入。
- 真正删除 S3 对象前，确认没有任何 media_backup 行还指向该键（防御性）。
- 拷贝失败（如源对象读不到）的组整体跳过，绝不删原对象；返回错误供排查。
- 幂等：重复运行，已合并的组不会再有冗余对象，自动变成 no-op。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select

from database.models_v2 import MediaBackup


#: 规范键前缀（与 object_key 现有前缀不冲突）
CANON_PREFIX = "dedup"


def canonical_key_for(checksum: str, media_type: str) -> str:
    """内容寻址规范键：``dedup/{media_type}/{sha256}``。"""
    mt = media_type if media_type in ("image", "video", "audio") else "image"
    return f"{CANON_PREFIX}/{mt}/{checksum}"


# ---------------------------------------------------------------------------
# 分析（只读，可随时跑，不影响运行中的爬虫）
# ---------------------------------------------------------------------------

async def analyze_duplicates(session) -> Dict[str, Any]:
    """扫描已上传备份，按 sha256 分组，统计可合并组数 / 冗余对象数 / 可收回体积。

    返回：
        analyzed_rows   参与分析的 uploaded 行数
        dup_groups      重复组数（同 checksum 且 >1 行）
        redundant_objects  冗余对象数（每组行数 - 1 之和）
        reclaimable_bytes  可收回字节（每组总行体积 - 规范对象体积 之和）
        groups[]          每组明细：checksum / media_type / count /
                         canonical_key / canonical_already / size_bytes /
                         reclaimable_bytes / keys[]
    """
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

    dup_groups: List[Dict[str, Any]] = []
    redundant_objects = 0
    reclaimable_bytes = 0
    for csum, items in by_csum.items():
        if len(items) < 2:
            continue
        sizes = [int(i.size_bytes or 0) for i in items]
        keys = [i.object_key for i in items]
        # 关键：按「不同 object_key」而不是「行数」统计。
        # 去重执行后多行会指向同一个规范键，S3 上只剩一份——若按行数算，
        # 报告会永远显示「可收回 N MB」，用户执行完以为没生效（2026-11-09 反馈）。
        distinct = list(dict.fromkeys(keys))
        if len(distinct) < 2:
            continue
        size_by_key: Dict[str, int] = {}
        for k, sz in zip(keys, sizes):
            size_by_key.setdefault(k, sz)
        # 若组内已存在规范键（上次去重留下的），优先复用，避免反复拷贝
        existing = [i for i in items if i.object_key.startswith(f"{CANON_PREFIX}/")]
        canonical = existing[0] if existing else items[0]
        canon_key = canonical.object_key
        canon_size = size_by_key.get(canon_key, int(canonical.size_bytes or 0))
        total = sum(size_by_key.values())
        reclaimable_bytes += (total - canon_size)
        redundant_objects += (len(distinct) - 1)
        dup_groups.append({
            "checksum": csum,
            "media_type": items[0].media_type,
            "count": len(distinct),
            "canonical_key": canon_key,
            "canonical_already": canon_key.startswith(f"{CANON_PREFIX}/"),
            "size_bytes": canon_size,
            "reclaimable_bytes": total - canon_size,
            "keys": distinct,
            "sizes": [size_by_key[k] for k in distinct],  # 与 keys 一一对应
            # 涉及的帖/评论（按 content_id 去重），便于判断合并范围
            "content_ids": sorted({i.content_id for i in items if i.content_id}),
        })
    dup_groups.sort(key=lambda g: g["reclaimable_bytes"], reverse=True)
    return {
        "analyzed_rows": len(rows),
        "dup_groups": len(dup_groups),
        "redundant_objects": redundant_objects,
        "reclaimable_bytes": reclaimable_bytes,
        "groups": dup_groups,
    }


# ---------------------------------------------------------------------------
# 执行合并（会删 S3 对象）
# ---------------------------------------------------------------------------

async def dedup_backups(session, settings, dry_run: bool = True,
                        concurrency: int = 4,
                        exclude_keys: Optional[set] = None) -> Dict[str, Any]:
    """把重复备份合并为规范键并删除冗余对象。

    规范键的选取与 :func:`analyze_duplicates` 保持一致：优先复用组内已有的
    ``dedup/`` 规范键（上次去重留下的），否则取组内首行的现有键作为规范键。
    这样执行结果与报告完全对应——每组恰好删除 ``行数-1`` 个冗余对象，保留
    1 份，可收回体积 = 总行体积 - 规范对象体积。

    :param dry_run: True 只返回将要执行的处理量，不改动任何数据/S3。
    :param settings: 生效的 S3Settings（来自 media_backup.active_settings）。
    :param exclude_keys: 受保护的对象键集合——这些键**绝不**改键、绝不删除
                        （用户在 UI 里把认为不该清理的副本勾掉后传入）。被排除的
                        键保持原样留存；组内其余非排除副本合并/删除照常进行。
    :returns: 统计 + 每组处理明细（errors 列出失败组，便于排查）。
    """
    exclude_keys = set(exclude_keys or set())
    report = await analyze_duplicates(session)
    groups = report["groups"]
    stats: Dict[str, Any] = {
        "groups": 0, "merged_rows": 0, "objects_deleted": 0,
        "bytes_reclaimed": 0, "errors": [], "skipped": 0,
        "protected": 0,
    }

    if dry_run:
        # 尊重排除：被完全排除（非排除副本 < 2）的组不计入；其余按与执行一致的
        # 规范键选取（优先组内 dedup/ 键、否则首个非排除键）估算冗余与回收量。
        merged, objs, reclaim = 0, 0, 0
        for g in groups:
            non_ex = [k for k in g["keys"] if k not in exclude_keys]
            if len(non_ex) < 2:
                continue
            merged += 1
            objs += (len(non_ex) - 1)
            # 规范键：组内 dedup/ 键（且未排除）优先，否则首个非排除键
            dedup_keys = [k for k in non_ex
                          if k.startswith(f"{CANON_PREFIX}/")]
            canon = dedup_keys[0] if dedup_keys else non_ex[0]
            size_by_key = dict(zip(g["keys"], g["sizes"]))
            total = sum(size_by_key[k] for k in non_ex)
            reclaim += (total - size_by_key.get(canon, 0))
        stats["groups"] = merged
        stats["objects_deleted"] = objs
        stats["bytes_reclaimed"] = reclaim
        stats["dry_run"] = True
        return stats

    stats["dry_run"] = False
    sem = asyncio.Semaphore(max(1, concurrency))

    for g in groups:
        csum = g["checksum"]
        try:
            # 重新取该组最新行（与 analyze 隔离，避免缓存过期）
            items = (await session.execute(
                select(MediaBackup).where(MediaBackup.checksum == csum)
            )).scalars().all()
            if len(items) < 2:
                stats["skipped"] += 1
                continue

            # 排除键不参与合并/删除；全部被排除或不足 2 条可合并则跳过整组
            non_excluded = [i for i in items
                            if i.object_key not in exclude_keys]
            if len(non_excluded) < 2:
                stats["skipped"] += 1
                stats["protected"] += (len(items) - len(non_excluded))
                continue

            # 规范键：优先组内已存在的 dedup/ 键（且未被排除），否则取首个非排除键
            existing = [i for i in non_excluded
                        if i.object_key.startswith(f"{CANON_PREFIX}/")]
            canonical = existing[0] if existing else non_excluded[0]
            target_canonical = canonical.object_key

            # 记录每行的原始键与体积（含被排除的，便于防御性核对）
            originals = {r.id: (r.object_key, int(r.size_bytes or 0))
                         for r in items}

            # 1) 规范对象是 ``dedup/`` 键且 S3 上不存在时，先从某源对象拷贝过来；
            #    其余情况（复用组内首行现有键 / 已存在的 dedup 键）无需拷贝。
            if target_canonical.startswith(f"{CANON_PREFIX}/"):
                head = await _head(settings, target_canonical)
                if head is None:
                    src = next((ok for ok in originals.values()
                                if ok[0] not in exclude_keys
                                and ok[0] != target_canonical), None)
                    if src is None:
                        stats["errors"].append(
                            {"checksum": csum, "error": "组内无可用源对象"})
                        continue
                    try:
                        await _ensure_canonical(settings, src[0], target_canonical)
                    except Exception as e:
                        stats["errors"].append(
                            {"checksum": csum,
                             "error": f"规范对象拷贝失败: {type(e).__name__}: {e}"})
                        continue

            # 2) 非排除的副本全部改键到规范键（被排除的保持原样）；同事务提交
            for r in non_excluded:
                r.object_key = target_canonical
            await session.commit()

            # 3) 删除冗余原始对象（规范键本身不删；被排除键不删；删前再确认
            #    无人引用）。被删除的恰好是「非排除副本数-1」个。
            dels = [orig for orig in originals.values()
                    if orig[0] not in exclude_keys
                    and orig[0] != target_canonical]
            deleted = await _delete_redundant(session, settings, sem, dels)
            stats["objects_deleted"] += deleted["deleted"]
            stats["bytes_reclaimed"] += deleted["bytes"]
            stats["protected"] += (len(items) - len(non_excluded))
            if deleted["deleted"] or len(dels):
                stats["groups"] += 1
                stats["merged_rows"] += len(non_excluded)
        except Exception as e:
            await _safe_rollback(session)
            stats["errors"].append(
                {"checksum": csum, "error": f"{type(e).__name__}: {e}"})

    return stats


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

async def _head(settings, key: str) -> Optional[Dict[str, Any]]:
    from tools.s3_client import head_object

    try:
        return await head_object(settings, key)
    except Exception:
        return None


async def _ensure_canonical(settings, src_key: str, dest_key: str) -> None:
    """把源对象弄到规范键：优先服务端拷贝，失败则下载重传。"""
    from tools.s3_client import copy_object, get_object, upload_bytes

    try:
        await copy_object(settings, src_key, dest_key)
        return
    except Exception:
        pass
    data = await get_object(settings, src_key)
    if data is None:
        raise RuntimeError("源对象读取失败")
    await upload_bytes(settings, data, dest_key)


async def _delete_redundant(session, settings, sem, dels: List[Tuple[str, int]]
                            ) -> Dict[str, int]:
    """删除冗余对象；删前确认该键已无任何 media_backup 行引用。"""
    from tools.s3_client import delete_object

    deleted, bytes_total = 0, 0
    async with sem:
        for key, size in dels:
            # 防御：仍被引用则不删（理论上改键后不会，但稳妥）
            ref = (await session.execute(
                select(func.count(MediaBackup.id)).where(
                    MediaBackup.object_key == key)
            )).scalar() or 0
            if ref > 0:
                continue
            try:
                await delete_object(settings, key)
                deleted += 1
                bytes_total += size
            except Exception:
                # 删除失败不阻断其它组，留给后台重试/手动清理
                pass
    return {"deleted": deleted, "bytes": bytes_total}


async def _safe_rollback(session) -> None:
    try:
        await session.rollback()
    except Exception:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
