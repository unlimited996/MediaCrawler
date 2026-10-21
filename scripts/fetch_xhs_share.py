# -*- coding: utf-8 -*-
"""小红书「分享链接」免登录抓取 / 探测工具。

用途
----
1. 给一条（或多条）小红书分享链接，免登录抓正文+图片+作者并入库：

       python scripts/fetch_xhs_share.py "https://www.xiaohongshu.com/explore/xxx?xsec_token=..."
       python scripts/fetch_xhs_share.py --file urls.txt
       python scripts/fetch_xhs_share.py --from-db            # 把库里所有 xhs 链接跑一遍

2. 只探测链接当前能不能免登录打开（不入库）：

       python scripts/fetch_xhs_share.py --probe "https://..."
       python scripts/fetch_xhs_share.py --probe --from-db

重要前提（实测结论）
--------------------
能不能免登录看，只取决于 URL 里 **xsec_token 是否仍然有效**，
与链接是 app_share 还是 pc_search 格式**无关**（30 条历史链接 A/B 对照，结果完全一致）。
token 由平台签发、有时效，未登录状态下无法自己换发
（搜索/首页接口一律返回 code=-101 无登录信息）。
所以：历史链接 99% 会因 token 过期撞登录墙，需要拿到**新的分享链接**才能抓。
"""
import argparse
import asyncio
import os
import socket
import sys
import urllib.parse
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from media_platform.xhs.ssr import probe_url, fetch_and_store


# --------------------------------------------------------------------------- #
# 配置库连接（与 api/routers/story.py 保持一致：直接按 CONFIG_DB_TYPE 连）
# --------------------------------------------------------------------------- #
def _db_url() -> str:
    from config.db_config import postgres_db_config as c
    ip = socket.gethostbyname(c["host"])  # 沙箱里 DNS 偶发失败，先解析成 IP
    pwd = urllib.parse.quote_plus(str(c["password"]).strip('"'))
    usr = urllib.parse.quote_plus(c["user"])
    return "postgresql+asyncpg://%s:%s@%s:%s/%s" % (usr, pwd, ip, c["port"], c["db_name"])


async def load_urls_from_db(limit: int = 0) -> List[str]:
    """取出库里所有 xhs 章节的原文链接（source_url 优先，其次用 ext 里的 token 拼）。"""
    from media_platform.xhs.ssr import build_explore_url

    eng = create_async_engine(_db_url(), pool_size=1, max_overflow=0)
    sql = """
        SELECT sc.id, sc.story_id, sc.source_content_id, sc.source_url,
               coalesce(sc.ext->>'xsec_token','')
        FROM story_chapter sc JOIN source_site ss ON ss.id = sc.source_site_id
        WHERE ss.code = 'xhs' AND sc.deleted_at IS NULL
        ORDER BY sc.id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    async with eng.connect() as conn:
        rows = (await conn.execute(text(sql))).all()
    await eng.dispose()

    urls = []
    for _cid, _sid, note_id, src_url, ext_token in rows:
        if src_url:
            urls.append(src_url)
        elif ext_token:
            urls.append(build_explore_url(note_id, ext_token, "pc_search"))
    return urls


# --------------------------------------------------------------------------- #
async def run_probe(urls: List[str], delay: float = 1.2) -> None:
    ok = fail = 0
    kinds: dict = {}
    for i, u in enumerate(urls, 1):
        r = await probe_url(u)
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
        flag = "OK  " if r["ok"] else "FAIL"
        print(f"[{i:3d}] {flag} {r['kind']:<10} {r['note_id'][:16]:<16} {r['detail']}")
        ok += 1 if r["ok"] else 0
        fail += 0 if r["ok"] else 1
        await asyncio.sleep(delay)
    print(f"\n可访问 {ok} 条 / 不可访问 {fail} 条")
    print("失败原因分布:", kinds)


async def run_fetch(urls: List[str], delay: float = 1.5) -> None:
    # 独立进程里 config.SAVE_DATA_OPTION 默认是 jsonl，按 .env 的 CONFIG_DB_TYPE 拉齐
    import config
    db_type = os.getenv("CONFIG_DB_TYPE") or ""
    if db_type:
        config.SAVE_DATA_OPTION = db_type
    print(f"落库方式: {config.SAVE_DATA_OPTION}")

    ok = fail = 0
    for i, u in enumerate(urls, 1):
        try:
            data = await fetch_and_store(u)
            ni = data["note_item"]
            print(f"[{i:3d}] OK   {data['note_id']} 《{ni['title'][:28]}》 "
                  f"@{ni['user'].get('nickname')} 图{len(ni['image_list'])} "
                  f"评论{data['stored_comments']}")
            ok += 1
        except Exception as e:
            kind = getattr(e, "kind", type(e).__name__)
            print(f"[{i:3d}] FAIL {kind:<10} {str(e)[:90]}")
            fail += 1
        await asyncio.sleep(delay)
    print(f"\n成功 {ok} 条 / 失败 {fail} 条")


def parse_args(argv: Optional[List[str]] = None):
    p = argparse.ArgumentParser(
        description="小红书分享链接免登录抓取 / 探测",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("urls", nargs="*", help="一个或多个小红书链接")
    p.add_argument("--file", help="从文件读取链接（一行一条）")
    p.add_argument("--from-db", action="store_true", help="用库里所有 xhs 章节链接")
    p.add_argument("--probe", action="store_true", help="只探测可访问性，不入库")
    p.add_argument("--limit", type=int, default=0, help="--from-db 时限制条数")
    p.add_argument("--delay", type=float, default=1.2, help="每次请求间隔秒（防风控）")
    return p.parse_args(argv)


def collect_urls(a) -> List[str]:
    urls = list(a.urls)
    if a.file:
        with open(a.file, encoding="utf-8") as f:
            urls += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    return urls


async def amain(a) -> int:
    if a.from_db:
        urls = await load_urls_from_db(a.limit)
        print(f"从库里取到 {len(urls)} 条 xhs 链接")
    else:
        urls = collect_urls(a)
    if not urls:
        print("没有可处理的链接（用 --file / --from-db 或直接传链接）")
        return 1
    if a.probe:
        await run_probe(urls, a.delay)
    else:
        await run_fetch(urls, a.delay)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain(parse_args())))
