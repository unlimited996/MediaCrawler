# -*- coding: utf-8 -*-
"""登录态共享：把浏览器里登录得到的 Cookie 同步到账号池。

背景：登录在**浏览器进程**里完成（扫码/手机号），Cookie 落在 Playwright 的
user_data_dir；而热榜抓取、无浏览器模式等场景走的是 httpx，需要的是 Cookie 字符串。
两者不共享，所以热榜会 401。

解决办法：登录成功后把 Cookie 导出并加密写入 account 表，
之后无浏览器场景统一从账号池取（见 discovery/hot.py 的 _cookie_for）。
"""
from __future__ import annotations

import logging
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# 各平台判定"是否真的登录了"的关键 Cookie
LOGIN_KEYS = {
    "xhs": ("web_session", "a1"),
    "zhihu": ("d_c0", "z_c0"),
    "wb": ("SUB", "SUBP"),
    "dy": ("sessionid", "passport_csrf_token"),
    "bili": ("SESSDATA", "bili_jct"),
    "ks": ("kuaishou.server.web_st", "userId"),
    "tieba": ("BDUSS", "STOKEN"),
}


def looks_logged_in(platform: str, cookie_str: str) -> bool:
    """粗判 Cookie 里是否含该平台的登录凭据。"""
    if not cookie_str:
        return False
    keys = LOGIN_KEYS.get(platform)
    if not keys:
        return True  # 未知平台不拦截，交由实际请求验证
    low = cookie_str.lower()
    return any(k.lower() in low for k in keys)


async def sync_login_to_pool(
    platform: str,
    browser_context,
    urls: Optional[List[str]] = None,
    account_name: str = "default",
) -> Tuple[bool, str]:
    """把浏览器上下文的 Cookie 同步到账号池（加密存储）。

    Returns:
        (是否成功, 说明文案)
    """
    try:
        from tools import utils

        cookie_str, _ = await utils.convert_browser_context_cookies(
            browser_context, urls=urls or [])
    except Exception as e:
        return False, f"读取浏览器 Cookie 失败：{type(e).__name__}: {e}"

    if not cookie_str:
        return False, "浏览器里没有可用 Cookie"

    if not looks_logged_in(platform, cookie_str):
        return False, f"Cookie 里缺少 {platform} 的登录凭据，可能未登录成功"

    try:
        from account import AccountPool

        pool = AccountPool()
        await pool.add(platform, account_name, cookie_str, login_type="browser")
    except Exception as e:
        return False, f"写入账号池失败：{type(e).__name__}: {e}"

    logger.info(f"[cookie_sync] {platform} 登录态已同步到账号池（{account_name}）")
    return True, f"已把 {platform} 登录态同步到账号池"
