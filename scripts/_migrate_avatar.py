import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
from sqlalchemy import text
from database.db_session import get_async_engine

# v1 内容/评论表（加 author_avatar）
V1_TABLES = [
    "bilibili_video", "bilibili_video_comment",
    "douyin_aweme", "douyin_aweme_comment",
    "kuaishou_video", "kuaishou_video_comment",
    "weibo_note", "weibo_note_comment",
    "xhs_note", "xhs_note_comment",
    "tieba_note", "tieba_comment",
    "zhihu_content", "zhihu_comment",
]


async def main():
    e = get_async_engine('postgres')
    async with e.connect() as c:
        for t in V1_TABLES:
            exists = (await c.execute(text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name=:t AND column_name='author_avatar'"
            ), {"t": t})).scalar()
            if exists:
                print(f"  {t}: 已有 author_avatar，跳过")
                continue
            await c.execute(text(f'ALTER TABLE {t} ADD COLUMN author_avatar TEXT'))
            print(f"  {t}: ADD COLUMN author_avatar TEXT")

        # v2 author 表
        exists = (await c.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='author' AND column_name='avatar_url'"
        ))).scalar()
        if not exists:
            await c.execute(text('ALTER TABLE author ADD COLUMN avatar_url TEXT'))
            print("  author: ADD COLUMN avatar_url TEXT")
        else:
            print("  author: 已有 avatar_url，跳过")

        await c.commit()

        # 校验
        rows = (await c.execute(text(
            "SELECT table_name FROM information_schema.columns "
            "WHERE column_name IN ('author_avatar','avatar_url') "
            "AND table_schema='public' ORDER BY table_name"
        ))).fetchall()
        print(f"\n带头像列的表共 {len(rows)} 张：")
        for r in rows:
            print("  -", r[0])
    await e.dispose()


asyncio.run(main())
