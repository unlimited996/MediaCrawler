# -*- coding: utf-8 -*-
"""HomeFeed（首页推荐流）抓取骨架。

各平台的推荐流接口差异大，且多数需要登录态 + 签名。这里提供统一入口与扩展点：
- 已接入的平台走 client 的推荐流接口
- 未接入的平台抛出明确错误，不静默返回空（避免"跑了但什么都没抓到"）

新增平台：在 SUPPORTED_HOMEFEED 里登记一个 async def(limit) -> List[dict] 即可。
"""
from __future__ import annotations

from typing import Any, Dict, List


async def _fetch_xhs_homefeed(limit: int = 20) -> List[Dict[str, Any]]:
    """小红书首页推荐流。需要登录态 + 签名，走项目自己的 client。"""
    raise NotImplementedError(
        "小红书 HomeFeed 需要登录态与签名，请通过抓取任务"
        "（crawler_type=homefeed）在已登录会话中执行")


# 平台 → 抓取函数
SUPPORTED_HOMEFEED: Dict[str, Any] = {
    "xhs": _fetch_xhs_homefeed,
}


async def fetch_homefeed(platform: str, limit: int = 20) -> List[Dict[str, Any]]:
    fn = SUPPORTED_HOMEFEED.get(platform)
    if fn is None:
        raise NotImplementedError(
            f"暂不支持该平台的 HomeFeed：{platform}，已登记 {list(SUPPORTED_HOMEFEED)}")
    return await fn(limit)


async def save_homefeed(platform: str, items: List[Dict[str, Any]],
                        task_id: int = None) -> int:
    """把 HomeFeed 内容写入 homefeed_content（按 (platform, content_id) 幂等）。"""
    from datetime import datetime

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from config.db_config import resolve_config_db_type
    from database.db_session import get_async_engine
    from database.models_v2 import HomefeedContent

    engine = get_async_engine(resolve_config_db_type())
    if engine is None:
        raise RuntimeError("配置库不可用")

    saved = 0
    async with AsyncSession(engine, expire_on_commit=False) as session:
        for it in items:
            content_id = str(it.get("content_id") or it.get("note_id") or "")
            if not content_id:
                continue
            exists = (await session.execute(
                select(HomefeedContent).where(HomefeedContent.platform == platform,
                                              HomefeedContent.content_id == content_id)
            )).scalar_one_or_none()
            if exists:
                continue
            session.add(HomefeedContent(
                platform=platform,
                content_id=content_id,
                title=it.get("title"),
                desc=it.get("desc"),
                author_hash=it.get("author_hash"),
                author_nickname=it.get("author_nickname"),
                cover_url=it.get("cover_url"),
                content_url=it.get("content_url"),
                liked_count=int(it.get("liked_count") or 0),
                comment_count=int(it.get("comment_count") or 0),
                share_count=it.get("share_count"),
                publish_ts=it.get("publish_ts"),
                task_id=task_id,
                raw_json=it.get("raw"),
            ))
            saved += 1
        await session.commit()

    await engine.dispose()
    return saved
