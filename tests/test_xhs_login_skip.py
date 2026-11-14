"""验证 XiaoHongShuCrawler.start() 的登录分支控制流（mock 掉浏览器客户端）。

核心回归：浏览器上下文已是登录态、但 pong() 偶发失败时，必须跳过整个登录校验
（不能调用 begin / clear_cookies / login_by_qrcode），直接进 _dispatch_crawl。
否则会在非 headless 模式下弹出二维码等扫码，任务卡死还不抓数据。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from media_platform.xhs.core import XiaoHongShuCrawler


def _build(logged_in, pong_val):
    crawler = XiaoHongShuCrawler()
    crawler._browser_required = AsyncMock(return_value=True)
    crawler.launch_browser_with_cdp = AsyncMock()
    crawler.launch_browser = AsyncMock()
    crawler._goto_index_page = AsyncMock()
    crawler.create_xhs_client = AsyncMock()
    crawler._browser_logged_in = AsyncMock(return_value=logged_in)
    crawler._dispatch_crawl = AsyncMock()
    crawler._human_verify = AsyncMock()

    client = MagicMock()
    client.pong = AsyncMock(side_effect=lambda: pong_val)
    client.update_cookies = AsyncMock()
    crawler.create_xhs_client.return_value = client
    crawler.xhs_client = client

    ctx = MagicMock()
    ctx.new_page = AsyncMock(return_value=MagicMock())
    ctx.cookies = AsyncMock(return_value=[{"name": "web_session", "value": "x"}])
    ctx.clear_cookies = AsyncMock()
    ctx.reload = AsyncMock()
    ctx.add_cookies = AsyncMock()
    crawler.launch_browser_with_cdp.return_value = ctx
    crawler.launch_browser.return_value = ctx
    crawler.browser_context = ctx
    crawler.context_page = MagicMock()
    return crawler, client, ctx


async def _call(crawler):
    counter = {"begin": 0, "login_by_qrcode": 0, "clear_cookies": 0}
    with patch("playwright.async_api.async_playwright") as ap, \
         patch("media_platform.xhs.core.XiaoHongShuLogin") as LoginCls, \
         patch("tools.cookie_sync.sync_login_to_pool",
               new_callable=AsyncMock, return_value=(True, "ok")):
        fake_pw = MagicMock()
        ap.return_value.__aenter__.return_value = fake_pw
        ap.return_value.__aexit__.return_value = False
        inst = LoginCls.return_value
        inst.begin = AsyncMock(
            side_effect=lambda: counter.__setitem__("begin", counter["begin"] + 1))
        inst.login_by_qrcode = AsyncMock(
            side_effect=lambda: counter.__setitem__(
                "login_by_qrcode", counter["login_by_qrcode"] + 1))
        await crawler.start()
    return counter


def test_logged_in_skips_login_and_qrcode():
    crawler, client, ctx = _build(logged_in=True, pong_val=False)
    counter = asyncio.run(_call(crawler))
    assert counter["begin"] == 0, "已登录不应调用 begin()"
    assert counter["login_by_qrcode"] == 0, "已登录不应弹二维码等扫码"
    assert counter["clear_cookies"] == 0, "已登录不应 clear_cookies（否则会清掉有效登录态）"
    assert crawler._dispatch_crawl.await_count == 1, "已登录应直接进入抓取"


def test_not_logged_in_runs_login_and_qrcode():
    crawler, client, ctx = _build(logged_in=False, pong_val=False)
    counter = asyncio.run(_call(crawler))
    assert counter["begin"] == 1, "未登录应调用 begin() 走常规登录流程"
    assert ctx.clear_cookies.await_count == 1, "未登录且非 headless 应清掉失效 Cookie 后弹码"
    assert counter["login_by_qrcode"] == 1, "未登录且非 headless 应回退扫码"
    assert crawler._dispatch_crawl.await_count == 1, "登录流程结束后应进入抓取"


def test_pong_ok_goes_straight_to_crawl():
    crawler, client, ctx = _build(logged_in=True, pong_val=True)
    counter = asyncio.run(_call(crawler))
    assert counter["begin"] == 0
    assert counter["login_by_qrcode"] == 0
    assert counter["clear_cookies"] == 0
    assert crawler._dispatch_crawl.await_count == 1
