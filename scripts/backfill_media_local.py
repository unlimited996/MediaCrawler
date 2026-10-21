# -*- coding: utf-8 -*-
"""本地媒体回挂（P0）：把已下载到本地的图片登记进 media_attachment。

背景：
    详情页图片裂的根因是外链为带时间戳签名的临时 CDN 链接（实测 403），
    而 `media_attachment.local_path` 为空 —— 同步那一刻本地文件还没落盘，
    之后也不会再回补。但文件其实就躺在 `data/<platform>/media/<content_id>/`
    下（001.webp / 001.jpg ...），只是没登记。

本脚本做的事：
    扫描 `local_path IS NULL` 的媒体行 → 由 chapter 的 source_content_id 定位
    本地目录 → 按 sort_order 找 001/002/... → 命中即写入 local_path、
    storage_type='local'、size_bytes、mime_type、checksum、downloaded_at。

约束：
    - **只补不全**：本地没有就跳过，绝不做网络补下载；
    - 幂等：只处理 `local_path IS NULL` 的行，可反复执行；
    - 不删除任何已有记录。

用法：
    python scripts/backfill_media_local.py            # 全库
    python scripts/backfill_media_local.py xhs        # 只处理某个平台
    python scripts/backfill_media_local.py --story 50 # 只处理某条内容
"""
from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.models_v2 import (  # noqa: E402
    MediaAttachment, SourceSite, Story, StoryChapter,
)
from media_downloader.paths import build_media_dir  # noqa: E402
from tools.paths import DATA_DIR  # noqa: E402
from tools.v2_sync import _v2_session  # noqa: E402

#: 本地文件名的扩展名候选（与下载器落盘规则一致）
EXTS = (".webp", ".jpg", ".jpeg", ".png", ".gif")

#: 小图（<256KB）可选择编码入库兜底；默认关闭，靠命令行开关打开
INLINE_LIMIT = 256 * 1024


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _find_local(media_dir: Path, sort_order: int) -> Optional[Path]:
    """按 sort_order（0 基）找 001/002/... 的本地文件。"""
    idx = int(sort_order or 0) + 1
    for ext in EXTS:
        p = media_dir / f"{idx:03d}{ext}"
        if p.exists() and p.is_file():
            return p
    return None


async def run(platform: str = "", story_id: Optional[int] = None,
              inline: bool = False) -> Dict[str, int]:
    stats = {"scanned": 0, "matched": 0, "missed": 0, "inlined": 0, "counts": 0}
    async with _v2_session() as session:
        if session is None:
            print("[backfill] 数据库不可用")
            return stats

        stmt = (select(MediaAttachment, StoryChapter, Story, SourceSite)
                .join(StoryChapter, MediaAttachment.target_id == StoryChapter.id)
                .join(Story, StoryChapter.story_id == Story.id)
                .join(SourceSite, Story.primary_site_id == SourceSite.id)
                .where(MediaAttachment.target_type == "chapter",
                       MediaAttachment.local_path.is_(None)))
        if platform:
            stmt = stmt.where(SourceSite.code == platform)
        if story_id:
            stmt = stmt.where(Story.id == story_id)

        rows = (await session.execute(stmt)).all()
        stats["scanned"] = len(rows)

        for media, chapter, story, site in rows:
            content_id = chapter.source_content_id
            if not content_id:
                stats["missed"] += 1
                continue
            media_dir = build_media_dir(DATA_DIR, site.code, content_id)
            local = _find_local(media_dir, media.sort_order)
            if local is None:
                stats["missed"] += 1
                continue

            media.local_path = str(local)
            media.storage_type = "local"
            media.download_status = "done"
            media.size_bytes = local.stat().st_size
            media.mime_type = (mimetypes.guess_type(str(local))[0]
                               or "image/jpeg")
            try:
                media.checksum = _sha256(local)
            except Exception:
                pass
            from sqlalchemy import text as _text
            media.downloaded_at = (await session.execute(_text("SELECT now()"))).scalar()
            media.source_status = "unknown"
            ext = dict(media.ext or {})
            ext["origin"] = "local_cache_backfill"
            media.ext = ext

            # 可选：小图编码入库兜底（对象存储不可用时的最后一道保险）
            if inline and media.size_bytes <= INLINE_LIMIT:
                try:
                    media.inline_data = local.read_bytes()
                    media.inline_mime = media.mime_type
                    stats["inlined"] += 1
                except Exception:
                    pass

            stats["matched"] += 1

        await session.commit()

        # 媒体计数 / 封面：本次扫到的 story 全部刷新（历史已挂过的也要补，
        # 否则 media_count 停留在 0）
        stories = {row[2].id for row in rows}
        for sid in stories:
            await _refresh_media_count(session, sid)
        await session.commit()

        # 全量补一遍（--all-counts 或首次跑）：没有待回挂媒体的 story 也会被覆盖
        if not rows or "--all-counts" in sys.argv:
            all_ids = [r[0] for r in (await session.execute(
                select(Story.id).where(Story.deleted_at.is_(None)))).all()]
            for sid in all_ids:
                await _refresh_media_count(session, sid)
            await session.commit()
            stats["counts"] = len(all_ids)
    return stats


async def _refresh_media_count(session, story_id: int) -> None:
    """回填 story.media_count（列表页可直接展示）。"""
    from sqlalchemy import func as _func
    ch_ids = [r[0] for r in (await session.execute(
        select(StoryChapter.id).where(StoryChapter.story_id == story_id))).all()]
    if not ch_ids:
        return
    cnt = (await session.execute(
        select(_func.count()).select_from(MediaAttachment).where(
            MediaAttachment.target_type == "chapter",
            MediaAttachment.target_id.in_(ch_ids))
    )).scalar() or 0
    cover_id = (await session.execute(
        select(MediaAttachment.id).where(
            MediaAttachment.target_type == "chapter",
            MediaAttachment.target_id.in_(ch_ids),
            MediaAttachment.media_type == "image")
        .order_by(MediaAttachment.sort_order.asc()).limit(1)
    )).scalar()
    story = await session.get(Story, story_id)
    if story is not None:
        story.media_count = int(cnt)
        story.cover_media_id = cover_id


def main() -> None:
    args = [a for a in sys.argv[1:]]
    inline = "--inline" in args
    args = [a for a in args if not a.startswith("--")]
    story_id: Optional[int] = None
    platform = ""
    for a in args:
        if a.isdigit():
            story_id = int(a)
        else:
            platform = a
    stats = asyncio.run(run(platform=platform, story_id=story_id, inline=inline))
    print(f"[backfill] 扫描 {stats['scanned']} 条 · 回挂成功 {stats['matched']} 条 · "
          f"本地缺失 {stats['missed']} 条 · 编码入库 {stats['inlined']} 条 · "
          f"刷新媒体计数 {stats['counts']} 篇")


if __name__ == "__main__":
    main()
