# -*- coding: utf-8 -*-
"""把 v1 遗留表（xhs_note 等）的存量记录回补进 v2 统一模型。

背景：v2_sync 是实时同步（2026-10-19 上线），之前 postgres 模式抓取的数据
只落在 v1 表里，数据管理页（读 v2）看不到。本脚本做一次性回补。

幂等：底层走 tools.v2_sync 的 upsert，可反复执行。

用法：
    python scripts/backfill_v1_to_v2.py            # 全部平台
    python scripts/backfill_v1_to_v2.py xhs        # 只回补小红书
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from database.db_session import get_async_engine  # noqa: E402
from database.models import (  # noqa: E402
    BilibiliVideo, BilibiliVideoComment, DouyinAweme, DouyinAwemeComment,
    KuaishouVideo, KuaishouVideoComment, WeiboNote, WeiboNoteComment,
    XhsNote, XhsNoteComment,
)
from tools.v2_sync import persist_to_v2  # noqa: E402

# v1 ORM 实例 -> store 层拍平 dict 的字段名映射（与 store/<platform>/__init__.py 写入时一致）
_V1_CONTENT_MAP = {
    "xhs": (XhsNote, ["note_id", "title", "desc", "time", "last_update_time",
                      "creator_hash", "nickname", "author_avatar", "liked_count",
                      "collected_count", "comment_count", "share_count",
                      "image_list", "tag_list", "note_url", "source_keyword",
                      "xsec_token"]),
    "dy": (DouyinAweme, ["aweme_id", "title", "desc", "create_time",
                         "creator_hash", "nickname", "author_avatar", "liked_count",
                         "comment_count", "share_count", "image_list", "tag_list",
                         "aweme_url", "source_keyword"]),
    "ks": (KuaishouVideo, ["video_id", "title", "desc", "create_time",
                           "creator_hash", "nickname", "author_avatar", "liked_count",
                           "comment_count", "share_count", "image_list", "tag_list",
                           "video_url", "source_keyword"]),
    "bili": (BilibiliVideo, ["video_id", "title", "desc", "create_time",
                             "creator_hash", "nickname", "author_avatar", "liked_count",
                             "comment_count", "share_count", "image_list", "tag_list",
                             "video_url", "source_keyword"]),
    "wb": (WeiboNote, ["note_id", "content", "create_time", "creator_hash",
                       "nickname", "author_avatar", "liked_count", "comment_count",
                       "share_count", "image_list", "tag_list", "note_url",
                       "source_keyword"]),
}
_V1_COMMENT_MAP = {
    "xhs": (XhsNoteComment, ["comment_id", "note_id", "content", "create_time",
                             "creator_hash", "nickname", "author_avatar",
                             "like_count", "sub_comment_count", "parent_comment_id"]),
    "dy": (DouyinAwemeComment, ["comment_id", "aweme_id", "content", "create_time",
                                "creator_hash", "nickname", "author_avatar",
                                "like_count", "sub_comment_count", "parent_comment_id"]),
    "ks": (KuaishouVideoComment, ["comment_id", "video_id", "content", "create_time",
                                  "creator_hash", "nickname", "author_avatar",
                                  "like_count", "sub_comment_count", "parent_comment_id"]),
    "bili": (BilibiliVideoComment, ["comment_id", "video_id", "content", "create_time",
                                    "creator_hash", "nickname", "author_avatar",
                                    "like_count", "sub_comment_count", "parent_comment_id"]),
    "wb": (WeiboNoteComment, ["comment_id", "note_id", "content", "create_time",
                              "creator_hash", "nickname", "author_avatar",
                              "like_count", "sub_comment_count", "parent_comment_id"]),
}


def _to_dict(row, fields: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for f in fields:
        v = getattr(row, f, None)
        if v is None:
            continue
        # v1 里 image_list / tag_list 存的是 json 字符串，还原成逗号串（store 层格式）
        if f in ("image_list", "tag_list") and isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    v = ",".join(str(x) for x in parsed)
            except (ValueError, TypeError):
                pass
        out[f] = v
    return out


async def backfill_platform(platform: str) -> Dict[str, int]:
    stats = {"content": 0, "comment": 0}
    content_cls, content_fields = _V1_CONTENT_MAP[platform]
    comment_cls, comment_fields = _V1_COMMENT_MAP[platform]
    engine = get_async_engine("postgres")
    if engine is None:
        print(f"[skip] {platform}: postgres 不可用")
        return stats

    from sqlalchemy.ext.asyncio import AsyncSession
    async with AsyncSession(engine, expire_on_commit=False) as session:
        rows = (await session.execute(select(content_cls))).scalars().all()
        for row in rows:
            item = _to_dict(row, content_fields)
            if await persist_to_v2(platform, item, "content"):
                stats["content"] += 1
        rows = (await session.execute(select(comment_cls))).scalars().all()
        for row in rows:
            item = _to_dict(row, comment_fields)
            if await persist_to_v2(platform, item, "comment"):
                stats["comment"] += 1
    return stats


async def main(platforms: List[str]) -> None:
    for p in platforms:
        if p not in _V1_CONTENT_MAP:
            print(f"[skip] {p}: 未支持的平台（tieba/zhihu 走 pydantic 模型，无 v1 表）")
            continue
        stats = await backfill_platform(p)
        print(f"[done] {p}: 内容 {stats['content']} 条，评论 {stats['comment']} 条")


if __name__ == "__main__":
    args = sys.argv[1:]
    asyncio.run(main(args or list(_V1_CONTENT_MAP.keys())))
