# -*- coding: utf-8 -*-
"""等待 xhs 登录态恢复后，自动补齐指定笔记的评论头像（含楼中楼）。

为什么需要这个脚本：
    小红书 cookie 会话易过期。backfill_comment_avatars.py 需要有效登录态才能
    抓评论、回填 Author.avatar_url。当用户在 WebUI 重新扫码登录后，cookie 会
    同步进账号池；本脚本轮询检测登录态，一旦恢复就自动触发回填，免去手动等待。

用法：
    python scripts/backfill_avatars_wait_login.py \
        --note-ids 69c401e30000000023014a21 \
        --wait 1200          # 最多等待秒数（默认 1200=20 分钟）
        --interval 30        # 轮询间隔秒数（默认 30）
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env", override=False)

import config  # noqa: E402
from tools import utils  # noqa: E402
from media_platform.xhs.client import XiaoHongShuClient  # noqa: E402


async def _is_logged_in() -> bool:
    """用账号池最新的 cookie 检测 xhs 登录态。每次都重读账号池，保证拿到刷新后的 cookie。"""
    try:
        from account import AccountPool
        pool = AccountPool()
        acc = await pool.pick("xhs")
        cookie_str = (acc or {}).get("cookie") or ""
    except Exception:
        cookie_str = ""
    if not cookie_str:
        return False
    try:
        cookie_dict = utils.convert_str_cookie_to_dict(cookie_str)
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9",
            "content-type": "application/json;charset=UTF-8",
            "origin": "https://www.xiaohongshu.com",
            "referer": "https://www.xiaohongshu.com/",
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
            "Cookie": cookie_str,
        }
        client = XiaoHongShuClient(
            proxy=None, headers=headers, playwright_page=None, cookie_dict=cookie_dict
        )
        return bool(await client.pong())
    except Exception:
        return False


async def main_async(note_ids: list[str], wait: int, interval: int) -> None:
    print(f"[wait_login] 开始轮询登录态（最多等待 {wait}s，间隔 {interval}s）")
    print("[wait_login] 请在 WebUI 重新扫码登录小红书，本脚本检测到登录态后会自动回填头像。")
    deadline = time.time() + wait
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        ok = await _is_logged_in()
        if ok:
            print(f"[wait_login] 第 {attempt} 次检测：登录态已恢复 ✅ 开始回填头像…")
            # 开启楼中楼抓取，复用 backfill_comment_avatars 的主流程
            config.ENABLE_GET_SUB_COMMENTS = True
            from scripts.backfill_comment_avatars import main_async as backfill_main
            await backfill_main("", note_ids, 100000)
            print("[wait_login] 头像回填流程结束。")
            return
        remain = int(deadline - time.time())
        print(f"[wait_login] 第 {attempt} 次检测：尚未登录（剩余 {remain}s）")
        await asyncio.sleep(interval)

    print("[wait_login] 等待超时仍未检测到有效登录态。请确认已在 WebUI 重新扫码登录，"
          "然后直接运行：")
    print(f"  python scripts/backfill_comment_avatars.py --note-ids {','.join(note_ids)} --with-sub")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="等待登录态后自动补齐评论头像")
    p.add_argument("--note-ids", default="",
                   help="逗号分隔的 note_id 列表")
    p.add_argument("--wait", type=int, default=1200,
                   help="最多等待秒数（默认 1200）")
    p.add_argument("--interval", type=int, default=30,
                   help="轮询间隔秒数（默认 30）")
    a = p.parse_args()
    a._note_list = [x.strip() for x in a.note_ids.split(",") if x.strip()] if a.note_ids else []
    return a


if __name__ == "__main__":
    args = parse_args()
    if not args._note_list:
        print("[wait_login] 必须通过 --note-ids 指定至少一个笔记")
        sys.exit(1)
    asyncio.run(main_async(args._note_list, args.wait, args.interval))
