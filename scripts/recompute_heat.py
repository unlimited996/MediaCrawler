# -*- coding: utf-8 -*-
"""重算 story.heat_score（对外输出的排序核心字段）。

热度 = 点赞 + 评论×3 + 收藏×2 + 分享×2，并对发布时间做轻微时间衰减：
    最终 = 原始热度 / (1 + 天数/30)
这样新内容不至于被历史高赞内容永久压制。

用法：
    python scripts/recompute_heat.py
"""
import asyncio
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.db_config import resolve_config_db_type  # noqa: E402
from database.db_session import get_async_engine  # noqa: E402
from database.models_v2 import Story, StoryChapter  # noqa: E402
from tools.v2_sync import compute_heat_score  # noqa: E402


def _safe(v, default=0.0) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return default


async def recompute() -> int:
    engine = get_async_engine(resolve_config_db_type())
    if engine is None:
        raise RuntimeError("配置库不可用")

    updated = 0
    async with AsyncSession(engine, expire_on_commit=False) as session:
        stories = (await session.execute(
            select(Story).where(Story.deleted_at.is_(None))
        )).scalars().all()

        chapter_ids = {}
        for s in stories:
            rows = (await session.execute(
                select(StoryChapter).where(StoryChapter.story_id == s.id)
            )).scalars().all()
            chapter_ids[s.id] = rows

        for s in stories:
            chapters = chapter_ids.get(s.id) or []
            like = _safe(s.like_count)
            comment = _safe(s.comment_count)
            collect = sum(_safe(c.collect_count) for c in chapters)
            share = sum(_safe(c.share_count) for c in chapters)

            s.heat_score = compute_heat_score(
                like, comment, collect, share, s.first_publish_ts)
            updated += 1

        await session.commit()

    await engine.dispose()
    return updated


if __name__ == "__main__":
    n = asyncio.run(recompute())
    print(f"[recompute_heat] 更新 {n} 条 story")
