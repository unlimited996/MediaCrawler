# -*- coding: utf-8 -*-
"""把存量抓取结果（data/<platform>/jsonl/*.jsonl）迁移到 v2 统一模型。

幂等：重复执行只补增量，已存在的记录按业务唯一键跳过。

用法：
    python scripts/migrate_jsonl_to_v2.py            # 迁移全部平台
    python scripts/migrate_jsonl_to_v2.py xhs        # 只迁移小红书
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.db_config import resolve_config_db_type  # noqa: E402
from database.db_session import get_async_engine  # noqa: E402
from database.models_v2 import (  # noqa: E402
    Author, Comment, MediaAttachment, SourceSite, Story, StoryChapter, StoryTag, Tag,
)
from tools.crawler_util import safe_int  # noqa: E402

DATA_DIR = Path(__file__).resolve().parents[1] / "data"

# 文件名 → 记录类型
KIND_BY_NAME = {
    "contents": "content",
    "comments": "comment",
    "creators": "creator",
}


def detect_kind(filename: str) -> Optional[str]:
    low = filename.lower()
    for key, kind in KIND_BY_NAME.items():
        if key in low:
            return kind
    return None


def split_list(value: Any) -> List[str]:
    """标签/图片字段可能是列表、JSON 字符串或逗号分隔字符串。"""
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v]
    text = str(value)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(v) for v in parsed if v]
    except (json.JSONDecodeError, TypeError):
        pass
    return [p.strip() for p in text.split(",") if p.strip()]


class Migrator:
    def __init__(self, session: AsyncSession, site_id: int, site_code: str):
        self.s = session
        self.site_id = site_id
        self.site_code = site_code
        self.stats = {"story": 0, "chapter": 0, "author": 0, "tag": 0,
                      "media": 0, "comment": 0, "skipped": 0}
        self._author_cache: Dict[str, int] = {}
        self._tag_cache: Dict[str, int] = {}
        self._story_by_content: Dict[str, int] = {}   # source_content_id -> story_id
        self._chapter_by_content: Dict[str, int] = {}

    async def get_author(self, anon_hash: str, nickname: str,
                         avatar: str = "") -> Optional[int]:
        if not anon_hash:
            return None
        if anon_hash in self._author_cache:
            return self._author_cache[anon_hash]
        row = (await self.s.execute(
            select(Author).where(Author.source_site_id == self.site_id,
                                 Author.anon_hash == anon_hash)
        )).scalar_one_or_none()
        if not row:
            row = Author(source_site_id=self.site_id, anon_hash=anon_hash,
                         nickname=nickname or None,
                         avatar_url=avatar or None)
            self.s.add(row)
            await self.s.flush()
            self.stats["author"] += 1
        elif avatar and not row.avatar_url:
            row.avatar_url = avatar
        self._author_cache[anon_hash] = row.id
        return row.id

    async def get_tag(self, name: str) -> Optional[int]:
        name = name.strip().lstrip("#")
        if not name:
            return None
        if name in self._tag_cache:
            return self._tag_cache[name]
        row = (await self.s.execute(
            select(Tag).where(Tag.source_site_id == self.site_id, Tag.name == name)
        )).scalar_one_or_none()
        if not row:
            row = Tag(source_site_id=self.site_id, name=name, tag_type="topic")
            self.s.add(row)
            await self.s.flush()
            self.stats["tag"] += 1
        self._tag_cache[name] = row.id
        return row.id

    # ------------------------------------------------------------------
    async def migrate_content(self, item: Dict[str, Any]) -> None:
        content_id = str(item.get("note_id") or item.get("aweme_id") or
                         item.get("video_id") or item.get("content_id") or "")
        if not content_id:
            self.stats["skipped"] += 1
            return

        exists = (await self.s.execute(
            select(StoryChapter).where(StoryChapter.source_site_id == self.site_id,
                                       StoryChapter.source_content_id == content_id)
        )).scalar_one_or_none()
        if exists:
            self.stats["skipped"] += 1
            self._chapter_by_content[content_id] = exists.id
            self._story_by_content[content_id] = exists.story_id
            return

        author_id = await self.get_author(str(item.get("creator_hash") or ""),
                                          str(item.get("nickname") or ""),
                                          str(item.get("author_avatar") or ""))
        like_count = safe_int(item.get("liked_count") or item.get("voteup_count") or 0)
        comment_count = safe_int(item.get("comment_count") or 0)
        publish_ts = safe_int(item.get("time") or item.get("create_time") or 0)

        story = Story(
            title=str(item.get("title") or "")[:500] or "（无标题）",
            summary=str(item.get("desc") or "")[:2000] or None,
            cover_url=None,
            dedupe_key=f"{self.site_code}:{content_id}",
            primary_site_id=self.site_id,
            author_id=author_id,
            chapter_count=1,
            comment_count=comment_count,
            like_count=like_count,
            first_publish_ts=publish_ts or None,
            last_update_ts=publish_ts or None,
            ext={"source_keyword": item.get("source_keyword", ""),
                 "legacy": {"xsec_token": item.get("xsec_token", "")}},
        )
        self.s.add(story)
        await self.s.flush()
        self.stats["story"] += 1

        images = split_list(item.get("image_list"))
        chapter = StoryChapter(
            story_id=story.id,
            source_site_id=self.site_id,
            author_id=author_id,
            source_content_id=content_id,
            source_url=str(item.get("note_url") or item.get("aweme_url") or "") or None,
            seq_no=1,
            title=str(item.get("title") or "")[:500] or None,
            content_text=str(item.get("desc") or "") or None,
            like_count=like_count,
            comment_count=comment_count,
            share_count=safe_int(item.get("share_count") or 0) or None,
            collect_count=safe_int(item.get("collected_count") or 0) or None,
            publish_ts=publish_ts or None,
            declared_comment_count=comment_count or None,
            ext={"type": item.get("type", ""), "video_url": item.get("video_url", "")},
        )
        self.s.add(chapter)
        await self.s.flush()
        self.stats["chapter"] += 1

        # 封面/图片
        for idx, url in enumerate(images[:9]):
            if not url.startswith(("http://", "https://")):
                continue
            self.s.add(MediaAttachment(
                target_type="chapter", target_id=chapter.id, media_type="image",
                source_url=url, sort_order=idx, download_status="pending",
            ))
            self.stats["media"] += 1

        # 标签（去重后写入，story_tag 主键是 (story_id, tag_id)）
        tag_ids: List[int] = []
        for tag_name in split_list(item.get("tag_list"))[:10]:
            tag_id = await self.get_tag(tag_name)
            if tag_id and tag_id not in tag_ids:
                tag_ids.append(tag_id)
        # 搜索关键词也作为标签
        for kw in split_list(item.get("source_keyword"))[:3]:
            tag_id = await self.get_tag(kw)
            if tag_id and tag_id not in tag_ids:
                tag_ids.append(tag_id)
        for tag_id in tag_ids:
            self.s.add(StoryTag(story_id=story.id, tag_id=tag_id))

        self._chapter_by_content[content_id] = chapter.id
        self._story_by_content[content_id] = story.id

    # ------------------------------------------------------------------
    async def migrate_comment(self, item: Dict[str, Any]) -> None:
        comment_id = str(item.get("comment_id") or "")
        if not comment_id:
            self.stats["skipped"] += 1
            return

        exists = (await self.s.execute(
            select(Comment).where(Comment.source_site_id == self.site_id,
                                  Comment.source_comment_id == comment_id)
        )).scalar_one_or_none()
        if exists:
            self.stats["skipped"] += 1
            return

        content_id = str(item.get("note_id") or "")
        story_id = self._story_by_content.get(content_id)
        chapter_id = self._chapter_by_content.get(content_id)

        if story_id is None:
            # 评论对应的内容不在本批（可能未迁移）：挂到一个兜底 story 上
            story_id = await self._ensure_orphan_story(content_id)
            chapter_id = None

        author_id = await self.get_author(str(item.get("creator_hash") or ""),
                                          str(item.get("nickname") or ""),
                                          str(item.get("author_avatar") or ""))
        self.s.add(Comment(
            story_id=story_id,
            chapter_id=chapter_id,
            source_site_id=self.site_id,
            author_id=author_id,
            source_comment_id=comment_id,
            parent_id=None,          # 第二阶段回填
            root_id=None,
            depth=0,
            content_text=str(item.get("content") or ""),
            like_count=safe_int(item.get("like_count") or 0),
            sub_count=safe_int(item.get("sub_comment_count") or 0),
            publish_ts=safe_int(item.get("create_time") or 0) or None,
        ))
        self.stats["comment"] += 1

    async def _ensure_orphan_story(self, content_id: str) -> int:
        """评论先于内容迁移时，为它建一个占位 story。"""
        dedupe = f"{self.site_code}:{content_id}"
        row = (await self.s.execute(
            select(Story).where(Story.dedupe_key == dedupe)
        )).scalar_one_or_none()
        if row:
            self._story_by_content[content_id] = row.id
            return row.id
        row = Story(title=f"（内容待补）{content_id}", dedupe_key=dedupe,
                    primary_site_id=self.site_id, status="active")
        self.s.add(row)
        await self.s.flush()
        self.stats["story"] += 1
        self._story_by_content[content_id] = row.id
        return row.id

    async def backfill_comment_tree(self) -> int:
        """回填 parent_id / root_id / depth（自关联，需第二阶段）。"""
        rows = (await self.s.execute(
            select(Comment).where(Comment.source_site_id == self.site_id)
        )).scalars().all()
        by_src = {c.source_comment_id: c for c in rows}
        updated = 0
        for c in rows:
            parent_src = None
            # 从 ext 拿不到 parent（迁移时未存），改用原始文件：见 migrate_file 中注入
            parent_src = (c.ext or {}).get("parent_comment_id")
            if not parent_src:
                c.root_id = c.id
                c.depth = 0
                updated += 1
                continue
            parent = by_src.get(str(parent_src))
            if parent is None:
                c.root_id = c.id
                c.depth = 0
            else:
                c.parent_id = parent.id
                c.root_id = parent.root_id or parent.id
                c.depth = int(parent.depth or 0) + 1
            c.ext = {}      # 用完即清；ext 是非空列，不能置 None
            updated += 1
        return updated


async def migrate_platform(platform: str) -> Dict[str, int]:
    engine = get_async_engine(resolve_config_db_type())
    if engine is None:
        raise RuntimeError("配置库不可用")

    totals: Dict[str, int] = {}
    async with AsyncSession(engine, expire_on_commit=False) as session:
        site = (await session.execute(
            select(SourceSite).where(SourceSite.code == platform)
        )).scalar_one_or_none()
        if not site:
            print(f"[skip] 未注册的站点：{platform}")
            await engine.dispose()
            return {}

        mig = Migrator(session, site.id, platform)
        files = sorted((DATA_DIR / platform / "jsonl").glob("*.jsonl"))
        if not files:
            files = sorted((DATA_DIR / platform).rglob("*.jsonl"))

        # 先内容，后评论（评论依赖内容建立 story 映射）
        ordered = sorted(files, key=lambda p: 0 if detect_kind(p.name) == "content" else 1)

        for path in ordered:
            kind = detect_kind(path.name)
            if kind is None:
                continue
            pending_parent: Dict[str, str] = {}
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if kind == "content":
                        await mig.migrate_content(item)
                    elif kind == "comment":
                        await mig.migrate_comment(item)
                        cid = str(item.get("comment_id") or "")
                        pid = str(item.get("parent_comment_id") or "")
                        if cid and pid:
                            pending_parent[cid] = pid
            if kind == "comment" and pending_parent:
                # 把父级信息写进 ext，供 backfill 使用
                for cid, pid in pending_parent.items():
                    row = (await session.execute(
                        select(Comment).where(Comment.source_site_id == site.id,
                                              Comment.source_comment_id == cid)
                    )).scalar_one_or_none()
                    if row is not None:
                        row.ext = {"parent_comment_id": pid}

        await session.commit()
        updated = await mig.backfill_comment_tree()
        await session.commit()
        totals = dict(mig.stats)
        totals["tree_backfilled"] = updated

    await engine.dispose()
    return totals


async def main(platforms: List[str]) -> None:
    for p in platforms:
        stats = await migrate_platform(p)
        if stats:
            print(f"[{p}] " + " ".join(f"{k}={v}" for k, v in stats.items()))


if __name__ == "__main__":
    args = sys.argv[1:]
    targets = args if args else ["xhs", "dy", "ks", "bili", "wb", "tieba", "zhihu"]
    import asyncio
    asyncio.run(main(targets))
