# -*- coding: utf-8 -*-
"""把 v2 `author` 表里的重复作者合并成一条，并把 story / chapter / comment
的 author_id 重指向保留行。

**默认只统计不写库（dry-run）**，确认数字无误后加 `--apply` 才真正执行。

重复的判定口径（任一相同即为同一人，与抓取时的自动合并一致）：
  * 同一平台下 `profile_url` 相同（最可靠：实时抓取必带真实主页链接）
  * 同一平台下 `anon_hash` 相同（唯一索引下正常不该出现，只兜历史脏数据）

合并规则：
  * 保留行：优先有头像 → 其次有昵称 → 最后 id 最小；
  * 先把重复行里有、保留行里没有的昵称/头像/主页/粉丝数**捞到保留行**（只填空，
    不覆盖已有值），再删重复行，避免「有头像的那行被删、头像永久丢失」；
  * 引用迁移：story.author_id / story_chapter.author_id / comment.author_id
    全部改指保留行，因此不会留悬空外键，也不会再出现「帖子挂在无头像老行上」。

用法：
    python scripts/merge_duplicate_authors.py                 # 全平台 dry-run
    python scripts/merge_duplicate_authors.py --platform xhs   # 只看小红书
    python scripts/merge_duplicate_authors.py --apply          # 真正执行
    python scripts/merge_duplicate_authors.py --apply --platform xhs
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.db_config import resolve_config_db_type  # noqa: E402
from database.db_session import get_async_engine  # noqa: E402
from database.models_v2 import (  # noqa: E402
    Author, Comment, SourceSite, Story, StoryChapter,
)
from tools.v2_sync import _merge_authors_into, _is_blank, _norm_profile_url  # noqa: E402


class _DSU:
    """并查集：同一（平台, 主页）或同一（平台, 哈希）的作者连成一个连通分量。"""

    def __init__(self) -> None:
        self.parent: Dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:          # 路径压缩
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _pick_keep(rows: List[Author]) -> Author:
    """保留行：优先有头像 → 有昵称 → id 最小。"""
    return min(rows, key=lambda a: (
        _is_blank(a.avatar_url),
        _is_blank(a.nickname),
        a.id,
    ))


async def _count_refs(session: AsyncSession, ids: List[int]) -> Tuple[int, int, int]:
    if not ids:
        return 0, 0, 0
    out = []
    for model in (Story, StoryChapter, Comment):
        out.append((await session.execute(
            select(func.count()).select_from(model)
            .where(model.author_id.in_(ids))
        )).scalar() or 0)
    return out[0], out[1], out[2]


async def run(platform: str | None, apply: bool) -> int:
    engine = get_async_engine(resolve_config_db_type())
    if engine is None:
        print("配置库不可用，请检查 .env 里的 POSTGRES_* 或 SAVE_DATA_OPTION")
        return 2

    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            sites = {s.id: s.code for s in (await session.execute(
                select(SourceSite))).scalars().all()}
            site_ids = [sid for sid, code in sites.items() if not platform or code == platform]
            if not site_ids:
                print(f"没有匹配的站点：{platform}")
                return 2

            rows = (await session.execute(
                select(Author).where(Author.source_site_id.in_(site_ids))
                .order_by(Author.id)
            )).scalars().all()
            total_authors = len(rows)

            # ---- 建连通分量 ----
            dsu = _DSU()
            by_url: Dict[Tuple[int, str], List[int]] = defaultdict(list)
            by_hash: Dict[Tuple[int, str], List[int]] = defaultdict(list)
            for a in rows:
                dsu.find(a.id)
                if not _is_blank(a.profile_url):
                    # 与抓取侧同一套归一化（去末尾斜杠），避免同一主页的两种写法
                    # 被当成两个人、越抓越多
                    by_url[(a.source_site_id, _norm_profile_url(a.profile_url))].append(a.id)
                if not _is_blank(a.anon_hash):
                    by_hash[(a.source_site_id, str(a.anon_hash).strip())].append(a.id)
            for bucket in (by_url, by_hash):
                for ids in bucket.values():
                    for other in ids[1:]:
                        dsu.union(ids[0], other)

            groups: Dict[int, List[Author]] = defaultdict(list)
            for a in rows:
                groups[dsu.find(a.id)].append(a)
            dup_groups = [g for g in groups.values() if len(g) > 1]

            print(f"平台：{platform or '全部'}｜作者总数 {total_authors}")
            print(f"重复作者组：{len(dup_groups)} 组，"
                  f"可合并掉 {sum(len(g) - 1 for g in dup_groups)} 行")

            empty_avatar_before = sum(1 for a in rows if _is_blank(a.avatar_url))
            plans = []
            for g in dup_groups:
                keep = _pick_keep(g)
                dupes = [a for a in g if a.id != keep.id]
                s_cnt, c_cnt, m_cnt = await _count_refs(session, [a.id for a in dupes])
                # 合并后能救回多少「空头像」作者：保留行有头像，而重复行没有
                rescued = sum(
                    1 for a in dupes
                    if _is_blank(a.avatar_url) and not _is_blank(keep.avatar_url))
                plans.append((keep, dupes, s_cnt, c_cnt, m_cnt, rescued))

            plans.sort(key=lambda p: -len(p[1]))
            print("\n前 10 组（按重复行数排序）：")
            for keep, dupes, s_cnt, c_cnt, m_cnt, rescued in plans[:10]:
                print(f"  keep={keep.id} ({keep.nickname or '-'}, "
                      f"头像{'有' if not _is_blank(keep.avatar_url) else '无'}) "
                      f"→ 合并 {len(dupes)} 行 {[d.id for d in dupes][:8]}"
                      f"{'…' if len(dupes) > 8 else ''}；"
                      f"牵动 story={s_cnt} chapter={c_cnt} comment={m_cnt}")
                print(f"        profile_url={keep.profile_url}")
            if len(plans) > 10:
                print(f"  … 其余 {len(plans) - 10} 组略")

            print(f"\n合计将删除重复作者行 {sum(len(p[1]) for p in plans)} 行，"
                  f"重指向 story {sum(p[2] for p in plans)} 条 / "
                  f"chapter {sum(p[3] for p in plans)} 条 / "
                  f"comment {sum(p[4] for p in plans)} 条")
            print(f"空头像作者：现在 {empty_avatar_before} 行 → "
                  f"预计 {empty_avatar_before - sum(p[5] for p in plans)} 行")

            if not apply:
                print("\n[dry-run] 未写库。确认无误后加 --apply 执行。")
                return 0
            if not plans:
                print("\n没有需要合并的重复作者。")
                return 0

            merged = 0
            failed = 0
            for keep, dupes, *_ in plans:
                try:
                    merged += await _merge_authors_into(
                        session, keep.id, [d.id for d in dupes])
                    await session.commit()
                except Exception as e:              # 一组失败不影响其它组
                    failed += 1
                    await session.rollback()
                    print(f"[fail] keep={keep.id} 合并失败：{e}")

            left = (await session.execute(
                select(func.count()).select_from(Author)
                .where(Author.source_site_id.in_(site_ids))
            )).scalar() or 0
            print(f"\n[done] 合并 {merged} 行，失败 {failed} 组；"
                  f"作者总数 {total_authors} → {left}")
            return 1 if failed else 0
    finally:
        await engine.dispose()


def main() -> int:
    ap = argparse.ArgumentParser(description="合并 v2 重复作者并重指向 story 引用")
    ap.add_argument("--platform", default=None,
                    help="只处理某个平台（xhs/dy/ks/bili/wb/tieba/zhihu），默认全部")
    ap.add_argument("--apply", action="store_true",
                    help="真正写库；不加则只统计（dry-run）")
    args = ap.parse_args()
    return asyncio.run(run(args.platform, args.apply))


if __name__ == "__main__":
    sys.exit(main())
