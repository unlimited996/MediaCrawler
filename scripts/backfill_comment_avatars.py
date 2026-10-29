# -*- coding: utf-8 -*-
"""对一批小红书笔记重新抓取评论并落库，补齐 PDF 留档导入时丢失的数据。

为什么要这个脚本：
    9 帖（如 momo 的「男生视角怎么看？…」系列）最初来自 pdf_archive 导入。
    import_pdf_archive.py 当时把每条评论的 user_info.image（评论者头像）与
    pictures（评论图片）都置空了，导致 95% 的评论者没有头像、评论图也从未入库。
    实时抓取接口（/api/sns/web/v2/comment/*）的评论原始数据里带有
    userInfo.image 与 pictures —— 重新抓一遍评论，store 层（pick_avatar）会取出
    头像、pictures 会被登记进 media_attachment(target_type='comment') 并触发 S3 备份。

前置条件：
    - 需要登录态（cookie）。脚本走 cookie 模式：config.COOKIES 非空即可，
      无需启动浏览器。若 COOKIES 失效，评论接口会 403/登录墙，请先更新 cookie。
    - 需要能访问小红书（直连或系统代理）。

用法：
    # 按作者主页哈希回填其全部帖子评论（推荐）
    python scripts/backfill_comment_avatars.py --author-profile 5cc009750000000016009a5a

    # 指定若干 note_id
    python scripts/backfill_comment_avatars.py \
        --note-ids 69ee44ca000000002202596c,66abcdef0000000012345678

    # 控制单帖最大评论数（默认 100000，即全量）
    python scripts/backfill_comment_avatars.py --author-profile 5cc0097... --max-comments 5000
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 让脚本在仓库任意位置都能 import 到项目模块
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

import config  # noqa: E402
from tools import utils  # noqa: E402
from store import xhs as xhs_store  # noqa: E402
from media_platform.xhs.client import XiaoHongShuClient  # noqa: E402

# 复用 v2 同步的 DB 访问方式（与 API / 爬虫进程一致）
from database.db_session import get_async_engine  # noqa: E402
from config.db_config import resolve_config_db_type  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402


async def load_note_ids_from_db(author_profile: str, limit_comments: int) -> list[str]:
    """按作者主页哈希，从 v2 读出其全部 story 的 source_content_id。"""
    db_type = resolve_config_db_type()
    engine = get_async_engine(db_type)
    if engine is None:
        print("[backfill] 无法连接配置库，请检查 db_config")
        return []
    ids: list[str] = []
    try:
        async with AsyncSession(engine, expire_on_commit=False) as s:
            # author.profile_url 形如 https://www.xiaohongshu.com/user/profile/<hash>
            rows = (await s.execute(text(
                "SELECT c.source_content_id FROM story st "
                "JOIN author a ON st.author_id = a.id "
                "JOIN story_chapter c ON c.story_id = st.id "
                "WHERE a.profile_url LIKE :p"
            ), {"p": f"%{author_profile}%"})).all()
            ids = [r[0] for r in rows if r[0]]
    finally:
        await engine.dispose()
    print(f"[backfill] 作者 {author_profile} 命中 note 数: {len(ids)}")
    return ids


def build_xhs_client(cookie_str: str) -> XiaoHongShuClient:
    """用 cookie 以 cookie 模式构造客户端（不启动浏览器）。"""
    cookie_dict = utils.convert_str_cookie_to_dict(cookie_str)
    index_url = "https://www.xiaohongshu.com"
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9",
        "cache-control": "no-cache",
        "content-type": "application/json;charset=UTF-8",
        "origin": index_url,
        "pragma": "no-cache",
        "referer": f"{index_url}/",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
        "Cookie": cookie_str,
    }
    return XiaoHongShuClient(
        proxy=None, headers=headers, playwright_page=None, cookie_dict=cookie_dict
    )


async def resolve_cookie() -> str:
    """取小红书登录态 cookie：优先 config.COOKIES，其次 account 账号池。

    扫码登录（WebUI）成功后 cookie 会写入 account 账号池，因此走账号池兜底
    可以免去手动往 config.COOKIES 填值的步骤。
    """
    cookie_str = getattr(config, "COOKIES", "") or ""
    if cookie_str:
        return cookie_str
    try:
        from account import AccountPool

        pool = AccountPool()
        acc = await pool.pick("xhs")
        if acc and acc.get("cookie"):
            return acc["cookie"]
    except Exception as e:
        print(f"[backfill] 读取账号池 cookie 失败（将检查 config.COOKIES）: {e}")
    return ""


async def backfill_one(client: XiaoHongShuClient, note_id: str,
                       max_comments: int) -> int:
    """重新抓一篇笔记的评论并落库，返回落库评论条数（约）。"""
    # 先取详情拿 xsec_token（评论接口需要）；取不到就空着试一次
    xsec_token = ""
    try:
        card = await client.get_note_by_id(note_id, xsec_source="pc_search", xsec_token="")
        if isinstance(card, dict):
            xsec_token = card.get("xsec_token") or ""
    except Exception as e:
        print(f"  [warn] note {note_id} 详情/xsec_token 获取失败（继续尝试评论）: {e}")

    if not xsec_token:
        print(f"  [warn] note {note_id} 无 xsec_token，评论接口可能返回登录墙")

    fetched = await client.get_note_all_comments(
        note_id=note_id,
        xsec_token=xsec_token,
        crawl_interval=float(getattr(config, "XHS_SLEEP", 5.0) or 5.0),
        callback=xhs_store.batch_update_xhs_note_comments,
        max_count=max_comments,
    )
    return len(fetched or [])


async def main_async(author_profile: str, note_ids: list[str],
                     max_comments: int) -> None:
    if not note_ids and author_profile:
        note_ids = await load_note_ids_from_db(author_profile, max_comments)
    if not note_ids:
        print("[backfill] 没有可处理的 note_id，退出")
        return

    cookie_str = await resolve_cookie()
    if not cookie_str:
        print("[backfill] 未找到有效 Cookie（config.COOKIES 与账号池均为空）"
              "—— 评论接口需要登录态，请先扫码登录或配置 Cookie 后重试")
        return

    client = build_xhs_client(cookie_str)
    total = 0
    for i, note_id in enumerate(note_ids, 1):
        print(f"[{i}/{len(note_ids)}] 重新抓取评论 note_id={note_id}")
        try:
            n = await backfill_one(client, note_id, max_comments)
            total += n
            print(f"    -> 本轮拉回评论约 {n} 条（已落库并触发头像/图片补齐）")
        except Exception as e:
            print(f"    -> 失败: {type(e).__name__} {e}")
        # 笔记间也留点间隔，降低风控
        await asyncio.sleep(3.0)
    print(f"[backfill] 完成，累计拉回评论约 {total} 条")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="重新抓取评论以补齐头像/图片")
    p.add_argument("--author-profile", default="",
                   help="作者主页哈希，如 5cc009750000000016009a5a")
    p.add_argument("--note-ids", default="",
                   help="逗号分隔的 note_id 列表，优先于 --author-profile")
    p.add_argument("--max-comments", type=int, default=100000,
                   help="单帖最大评论数（默认全量）")
    p.add_argument("--with-sub", action="store_true",
                   help="同时抓取楼中楼(二级)评论，补齐更多评论者头像")
    a = p.parse_args()
    if a.note_ids:
        a._note_list = [x.strip() for x in a.note_ids.split(",") if x.strip()]
    else:
        a._note_list = []
    if a.with_sub:
        # 临时开启二级评论抓取（重抓按 source_comment_id 幂等，不会重复建行）
        config.ENABLE_GET_SUB_COMMENTS = True
    return a


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main_async(args.author_profile, args._note_list, args.max_comments))
