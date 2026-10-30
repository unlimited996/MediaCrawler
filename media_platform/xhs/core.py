# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/xhs/core.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#

# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

import asyncio
import os
import random
from asyncio import Task
from typing import Callable, Dict, List, Optional
from urllib.parse import quote

from tenacity import RetryError

# Playwright 只在真正需要浏览器时才导入 —— 无浏览器模式（#3）下不装也能跑
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查
    from playwright.async_api import BrowserContext, BrowserType, Page, Playwright
else:
    BrowserContext = BrowserType = Page = Playwright = object  # 运行时占位

import config
from config.runtime_config import runtime_config
from base.base_crawler import AbstractCrawler
from media_downloader import MediaDownloader
from model.m_xiaohongshu import NoteUrlInfo, CreatorUrlInfo
from proxy.proxy_ip_pool import IpInfoModel, create_ip_pool
from store import xhs as xhs_store
from tools import anti_risk, utils
from tools.crawler_util import chromium_launch_kwargs, system_playwright_proxy
from tools.cdp_browser import CDPBrowserManager
from var import crawler_type_var, source_keyword_var

from . import media as xhs_media
from .client import XiaoHongShuClient
from .exception import (
    CaptchaError,
    DataFetchError,
    IPBlockError,
    NoteHiddenError,
    NoteNotFoundError,
    PlatformAccessError,
)
from .field import SearchSortType
from .help import parse_note_info_from_note_url, parse_creator_info_from_url, get_search_id
from .login import XiaoHongShuLogin


def _xhs_concurrency() -> int:
    """小红书并发数：默认恒为 1（串行请求最像真人，风控最低）。"""
    return max(1, int(getattr(config, "XHS_MAX_CONCURRENCY_NUM",
                              config.MAX_CONCURRENCY_NUM) or 1))


def _xhs_crawl_interval():
    """小红书请求间隔规格：(lo, hi) 随机区间，默认 5-15s。"""
    return anti_risk.xhs_sleep_bounds()


def _xhs_captcha_wait_sec() -> int:
    return int(getattr(config, "XHS_CAPTCHA_WAIT_SEC", 300) or 300)


def update_idle_streak(idle_pages: Dict[str, int], behavior: str, queued: int,
                       early_stop_pages: int) -> bool:
    """更新「连续空转页」计数，返回是否应提前停止翻页。

    account 模式的点赞/收藏列表按时间倒序（新帖在最前），一旦连续若干页
    都没有「新增待抓帖」（全是已归档/视频），后面的只会更老 —— 提前收工，
    避免每次任务都把整份列表从头翻一遍。未完成的帖子由「按 note_id 直连
    续抓」兜底，不依赖列表发现，所以早停不会漏掉待补帖。

    early_stop_pages <= 0 表示关闭早停（永远返回 False）。
    """
    if queued == 0:
        idle_pages[behavior] = idle_pages.get(behavior, 0) + 1
    else:
        idle_pages[behavior] = 0
    return (early_stop_pages > 0
            and idle_pages.get(behavior, 0) >= early_stop_pages)


def comment_fetch_order(progress: Dict[str, tuple],
                        declared_map: Dict[str, int]) -> Callable[[str], tuple]:
    """评论抓取排序键：**快抓完的先抓**，让一轮能真正收口若干篇。

    背景（2026-10-29 用户反馈）：原先按列表原始顺序串行抓，被风控打断时
    结果是「每篇都抓了一点」（0%→40%、0%→1%、其余全 0%），一轮下来
    没有任何一篇是完整的，下一轮还得从头摊一遍。
    改成按完成度降序后：接近完成的先抓完落库，风控打断时也已经有一批
    帖子是完整的；未完成的靠断点续抓继续，不会被摊薄。

    返回的 key 元组依次为：
      0) 有已抓进度的排最前，其次有声明数但还没开抓的，最后是声明数缺失的；
      1) 完成度（取负 → 越大越靠前，即「缺口越小越优先」）；
      2) 声明数（同完成度时，评论区小、更容易收口的先抓）。
    """
    def _key(content_id: str) -> tuple:
        fetched, declared = progress.get(str(content_id), (0, 0))
        declared = int(declared or declared_map.get(str(content_id), 0) or 0)
        if declared <= 0:
            # 没有声明数（= 没有评论需求 / 数据缺失）：排最后
            return (2, 0.0, 0)
        ratio = min(1.0, fetched / float(declared))
        return (0 if fetched > 0 else 1, -ratio, declared)

    return _key


def _comment_max_count(declared: int) -> int:
    """单篇笔记抓多少条一级评论（0 = 不限制，翻到 has_more=False）。

    目标是把评论抓到「原帖显示评论量」的 90% 以上（见
    tools/v2_sync.COMMENT_COMPLETENESS_TARGET / get_archived_content_ids），
    所以默认按声明数抓，而不是固定 30 条 —— 固定值会让热门帖永远不达标
    或者反过来（抓够了仍被跳过）。
      - declared > 0：declared + 余量（平台显示数常含已删/折叠评论）
      - declared == 0（详情没拿到统计）：不限制，交给 has_more
      - XHS_COMMENT_MAX_PER_NOTE > 0：再压一道硬顶，控制单篇耗时
    """
    margin = runtime_config.get_int("xhs_comment_count_margin", config.XHS_COMMENT_COUNT_MARGIN)
    hard_cap = runtime_config.get_int("xhs_comment_max_per_note", config.XHS_COMMENT_MAX_PER_NOTE)
    limit = (declared + margin) if declared > 0 else 0
    if hard_cap > 0:
        limit = min(limit, hard_cap) if limit > 0 else hard_cap
    return limit


class XiaoHongShuCrawler(AbstractCrawler):
    # 这些是运行期注入的句柄。类级默认 None + __init__/launch 赋值：
    # 单测常用 `XiaoHongShuCrawler.__new__()` 绕开 __init__ 造对象，
    # 没有类默认值就会 AttributeError（曾把 access_error 用例打挂）。
    context_page: Optional[Page] = None
    xhs_client: Optional[XiaoHongShuClient] = None
    browser_context: Optional[BrowserContext] = None
    cdp_manager: Optional[CDPBrowserManager] = None

    # 风控熔断状态。类级默认值 + __init__ 里重置：
    # 单测常用 `XiaoHongShuCrawler.__new__()` 绕开 __init__ 造对象，
    # 没有类默认值就会 AttributeError（曾把 4 个 access_error 用例打挂）。
    _risk_abort_reason: str = ""
    _comment_fail_streak: int = 0
    _comment_done_count: int = 0
    # 人工过验证：每轮只弹一次浏览器，避免连着几篇 461 反复弹窗打断用户
    _human_verify_tried: bool = False
    # 浏览器**实际**是否无头（启动时记录）。不能只看配置推断：
    # CDP 模式常配 XHS_CDP_HEADLESS=False，但 CDP 连不上时会回退到标准模式，
    # 那里 headless 可能仍是 True —— 结果是「以为窗口可见、其实什么都没有」，
    # 撞验证码时用户看不到任何窗口（2026-10-29 踩过）。
    _browser_headless_actual: Optional[bool] = None
    # 无浏览器模式下临时拉起的 playwright 句柄（用完要 stop，否则泄漏 driver 进程）
    _verify_playwright = None

    def __init__(self) -> None:
        self.index_url = "https://www.rednote.com" if config.XHS_INTERNATIONAL else "https://www.xiaohongshu.com"
        self.cookie_urls = [self.index_url]
        # self.user_agent = utils.get_user_agent()
        self.user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        self.cdp_manager = None
        self.ip_proxy_pool = None  # Proxy IP pool for automatic proxy refresh
        self._media_downloader: Optional[MediaDownloader] = None
        self.task_id: Optional[int] = None  # 断点续爬任务 ID（未开启时为 None）
        # 风控熔断：评论接口连续失败到上限就整批停手
        self._risk_abort_reason: str = ""
        self._comment_fail_streak: int = 0
        self._comment_done_count: int = 0
        self._human_verify_tried: bool = False
        self._browser_headless_actual = None

    async def start(self) -> None:
        playwright_proxy_format, httpx_proxy_format = None, None
        if config.ENABLE_IP_PROXY:
            self.ip_proxy_pool = await create_ip_pool(config.IP_PROXY_POOL_COUNT, enable_validate_ip=True)
            ip_proxy_info: IpInfoModel = await self.ip_proxy_pool.get_proxy()
            playwright_proxy_format, httpx_proxy_format = utils.format_proxy_info(ip_proxy_info)
        else:
            # 未配代理池时回退系统代理：Chromium 不读 http_proxy 环境变量，
            # 不显式传代理会导致浏览器打不开目标站点（Page.goto 超时）
            playwright_proxy_format = system_playwright_proxy()
            if playwright_proxy_format:
                utils.logger.info(f"[{self.__class__.__name__}] 使用系统代理: {playwright_proxy_format['server']}")
                # TCP 端口在监听 ≠ 代理能出网：代理软件没开上游 / 规则拦截时，
                # Chromium 会一路卡到 Page.goto 超时，整个抓取崩在第一步。
                # 先用 httpx 花几秒探一下，不通就让浏览器直连。
                if not await self._probe_reachable(playwright_proxy_format):
                    if await self._probe_reachable(None):
                        utils.logger.warning(
                            f"[{self.__class__.__name__}] 系统代理 {playwright_proxy_format['server']} "
                            f"无法访问 {self.index_url}，但直连可用 —— 本次改用直连")
                        playwright_proxy_format = None
                        self._disable_system_proxy_env()
                    else:
                        utils.logger.warning(
                            f"[{self.__class__.__name__}] 系统代理 {playwright_proxy_format['server']} "
                            f"与直连均无法访问 {self.index_url}：请检查网络或代理软件")

        # 无浏览器模式（#3 去 Playwright）：直接用 Cookie 发请求，不启动任何浏览器
        # 注意：_browser_required 是 async，漏了 await 会拿到协程对象（恒为真），
        # 无浏览器模式永远进不去，还会抛 "coroutine was never awaited"。
        if not await self._browser_required():
            utils.logger.info("[XiaoHongShuCrawler] 无浏览器模式：直接使用配置的 Cookie")
            self.browser_context = None
            self.context_page = None
            self.xhs_client = await self.create_xhs_client(httpx_proxy_format)
            if not await self.xhs_client.pong():
                raise RuntimeError(
                    "无浏览器模式下登录态无效：请先在配置里填入有效 Cookie "
                    "（config.COOKIES 或配置表 crawl.cookies），或关闭无浏览器模式改用扫码登录")
            await self._dispatch_crawl()
            return

        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            # Choose launch mode based on configuration
            # 小红书默认走 CDP：连接本地真实浏览器，复用登录态/扩展/历史，
            # 不带 Playwright 自动化标识（--enable-automation 等已在启动参数剔除）
            use_cdp = bool(getattr(config, "XHS_ENABLE_CDP_MODE",
                                   config.ENABLE_CDP_MODE))
            if use_cdp:
                utils.logger.info(
                    "[XiaoHongShuCrawler] Launching browser using CDP mode "
                    "(连接本地真实浏览器，复用登录态)")
                self.browser_context = await self.launch_browser_with_cdp(
                    playwright,
                    playwright_proxy_format,
                    self.user_agent,
                    headless=bool(getattr(config, "XHS_CDP_HEADLESS",
                                          config.CDP_HEADLESS)),
                    connect_existing=bool(getattr(
                        config, "XHS_CDP_CONNECT_EXISTING",
                        config.CDP_CONNECT_EXISTING)),
                    auto_close=bool(getattr(config, "XHS_AUTO_CLOSE_BROWSER",
                                           config.AUTO_CLOSE_BROWSER)),
                )
                self._browser_headless_actual = bool(
                    getattr(config, "XHS_CDP_HEADLESS", config.CDP_HEADLESS))
            else:
                utils.logger.info("[XiaoHongShuCrawler] Launching browser using standard mode")
                # Launch a browser context.
                chromium = playwright.chromium
                self.browser_context = await self.launch_browser(
                    chromium,
                    playwright_proxy_format,
                    self.user_agent,
                    headless=config.HEADLESS,
                )
                # 标准模式（CDP 连不上时的回退路径）实际 headless 由 config.HEADLESS
                # 决定。必须记下来：CDP 配置里的 XHS_CDP_HEADLESS=False 在这条路径上
                # 根本不生效，只按配置推断会以为窗口可见，实际是无头的（看不见）。
                self._browser_headless_actual = bool(config.HEADLESS)
                # stealth.min.js is a js script to prevent the website from detecting the crawler.
                await self.browser_context.add_init_script(path="libs/stealth.min.js")

            self.context_page = await self.browser_context.new_page()
            await self._goto_index_page()

            # Create a client to interact with the Xiaohongshu website.
            self.xhs_client = await self.create_xhs_client(httpx_proxy_format)
            if not await self.xhs_client.pong():
                login_obj = XiaoHongShuLogin(
                    login_type=config.LOGIN_TYPE,
                    login_phone="",  # input your phone number
                    browser_context=self.browser_context,
                    context_page=self.context_page,
                    cookie_str=config.COOKIES,
                )
                await login_obj.begin()
                await self.xhs_client.update_cookies(
                    browser_context=self.browser_context,
                    urls=self.cookie_urls,
                )

                # Cookie 模式只是把 web_session 塞进浏览器上下文，既不校验也不弹二维码。
                # Cookie 过期时表现为"没让扫码就直接抓 → 全部跳过 → 显示成功"，
                # 用户根本不知道是登录态挂了。这里二次校验，仍无效就回退扫码。
                if not await self.xhs_client.pong():
                    use_cdp_now = bool(getattr(config, "XHS_ENABLE_CDP_MODE",
                                               config.ENABLE_CDP_MODE))
                    headless_now = (bool(getattr(config, "XHS_CDP_HEADLESS",
                                                 config.CDP_HEADLESS))
                                    if use_cdp_now else config.HEADLESS)
                    if headless_now:
                        utils.logger.warning(
                            "[XiaoHongShuCrawler] 登录态无效（Cookie 已过期），当前为无头模式无法扫码。"
                            "请更新 Cookie，或关闭无头模式后重试（届时会弹出扫码）")
                    else:
                        utils.logger.warning(
                            "[XiaoHongShuCrawler] 登录态无效（Cookie 已过期），回退到扫码登录："
                            "请在弹出的浏览器窗口中扫描二维码 ...")
                        # 先清掉失效 Cookie 再刷新，否则页面停在"有 session 但服务端不认"的
                        # 假登录态，登录弹窗不出来，login_by_qrcode 会直接 sys.exit()
                        try:
                            await self.browser_context.clear_cookies()
                            await self.context_page.reload()
                            await asyncio.sleep(2)
                        except Exception as e:
                            utils.logger.warning(
                                f"[XiaoHongShuCrawler] 清理失效 Cookie 失败（继续尝试扫码）：{e}")
                        await login_obj.login_by_qrcode()
                        await self.xhs_client.update_cookies(
                            browser_context=self.browser_context,
                            urls=self.cookie_urls,
                        )
                        if not await self.xhs_client.pong():
                            utils.logger.warning(
                                "[XiaoHongShuCrawler] 扫码后登录态仍无效，本次抓取可能拿不到数据")

            # 登录态同步到账号池（无论本次是否触发登录流程）：
            # 热榜抓取/无浏览器模式走 httpx，需要这里的 Cookie。
            # 只在服务端确认有效时才写回——looks_logged_in 只看 Cookie 里有没有
            # web_session 这个键，过期 Cookie 也能通过，会拿死 Cookie 污染账号池，
            # 下次启动又被取出来注入，表现为"每次都登录失败还查不出原因"。
            if await self.xhs_client.pong():
                try:
                    from tools.cookie_sync import sync_login_to_pool

                    synced, sync_msg = await sync_login_to_pool(
                        "xhs", self.browser_context, self.cookie_urls)
                    utils.logger.info(f"[XiaoHongShuCrawler] 登录态同步到账号池：{sync_msg}")
                except Exception as e:
                    utils.logger.warning(f"[XiaoHongShuCrawler] 登录态同步失败（不影响本次抓取）：{e}")
            else:
                utils.logger.warning(
                    "[XiaoHongShuCrawler] 登录态无效，跳过账号池同步（避免把失效 Cookie 写回账号池）")

            await self._dispatch_crawl()

            utils.logger.info("[XiaoHongShuCrawler.start] Xhs Crawler finished ...")

    async def _probe_reachable(self, proxy: Optional[Dict]) -> bool:
        """实测能否访问目标站。

        只做 TCP 连通性判断不够：代理软件在监听但上游不通 / 规则拦截时，
        端口照样连得上，可 Chromium 的 Page.goto 会一路卡到超时。
        这里用 httpx 花几秒真发一次请求。

        proxy=None 表示「强制直连」（trust_env=False，忽略环境变量里的代理）。
        """
        import httpx

        kwargs: Dict = {"timeout": 10, "follow_redirects": True, "verify": False}
        if proxy:
            server = proxy.get("server", "")
            kwargs["proxy"] = server if "://" in server else f"http://{server}"
        else:
            kwargs["trust_env"] = False

        try:
            async with httpx.AsyncClient(**kwargs) as client:
                resp = await client.get(self.index_url)
            ok = resp.status_code < 500
            utils.logger.info(
                f"[XiaoHongShuCrawler] {'系统代理' if proxy else '直连'}探测"
                f"{'可用' if ok else '异常'}：HTTP {resp.status_code}")
            return ok
        except Exception as e:
            utils.logger.warning(
                f"[XiaoHongShuCrawler] {'系统代理' if proxy else '直连'}探测失败："
                f"{type(e).__name__}: {str(e)[:160]}")
            return False

    def _disable_system_proxy_env(self) -> None:
        """清掉进程内的代理环境变量。

        浏览器可以靠「不传 proxy」单独绕开，但 httpx 默认 trust_env=True，
        不清理的话 HTTP 客户端仍会走那个坏代理，抓详情照样崩。
        """
        removed = [k for k in ("http_proxy", "https_proxy", "HTTP_PROXY",
                               "HTTPS_PROXY", "all_proxy", "ALL_PROXY") if os.environ.pop(k, None)]
        if removed:
            utils.logger.warning(
                f"[XiaoHongShuCrawler] 已在本进程内清除代理环境变量：{', '.join(removed)}")

    async def _goto_index_page(self) -> None:
        """打开首页（带重试 + 更宽松的等待条件）。

        Playwright 默认 wait_until='load'，要等首页所有图片/脚本下载完。小红书首页
        资源多，弱网或代理环境下经常卡到 30s 超时，抓取直接崩在第一步。
        改成 domcontentloaded 并重试 3 次；全部失败时抛出带诊断信息的错误，
        而不是一句看不懂的 Timeout 30000ms exceeded。
        """
        timeout_ms = int(getattr(config, "PAGE_GOTO_TIMEOUT_SEC", 45) * 1000)
        last_err: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                await self.context_page.goto(
                    self.index_url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                if attempt > 1:
                    utils.logger.info(f"[XiaoHongShuCrawler] 第 {attempt} 次尝试打开首页成功")
                # 风控页拦截：标题/正文带验证关键字时把窗口置前，等用户人工过验证
                if await anti_risk.page_has_captcha(self.context_page):
                    utils.logger.warning(
                        "[XiaoHongShuCrawler] 首页命中安全验证，等待人工处理 ...")
                    await self._human_verify(self.index_url)
                return
            except Exception as e:
                last_err = e
                utils.logger.warning(
                    f"[XiaoHongShuCrawler] 打开 {self.index_url} 失败（第 {attempt}/3 次）："
                    f"{type(e).__name__}: {str(e)[:200]}")
                await asyncio.sleep(2)

        proxy_env = (os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
                     or os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY") or "无")
        raise RuntimeError(
            f"打开 {self.index_url} 连续 3 次失败：{type(last_err).__name__}: {last_err}。"
            f"当前系统代理 = {proxy_env}；"
            f"请确认代理软件已启动且能访问小红书，不需要代理时清空 "
            f"http_proxy/https_proxy 环境变量后重试（或调大 config.PAGE_GOTO_TIMEOUT_SEC）")

    async def _browser_required(self) -> bool:
        """是否需要启动浏览器。

        配置 browser.enable_browser=false 时走无浏览器模式（#3），
        此时签名走纯算法、Cookie 由配置提供，常规抓取不启动任何浏览器。
        """
        try:
            from config.runtime_config import runtime_config

            return runtime_config.get_bool("enable_browser", True)
        except Exception:
            return True

    async def _dispatch_crawl(self) -> None:
        """按抓取类型分发（浏览器模式与无浏览器模式共用）。"""
        crawler_type_var.set(config.CRAWLER_TYPE)
        if config.CRAWLER_TYPE == "search":
            # Search for notes and retrieve their comment information.
            await self.search()
        elif config.CRAWLER_TYPE == "detail":
            # Get the information and comments of the specified post
            await self.get_specified_notes()
        elif config.CRAWLER_TYPE == "creator":
            # Get creator's information and their notes and comments
            await self.get_creators_and_notes()
        elif config.CRAWLER_TYPE == "account":
            # Get the logged-in user's liked & collected image posts
            await self.get_account_behavior_notes()
        else:
            pass

    async def _init_checkpoint_task(self, crawl_type: str) -> Optional[int]:
        """开启断点续爬时创建/复用任务并返回 task_id；未开启或失败返回 None。"""
        try:
            from config.runtime_config import runtime_config
            from store import checkpoint

            if not runtime_config.get_bool("enable_checkpoint", False):
                return None

            params = {
                "keywords": config.KEYWORDS,
                "start_page": config.START_PAGE,
                "max_notes": config.CRAWLER_MAX_NOTES_COUNT,
                "sort_type": getattr(config, "SORT_TYPE", ""),
            }
            task_code = checkpoint.make_task_code("xhs", crawl_type, params)
            task_id = await checkpoint.ensure_task(task_code, "xhs", crawl_type, params)
            if task_id:
                pending = await checkpoint.get_pending(task_id, "search_page")
                utils.logger.info(
                    f"[XiaoHongShuCrawler] 断点续爬开启 task_code={task_code} "
                    f"task_id={task_id} 待补页数={len(pending)}")
            return task_id
        except Exception as e:
            utils.logger.warning(f"[XiaoHongShuCrawler] 断点续爬初始化失败，按普通模式执行：{e}")
            return None

    async def search(self) -> None:
        """Search for notes and retrieve their comment information."""
        utils.logger.info("[XiaoHongShuCrawler.search] Begin search Xiaohongshu keywords")
        xhs_limit_count = 20  # Xiaohongshu limit page fixed value
        if config.CRAWLER_MAX_NOTES_COUNT < xhs_limit_count:
            config.CRAWLER_MAX_NOTES_COUNT = xhs_limit_count
        start_page = config.START_PAGE

        # 断点续爬：相同参数生成相同 task_code，重跑即续跑
        from store import checkpoint

        self.task_id = await self._init_checkpoint_task("search")
        for keyword in config.KEYWORDS.split(","):
            source_keyword_var.set(keyword)
            utils.logger.info(f"[XiaoHongShuCrawler.search] Current search keyword: {keyword}")
            page = 1
            search_id = get_search_id()
            while (page - start_page + 1) * xhs_limit_count <= config.CRAWLER_MAX_NOTES_COUNT:
                if page < start_page:
                    utils.logger.info(f"[XiaoHongShuCrawler.search] Skip page {page}")
                    page += 1
                    continue

                # 断点续爬：已抓过的页直接跳过
                if self.task_id and await checkpoint.is_done(self.task_id, "search_page", keyword, page):
                    utils.logger.info(
                        f"[XiaoHongShuCrawler.search] Skip page {page} (checkpoint done)")
                    page += 1
                    continue

                try:
                    utils.logger.info(f"[XiaoHongShuCrawler.search] search Xiaohongshu keyword: {keyword}, page: {page}")
                    note_ids: List[str] = []
                    xsec_tokens: List[str] = []
                    notes_res = await self.xhs_client.get_note_by_keyword(
                        keyword=keyword,
                        search_id=search_id,
                        page=page,
                        sort=(SearchSortType(config.SORT_TYPE) if config.SORT_TYPE != "" else SearchSortType.GENERAL),
                    )
                    utils.logger.info(f"[XiaoHongShuCrawler.search] Search notes response: {notes_res}")
                    if not notes_res or not notes_res.get("has_more", False):
                        utils.logger.info("[XiaoHongShuCrawler.search] No more content!")
                        break
                    semaphore = asyncio.Semaphore(_xhs_concurrency())
                    task_list = [
                        self.get_note_detail_async_task(
                            note_id=post_item.get("id"),
                            xsec_source=post_item.get("xsec_source"),
                            xsec_token=post_item.get("xsec_token"),
                            semaphore=semaphore,
                        ) for post_item in notes_res.get("items", {}) if post_item.get("model_type") not in ("rec_query", "hot_query")
                    ]
                    note_details = await asyncio.gather(*task_list)
                    for note_detail in note_details:
                        if note_detail:
                            await xhs_store.update_xhs_note(note_detail)
                            await self.download_media(note_detail)
                            note_ids.append(note_detail.get("note_id"))
                            xsec_tokens.append(note_detail.get("xsec_token"))
                    # 断点续爬：标记本页完成（必须在 page += 1 之前，用当前页码）
                    if self.task_id:
                        await checkpoint.mark_done(
                            self.task_id, "search_page", keyword, page,
                            fetched_count=len(note_ids),
                        )
                    page += 1
                    utils.logger.info(f"[XiaoHongShuCrawler.search] Note details: {note_details}")
                    await self.batch_get_note_comments(note_ids, xsec_tokens)

                    # Sleep after each page navigation（5-15s 随机，降低频率特征）
                    await anti_risk.xhs_sleep(f"search 第 {page-1} 页间隔")
                except DataFetchError:
                    utils.logger.error("[XiaoHongShuCrawler.search] Get note detail error")
                    if self.task_id:
                        await checkpoint.mark_error(
                            self.task_id, "search_page", keyword, page, "DataFetchError")
                    break
                except (IPBlockError, PlatformAccessError) as ex:
                    # 验证码 / 安全限制：弹出浏览器让用户人工过验证后重试当前页，
                    # 超时或仍失败才中断本轮（不无限重试）
                    if anti_risk.is_captcha_error(ex):
                        verified = await self._human_verify(self.index_url)
                        if verified:
                            utils.logger.info(
                                "[XiaoHongShuCrawler.search] 人工验证完成，重试当前页")
                            continue
                    utils.logger.error(
                        f"[XiaoHongShuCrawler.search] Access restricted: {ex}. "
                        f"建议降低采集频率、更换 IP 或检查账号状态")
                    break

    async def get_creators_and_notes(self) -> None:
        """Get creator's notes and retrieve their comment information."""
        utils.logger.info("[XiaoHongShuCrawler.get_creators_and_notes] Begin get Xiaohongshu creators")
        for creator_url in config.XHS_CREATOR_ID_LIST:
            try:
                # Parse creator URL to get user_id and security tokens
                creator_info: CreatorUrlInfo = parse_creator_info_from_url(creator_url)
                utils.logger.info(f"[XiaoHongShuCrawler.get_creators_and_notes] Parse creator URL info: {creator_info}")
                user_id = creator_info.user_id

                # get creator detail info from web html content
                createor_info: Dict = await self.xhs_client.get_creator_info(
                    user_id=user_id,
                    xsec_token=creator_info.xsec_token,
                    xsec_source=creator_info.xsec_source
                )
                if createor_info:
                    await xhs_store.save_creator(user_id, creator=createor_info)
            except ValueError as e:
                utils.logger.error(f"[XiaoHongShuCrawler.get_creators_and_notes] Failed to parse creator URL: {e}")
                continue
            except (IPBlockError, PlatformAccessError) as e:
                # Access restricted on the creator homepage, skip this creator instead of crashing the run.
                # 先等人工过验证再重试一次（CDP 连的是真实浏览器，用户直接在窗口里操作）
                if anti_risk.is_captcha_error(e):
                    verified = await self._human_verify(self.index_url)
                    if verified:
                        try:
                            createor_info = await self.xhs_client.get_creator_info(
                                user_id=user_id,
                                xsec_token=creator_info.xsec_token,
                                xsec_source=creator_info.xsec_source,
                            )
                            if createor_info:
                                await xhs_store.save_creator(user_id, creator=createor_info)
                                utils.logger.info(
                                    f"[XiaoHongShuCrawler] 人工验证后重试成功: {user_id}")
                        except Exception as retry_err:
                            utils.logger.warning(
                                f"[XiaoHongShuCrawler] 人工验证后重试仍失败 {user_id}: {retry_err}")
                utils.logger.error(
                    f"[XiaoHongShuCrawler.get_creators_and_notes] Access restricted for creator {creator_url}: {e}. "
                    f"建议降低采集频率、更换 IP 或检查账号状态"
                )
                continue

            # Use randomized crawling interval (5-15s) instead of a fixed one
            crawl_interval = _xhs_crawl_interval()
            # 增量模式（定时任务默认）：预读该作者已入库的 note_id，
            # 翻到整页旧笔记就停止翻页，只抓新帖，避免每次全量重跑
            known_note_ids = None
            if os.getenv("XHS_CREATOR_INCREMENTAL", "").strip().lower() in ("1", "true", "yes", "on"):
                try:
                    from tools.v2_sync import get_content_index

                    index = await get_content_index("xhs", user_id=user_id)
                    known_note_ids = set(index.keys())
                    utils.logger.info(
                        f"[XiaoHongShuCrawler.get_creators_and_notes] 增量模式："
                        f"作者 {user_id} 已入库 {len(known_note_ids)} 条笔记")
                except Exception as e:
                    utils.logger.warning(
                        f"[XiaoHongShuCrawler] 增量预读失败，退化为全量翻页: {e}")
                    known_note_ids = None
            # Get all note information of the creator
            all_notes_list = await self.xhs_client.get_all_notes_by_creator(
                user_id=user_id,
                crawl_interval=crawl_interval,
                callback=self.fetch_creator_notes_detail,
                xsec_token=creator_info.xsec_token,
                xsec_source=creator_info.xsec_source,
                known_note_ids=known_note_ids,
            )

            note_ids = []
            xsec_tokens = []
            for note_item in all_notes_list:
                note_ids.append(note_item.get("note_id"))
                xsec_tokens.append(note_item.get("xsec_token"))
            await self.batch_get_note_comments(note_ids, xsec_tokens)

    async def fetch_creator_notes_detail(self, note_list: List[Dict]):
        """Concurrently obtain the specified post list and save the data"""
        semaphore = asyncio.Semaphore(_xhs_concurrency())
        task_list = [
            self.get_note_detail_async_task(
                note_id=post_item.get("note_id"),
                xsec_source=post_item.get("xsec_source"),
                xsec_token=post_item.get("xsec_token"),
                semaphore=semaphore,
            ) for post_item in note_list
        ]

        note_details = await asyncio.gather(*task_list)
        for note_detail in note_details:
            if note_detail:
                await xhs_store.update_xhs_note(note_detail)
                await self.download_media(note_detail)

    async def get_specified_notes(self):
        """Get the information and comments of the specified post

        Note: Must specify note_id, xsec_source, xsec_token
        """
        get_note_detail_task_list = []
        for full_note_url in config.XHS_SPECIFIED_NOTE_URL_LIST:
            note_url_info: NoteUrlInfo = parse_note_info_from_note_url(full_note_url)
            utils.logger.info(f"[XiaoHongShuCrawler.get_specified_notes] Parse note url info: {note_url_info}")
            crawler_task = self.get_note_detail_async_task(
                note_id=note_url_info.note_id,
                xsec_source=note_url_info.xsec_source,
                xsec_token=note_url_info.xsec_token,
                semaphore=asyncio.Semaphore(_xhs_concurrency()),
            )
            get_note_detail_task_list.append(crawler_task)

        need_get_comment_note_ids = []
        xsec_tokens = []
        note_details = await asyncio.gather(*get_note_detail_task_list)
        for note_detail in note_details:
            if note_detail:
                need_get_comment_note_ids.append(note_detail.get("note_id", ""))
                xsec_tokens.append(note_detail.get("xsec_token", ""))
                await xhs_store.update_xhs_note(note_detail)
                await self.download_media(note_detail)
        await self.batch_get_note_comments(need_get_comment_note_ids, xsec_tokens)

    # ------------------------------------------------------------------
    # account 维度：登录用户的点赞 / 收藏图文帖
    #
    # 与 profile（作者主页）/ note（单帖）并列的第三个抓取维度，供定时任务
    # 「扫码登录用户」使用：把该账号点赞过、收藏过的图文帖陆续拉进来。
    # - 只保留图文帖，视频帖（note.type == "video"）直接跳过；
    # - 详情/评论/媒体/S3 备份全部复用既有管线，不另起炉灶；
    # - 入库走 v2 upsert，已存在的帖子自动增量刷新（评论重抓）。
    # ------------------------------------------------------------------

    async def _resolve_account_cookie(self) -> str:
        """account 模式解析登录态：优先账号池指定 ID，否则健康度最高。

        返回解密后的 cookie 串；账号池不可用 / 无可用账号返回空串。
        """
        account_id = (getattr(config, "XHS_ACCOUNT_ID", "") or "").strip()
        try:
            from account import AccountPool

            pool = AccountPool()
            if account_id:
                account = await pool.get_account(int(account_id))
                if account and account.get("cookie"):
                    utils.logger.info(
                        f"[XiaoHongShuCrawler] account 模式使用账号池账号 "
                        f"#{account_id}（{account.get('account_name')}）")
                    return account["cookie"]
                utils.logger.warning(
                    f"[XiaoHongShuCrawler] 账号池无可用账号 #{account_id}，"
                    f"回退为健康度最高的账号")
            picked = await pool.pick("xhs")
            if picked and picked.get("cookie"):
                return picked["cookie"]
        except Exception as e:
            utils.logger.warning(f"[XiaoHongShuCrawler] 账号池解析失败: {e}")
        return ""

    # -------------------- 页面内抓取「赞过 / 收藏」 --------------------
    # 实测结论（2026-10-28）：接口直连 /note/like/page 虽然签名通过、
    # selfinfo 里 liked=12，却稳定返回 {"notes": [], "has_more": false}。
    # 平台按「请求是否来自网页会话」放行，直连永远拿不到列表。
    # 唯一可行路径：打开本人主页的「赞过 / 收藏」标签页，截获页面自己发的请求。
    # --------------------

    _BEHAVIOR_TAB = {"likes": ("赞过", "like"), "collects": ("收藏", "collect")}
    _BEHAVIOR_API = {"likes": "note/like/page", "collects": "note/collect/page"}

    # 评论抓取连续失败多少次就熔断整批（不再对剩下的帖子继续发请求）。
    # 2026-10-28：100 篇点赞帖全撞 HTTP 461 验证码，每篇还重试 3 次，
    # 一轮下来 300 次无效请求，账号越打越死。
    RISK_FAIL_LIMIT = 3
    # 熔断标记：调度器扫到它就该把任务标成失败而不是 success，
    # 否则用户只看得到「帖子进来了、评论是空的」。
    RISK_ABORT_MARKER = "[MEDIACRAWLER_RISK_ABORT]"

    @staticmethod
    def _normalize_behavior_note(raw: Dict) -> Optional[Dict]:
        """把点赞/收藏列表里的卡片统一成 {note_id, type, xsec_token, cover}。

        页面接口的卡片有两种形态：扁平（note_id/type 直接在顶层）和
        包一层 note_card（note_id 在顶层、其余字段在 card 里），都要兼容。
        """
        if not isinstance(raw, dict):
            return None
        card = raw.get("note_card") or raw.get("noteCard") or {}
        note_id = str(raw.get("note_id") or raw.get("id") or card.get("note_id") or "")
        if not note_id:
            return None
        cover = card.get("cover") or raw.get("cover") or {}
        return {
            "note_id": note_id,
            "type": (raw.get("type") or card.get("type") or ""),
            "xsec_token": (raw.get("xsec_token") or card.get("xsec_token") or ""),
            "display_title": (card.get("display_title") or raw.get("display_title") or ""),
            "cover": cover,
        }

    async def _collect_behavior_notes_via_page(
        self, user_id: str, behaviors: List[str], max_notes: int = 200,
    ) -> List[Dict]:
        """在真实浏览器页面里滚动「赞过 / 收藏」标签页，截获页面自身发出的
        列表请求，返回归一化后的笔记列表（按 note_id 去重）。

        只在接口直连返回空时使用；没有浏览器上下文（纯无头直连）返回空。
        """
        if self.browser_context is None:
            return []

        collected: List[Dict] = []
        seen: set = set()

        # 页面必须有登录态：CDP 复用失败时上下文是全新空浏览器（没有 cookie），
        # 打开「赞过」页只会看到登录墙。这里把账号 cookie 直接注入上下文。
        try:
            cookie_dict = (self.xhs_client.cookie_dict
                           if self.xhs_client is not None else None) \
                or utils.convert_str_cookie_to_dict(config.COOKIES or "")
            if cookie_dict:
                domain = "rednote.com" if config.XHS_INTERNATIONAL else "xiaohongshu.com"
                await self.browser_context.add_cookies([
                    {"name": str(k), "value": str(v),
                     "domain": f".{domain}", "path": "/"}
                    for k, v in cookie_dict.items() if k and v
                ])
                utils.logger.info(
                    f"[get_account_behavior_notes] 已注入 {len(cookie_dict)} 条 cookie 到页面上下文")
        except Exception as e:
            utils.logger.warning(
                f"[get_account_behavior_notes] cookie 注入失败（页面可能掉登录）: {e}")

        page = await self.browser_context.new_page()
        try:
            for behavior in behaviors:
                label = self._BEHAVIOR_TAB.get(behavior, ("赞过", "like"))[0]
                tab_key = self._BEHAVIOR_TAB.get(behavior, ("赞过", "like"))[1]
                api_part = self._BEHAVIOR_API.get(behavior, "note/like/page")

                grabbed: List[Dict] = []
                resp_tasks: List = []
                # 诊断用：记录本次页面请求过的所有 /api/sns/web 接口，
                # 拿不到数据时打出来，省得靠猜（标签名/接口路径改版排查）
                seen_apis: List[str] = []

                async def _grab(resp, grabbed=grabbed) -> None:
                    try:
                        body = await resp.json()
                    except Exception:
                        return
                    got = 0
                    for n in ((body or {}).get("notes") or []):
                        item = self._normalize_behavior_note(n)
                        if item:
                            grabbed.append(item)
                            got += 1
                    if got:
                        utils.logger.info(
                            f"[get_account_behavior_notes] 截获「{label}」"
                            f"接口一批 {got} 条：{resp.url.split('?')[0].rsplit('/', 3)[-3:]}")

                def _on_resp(resp, api_part=api_part, resp_tasks=resp_tasks,
                             seen_apis=seen_apis) -> None:
                    url = resp.url
                    if "/api/sns/web" in url:
                        short = url.split("?")[0].replace("https://edith.xiaohongshu.com", "")
                        if short not in seen_apis and len(seen_apis) < 40:
                            seen_apis.append(short)
                    if api_part in url:
                        resp_tasks.append(asyncio.create_task(_grab(resp)))

                page.on("response", _on_resp)
                # 1) 打开发帖人主页（浏览器上下文里已带登录态），切到目标标签
                await page.goto(f"{self.index_url}/user/profile/{user_id}",
                                wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(2500)
                baseline = len(grabbed)
                tab_clicked = False
                # 点标签：优先在 DOM 里按文本找可点元素（比 get_by_text 稳，
                # 小红书标签有多个同名节点，exact 匹配常点空）
                try:
                    tab_clicked = bool(await page.evaluate(
                        """(label) => {
                            const nodes = Array.from(document.querySelectorAll('div,span,a,li'))
                              .filter(e => e.textContent && e.textContent.trim() === label);
                            if (!nodes.length) return false;
                            const el = nodes[nodes.length - 1];
                            (el.closest('div') || el).click();
                            return true;
                        }""", label))
                except Exception:
                    tab_clicked = False
                if not tab_clicked:
                    try:
                        await page.get_by_text(label, exact=True).first.click(timeout=6000)
                        tab_clicked = True
                    except Exception:
                        await page.goto(
                            f"{self.index_url}/user/profile/{user_id}?tab={tab_key}",
                            wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(3500)
                try:
                    cur_url = page.url.split("?")[-1]
                    title = await page.title()
                except Exception:
                    cur_url, title = "", ""
                utils.logger.info(
                    f"[get_account_behavior_notes] 「{label}」页已就位"
                    f"（点中标签={tab_clicked}，地址={cur_url[:60]}，标题={title[:30]}）")
                # 2) 滚动加载：达到上限 / 连续 3 轮无增长就停
                stale = 0
                for _ in range(60):
                    if len(grabbed) - baseline >= max_notes:
                        break
                    before = len(grabbed)
                    try:
                        await page.mouse.wheel(0, 2600)
                    except Exception:
                        pass
                    await page.wait_for_timeout(1800)
                    if len(grabbed) == before:
                        stale += 1
                        if stale >= 3:
                            break
                    else:
                        stale = 0
                    await anti_risk.interval_sleep(
                        random.uniform(0.6, 1.4), f"{label}列表滚动")

                for t in resp_tasks:
                    if not t.done():
                        t.cancel()
                try:
                    page.remove_listener("response", _on_resp)
                except Exception:
                    pass

                fresh = 0
                for item in grabbed:
                    nid = item["note_id"]
                    if nid in seen:
                        continue
                    seen.add(nid)
                    collected.append(item)
                    fresh += 1
                if not grabbed:
                    # 一条都没截到：把页面实际发过的接口打出来，方便判断是
                    # 标签没切对、还是列表接口换了路径
                    utils.logger.warning(
                        f"[get_account_behavior_notes] 「{label}」未截到列表接口，"
                        f"页面共请求 {len(seen_apis)} 个接口："
                        f"{' | '.join(seen_apis[:25])}")
                utils.logger.info(
                    f"[get_account_behavior_notes] 页面截获「{label}」"
                    f"{len(grabbed)} 条，去重后新增 {fresh} 条")
        finally:
            try:
                await page.close()
            except Exception:
                pass
        return collected

    async def get_account_behavior_notes(self):
        """拉取登录用户「点赞 + 收藏」的图文帖并入库（跳过视频帖）。"""
        # 1) 登录态兜底：没带 cookie 时按账号池解析，并同步到已建好的客户端
        if not (config.COOKIES or "").strip():
            cookie = await self._resolve_account_cookie()
            if cookie:
                config.COOKIES = cookie
                config.LOGIN_TYPE = "cookie"
                if self.xhs_client is not None:
                    self.xhs_client.headers["Cookie"] = cookie
                    self.xhs_client.cookie_dict = utils.convert_str_cookie_to_dict(cookie)
        if not (config.COOKIES or "").strip():
            raise RuntimeError(
                "account 模式需要登录态：请在任务 options 里配置 Cookie，"
                "或先在账号池添加扫码登录账号（--account_id 指定）")

        # 2) 拿登录用户自己的 user_id（点赞/收藏接口按它拉列表）
        #    这里就撞验证码的话立刻收手：别再翻 25 页列表、再抓 100 篇详情，
        #    白打几百个请求把账号越推越死（2026-10-28 实测过一轮）。
        try:
            self_user_id = await self.xhs_client.get_self_user_id()
        except (CaptchaError, IPBlockError, PlatformAccessError) as e:
            self._risk_abort_reason = f"身份检查阶段即触发风控：{type(e).__name__}"
            utils.logger.error(
                f"[get_account_behavior_notes] {self._risk_abort_reason}，"
                f"本轮放弃抓取。{self.RISK_ABORT_MARKER} 请先冷却或人工过验证码。")
            return
        if not self_user_id:
            raise RuntimeError(
                "无法获取当前登录用户 ID（selfinfo 与互动列表均未返回）："
                "请检查 Cookie 是否有效、账号是否发布过笔记/收到过互动")

        behaviors = (os.environ.get("XHS_ACCOUNT_BEHAVIORS")
                     or getattr(config, "XHS_ACCOUNT_BEHAVIORS", "")
                     or "likes,collects")
        behavior_list = [b.strip() for b in behaviors.split(",") if b.strip()]

        # 3) 边拉列表边建详情任务（回调里过滤视频帖 + 已归档帖子）
        get_note_detail_task_list = []
        skipped_video = 0
        skipped_archived = 0
        # 本次「新帖」抓取额度，按行为独立计数。列表本身会一直翻到
        # has_more=False（跨过的老帖不算消耗额度），否则增量模式下永远
        # 只会扫到最前面几页的已归档帖子。
        budget = runtime_config.get_int("xhs_behavior_max_notes", config.XHS_BEHAVIOR_MAX_NOTES)
        queued_by_behavior: Dict[str, int] = {}

        # 增量：已归档的帖子不再重抓详情+评论。
        # 点赞动辄几百条（实测本号 495 条），全量重抓评论要跑几小时，
        # 日常备份任务只需要补新增的；想强制刷新老帖时把
        # XHS_BEHAVIOR_INCREMENTAL 设成 0。
        incremental = runtime_config.get_bool("xhs_behavior_incremental", config.XHS_BEHAVIOR_INCREMENTAL)
        known_ids: set = set()
        if incremental:
            try:
                from tools.v2_sync import get_archived_content_ids

                # 跳过判据 = 评论完整率（已抓 / 原帖显示）>= 90%。
                # backfill_limit（篇数上限）与「本次抓取上限」(XHS_BEHAVIOR_MAX_NOTES)
                # 统一；backfill_max_comments（评论条数预算）= 本次抓取上限 ×
                # XHS_COMMENT_BACKFILL_MAX_COMMENTS（每篇预留额度），使每轮选中的
                # 待补批次 ≈ 本次抓取上限。旧实现把评论条数预算写成死值 3000，导致
                # 168 篇待补帖每轮只挑得出 ~8 篇、其余被「顺延=假装已归档」永久跳过，
                # 点赞备份永远停在 9 篇。本预算只决定「本轮挑哪些帖重抓」，真正抓
                # 几条评论由 XHS_COMMENT_MAX_PER_NOTE 管。
                backfill_limit = runtime_config.get_int(
                    "xhs_behavior_max_notes", config.XHS_BEHAVIOR_MAX_NOTES)
                per_post = runtime_config.get_int(
                    "xhs_comment_backfill_max_comments", config.XHS_COMMENT_BACKFILL_MAX_COMMENTS)
                backfill_max_comments = max(1, budget) * max(1, per_post)
                known_ids = await get_archived_content_ids(
                    "xhs",
                    backfill_limit=backfill_limit,
                    backfill_max_comments=backfill_max_comments)
                utils.logger.info(
                    f"[get_account_behavior_notes] 增量模式：库内已归档 "
                    f"{len(known_ids)} 条帖子，本次跳过它们（额度按新帖算：{budget}）")
            except Exception as e:
                utils.logger.warning(
                    f"[get_account_behavior_notes] 读取已归档列表失败，"
                    f"退回全量模式：{type(e).__name__}: {e}")

        # 整批详情请求共用同一个并发闸门（见下方调用处的注释）
        detail_semaphore = asyncio.Semaphore(_xhs_concurrency())

        # 3.1) 优先续抓「之前没抓完」的帖子：直接按 note_id + 已存 xsec_token
        #      拉详情+评论，**不必先把整份点赞/收藏列表从头翻一遍**。
        #      用户明确要求「先补未完成的，不要每次都从头读全部帖子」——
        #      未完成帖子靠 DB 里的完整率筛出来（story_chapter.ext 存了 token），
        #      列表阶段从此只负责发现「新增」。
        backfill_queued = 0
        if incremental:
            try:
                from tools.v2_sync import get_backfill_candidates
                # 待补「篇数」上限 = 本次抓取上限 budget；评论条数预算沿用
                # max(1,budget) × 每篇预留额度（与 get_archived_content_ids 保持一致）
                _per_post = runtime_config.get_int(
                    "xhs_comment_backfill_max_comments", config.XHS_COMMENT_BACKFILL_MAX_COMMENTS)
                _bf_max_comments = max(1, budget) * max(1, _per_post)
                candidates = await get_backfill_candidates(
                    "xhs", limit=budget, max_comments=_bf_max_comments)
            except Exception as e:
                candidates = []
                utils.logger.warning(
                    f"[get_account_behavior_notes] 读取待补帖子失败："
                    f"{type(e).__name__}: {e}")
            # 未完成帖优先占用本轮额度，但给「新增帖」预留约 1/5 —— 否则待补池
            # 长期清不空（个别帖一直失败）时，列表阶段额度恒为 0，新点赞永远进不来。
            reserve_new = min(max(1, budget // 5), max(0, budget - 1)) if budget > 0 else 0
            backfill_cap = max(0, budget - reserve_new) if budget > 0 else len(candidates)
            take = candidates[:backfill_cap]
            for cand in take:
                cid = str(cand.get("content_id") or "")
                if not cid:
                    continue
                # 竞态保护：两次查询之间若该帖已被判定完成/顺延，跳过不重复排队
                if cid in known_ids:
                    continue
                known_ids.add(cid)   # 列表阶段不再重复排队
                get_note_detail_task_list.append(self.get_note_detail_async_task(
                    note_id=cid,
                    xsec_source="pc_feed",
                    xsec_token=cand.get("xsec_token") or "",
                    semaphore=detail_semaphore,
                ))
                backfill_queued += 1
            if backfill_queued:
                utils.logger.info(
                    f"[get_account_behavior_notes] 优先续抓未完成帖子 "
                    f"{backfill_queued} 篇（按 note_id 直连，不翻列表；"
                    f"待补池共 {len(candidates)} 篇）")

        # 列表阶段额度 = 总额度 - 已用于续抓的篇数（优先补完再发现新增）
        list_budget = max(0, budget - backfill_queued)
        # 早停：连续 N 页无「新增待抓帖」即停止翻页，避免每次从头扫完整列表
        early_stop_pages = runtime_config.get_int(
            "xhs_behavior_early_stop_pages",
            getattr(config, "XHS_BEHAVIOR_EARLY_STOP_PAGES", 3))
        idle_pages: Dict[str, int] = {}

        async def _on_batch(notes: List[Dict], behavior: str = "likes") -> Dict:
            """回调：把本批笔记里「要抓详情+评论」的排进队列。

            返回 dict `{"stop": bool, "queued": int}`：
              - stop=True 表示该行为额度已用满，调用方应停止翻页；
              - queued 为本页真正排进抓取队列的帖子数（视频帖/已归档帖不计入）。
                queued=0 时 client 端列表翻页不再随机休眠——没有评论抓取
                就不需要为节流白白等待。
            """
            nonlocal skipped_video, skipped_archived
            queued = 0
            for note in notes:
                if queued_by_behavior.get(behavior, 0) >= list_budget:
                    return {"stop": True, "queued": queued}
                note_id = str(note.get("note_id") or note.get("id") or "")
                if not note_id:
                    continue
                if (note.get("type") or "") == "video":
                    skipped_video += 1
                    utils.logger.info(
                        f"[get_account_behavior_notes] 跳过视频帖 {note_id}")
                    continue
                if note_id in known_ids:
                    skipped_archived += 1
                    continue
                get_note_detail_task_list.append(self.get_note_detail_async_task(
                    note_id=note_id,
                    xsec_source="pc_feed",
                    xsec_token=note.get("xsec_token") or "",
                    # 整批共用同一个信号量！原先这里每篇都 new 一个，
                    # 等于 100 篇同时并发打出去 —— 秒级 100 请求，风控必炸
                    # （2026-10-28：一轮点赞任务把账号打到 HTTP 461 验证码）。
                    semaphore=detail_semaphore,
                ))
                queued_by_behavior[behavior] = queued_by_behavior.get(behavior, 0) + 1
                queued += 1
            # 本页无「新增待抓帖」→ 累计空转页；一旦连续 N 页空转即早停
            # （点赞/收藏按时间倒序，后面只会更老，已处理/未达标都在别处兜底）。
            stop = queued_by_behavior.get(behavior, 0) >= list_budget
            if not stop and update_idle_streak(
                    idle_pages, behavior, queued, early_stop_pages):
                stop = True
                utils.logger.info(
                    f"[get_account_behavior_notes] 「{behavior}」连续 "
                    f"{idle_pages[behavior]} 页无新增待抓帖，提前停止翻页"
                    f"（不再从头扫完整列表）")
            return {"stop": stop, "queued": queued}

        try:
            await self.xhs_client.get_all_behavior_notes(
                self_user_id, behaviors=behavior_list, callback=_on_batch,
                # 别用 client 默认的 1s/页：点赞列表要翻几十页，1s 一页太急。
                # 走小红书统一的 5-15s 随机间隔。
                crawl_interval=_xhs_crawl_interval())
        except (CaptchaError, IPBlockError, PlatformAccessError) as e:
            # 翻列表途中被风控：直接收手，别再进详情阶段
            self._risk_abort_reason = f"列表阶段触发风控：{type(e).__name__}"
            utils.logger.error(
                f"[get_account_behavior_notes] {self._risk_abort_reason}，"
                f"本轮放弃抓取。{self.RISK_ABORT_MARKER}")
            return

        # 接口直连拿不到列表时（平台只对网页会话放行）退回页面内截获：
        # 打开本人主页「赞过/收藏」页滚动，抓页面自己发出的请求
        if not get_note_detail_task_list and self.browser_context is not None:
            utils.logger.info(
                "[get_account_behavior_notes] 接口列表为空，改用浏览器页面截获"
                "（打开本人主页「赞过/收藏」并滚动加载）")
            page_notes = await self._collect_behavior_notes_via_page(
                self_user_id, behavior_list)
            if page_notes:
                await _on_batch(page_notes)

        if not get_note_detail_task_list:
            utils.logger.warning(
                "[get_account_behavior_notes] 点赞/收藏列表为空：接口直连与"
                "浏览器页面截获都没拿到数据。若账号里确实有点赞，请确认"
                "selfinfo 的 liked 数是否 >0、以及浏览器是否处于登录态。")

        utils.logger.info(
            f"[get_account_behavior_notes] 用户 {self_user_id} 待抓取图文帖 "
            f"{len(get_note_detail_task_list)} 篇，跳过视频帖 {skipped_video} 篇，"
            f"跳过已归档 {skipped_archived} 篇")

        # 4) 详情管线（与 get_specified_notes 完全一致）
        need_get_comment_note_ids = []
        xsec_tokens = []
        note_details = await asyncio.gather(*get_note_detail_task_list)
        for note_detail in note_details:
            if note_detail:
                need_get_comment_note_ids.append(note_detail.get("note_id", ""))
                xsec_tokens.append(note_detail.get("xsec_token", ""))
                await xhs_store.update_xhs_note(note_detail)
                await self.download_media(note_detail)
        await self.batch_get_note_comments(need_get_comment_note_ids, xsec_tokens)

    async def get_note_detail_async_task(
        self,
        note_id: str,
        xsec_source: str,
        xsec_token: str,
        semaphore: asyncio.Semaphore,
    ) -> Optional[Dict]:
        """Get note detail

        Args:
            note_id:
            xsec_source:
            xsec_token:
            semaphore:

        Returns:
            Dict: note detail
        """
        note_detail = None
        # 风控熔断：前面已经连续被验证码拦下，这篇直接跳过，别再撞
        if self._risk_abort_reason:
            return None
        utils.logger.info(f"[get_note_detail_async_task] Begin get note detail, note_id: {note_id}")
        async with semaphore:
            try:
                try:
                    note_detail = await self.xhs_client.get_note_by_id(note_id, xsec_source, xsec_token)
                except (CaptchaError, IPBlockError, PlatformAccessError) as e:
                    # 账号已被平台风控（461/471）：任何兜底通道（html/SSR）此时
                    # 也只会再吃一次 461，纯属加重风控、把账号彻底打死。
                    # 立即熔断本批，绝不再级联重试 —— 这是 2026-10-28 那次
                    # 「详情阶段被风控后几百个 461 把账号彻底锁死」事故的止血点。
                    self._abort(f"详情接口触发风控：{type(e).__name__}", type(e))
                    utils.logger.error(
                        f"[get_note_detail_async_task] 详情请求被平台拦截，跳过 "
                        f"{note_id}: {type(e).__name__}: {str(e)[:120]}")
                    # 风控类：弹出浏览器让用户人工过验证，通过后当场重试本篇。
                    # 这是唯一一次机会（_human_verify 内部限一次），
                    # 用户不在场/验证失败就按熔断处理，等冷却后再补。
                    if (anti_risk.is_captcha_error(e)
                            and await self._human_verify(
                                self._note_web_url(note_id, xsec_source, xsec_token))):
                        try:
                            retried = await self.xhs_client.get_note_by_id(
                                note_id, xsec_source, xsec_token)
                        except Exception as retry_err:
                            utils.logger.warning(
                                f"[get_note_detail_async_task] 人工验证后重试异常 "
                                f"{note_id}: {type(retry_err).__name__}")
                            retried = None
                        if retried:
                            retried.update({"xsec_token": xsec_token,
                                            "xsec_source": xsec_source})
                            utils.logger.info(
                                f"[get_note_detail_async_task] 人工验证后重试成功: {note_id}")
                            await anti_risk.xhs_sleep(
                                f"note {note_id} 人工验证后重试间隔")
                            return retried
                    return None
                except RetryError:
                    note_detail = None

                if not note_detail:
                    # 兜底通道只处理「非风控」失败（空结果/token 过期/网络）。
                    # 每个兜底都要捕获风控错误，一旦撞上立即熔断，绝不连发。
                    try:
                        note_detail = await self.xhs_client.get_note_by_id_from_html(
                            note_id, xsec_source, xsec_token, enable_cookie=True)
                    except (CaptchaError, IPBlockError, PlatformAccessError) as e:
                        self._abort(f"详情接口触发风控：{type(e).__name__}", type(e))
                        return None
                    if not note_detail:
                        # 带 Cookie 还失败，多半是登录态已失效、页面被登录墙拦了。
                        # 去掉 Cookie 再试一次（分享链接的详情页本身是 SSR 免登录可读）。
                        try:
                            note_detail = await self.xhs_client.get_note_by_id_from_html(
                                note_id, xsec_source, xsec_token, enable_cookie=False)
                        except (CaptchaError, IPBlockError, PlatformAccessError) as e:
                            self._abort(f"详情接口触发风控：{type(e).__name__}", type(e))
                            return None
                    if not note_detail:
                        # 最后兜底：免登录 SSR 通道（不带 x-s 签名、不带失效 Cookie）。
                        try:
                            from media_platform.xhs.ssr import fetch_note_by_url

                            ssr_url = (f"{self.xhs_client._domain}/explore/{note_id}"
                                       f"?xsec_token={quote(str(xsec_token), safe='')}"
                                       f"&xsec_source={xsec_source}")
                            ssr_data = await fetch_note_by_url(ssr_url)
                            note_detail = ssr_data.get("note_item") or None
                            if note_detail:
                                utils.logger.info(
                                    f"[get_note_detail_async_task] 免登录 SSR 通道兜底成功: {note_id}")
                        except (CaptchaError, IPBlockError, PlatformAccessError) as e:
                            self._abort(f"详情接口触发风控：{type(e).__name__}", type(e))
                            return None
                        except Exception as ssr_exc:
                            utils.logger.info(f"[get_note_detail_async_task] SSR 兜底失败: {ssr_exc}")
                    if not note_detail:
                        # 注意：空结果只代表"这次没抓到"（风控/token 过期/网络），
                        # 不代表内容已删除。已删除由下面的 NoteNotFoundError
                        # （平台明确返回 NOTE_NOT_FOUND/ABNORMAL 错误码）分支标记，
                        # 这里绝不碰本地数据，否则会把活的内容误标成已删除。
                        utils.logger.warning(f"[skip] Failed to get note detail, Id: {note_id}, 跳过继续")
                        # 失败也要退避：否则被风控/批量空结果时会以零间隔
                        # 紧挨着打下一帖，进一步坐实风控。
                        await anti_risk.xhs_sleep(f"note {note_id} 详情失败间隔")
                        return None

                note_detail.update({"xsec_token": xsec_token, "xsec_source": xsec_source})

                # Sleep after fetching note detail（5-15s 随机，避免固定节奏）
                await anti_risk.xhs_sleep(f"note {note_id} 详情间隔")

                return note_detail

            except NoteNotFoundError as ex:
                utils.logger.warning(f"[XiaoHongShuCrawler.get_note_detail_async_task] Note not found: {note_id}, {ex}")
                await self._mark_content_status(note_id, "deleted")
                return None
            except NoteHiddenError as ex:
                # 作者已隐藏原贴（code -510001）：与"已删除"是不同的状态，单独标记，
                # 前端展示【原贴已隐藏】徽标，数据仍保留可查。
                utils.logger.warning(f"[XiaoHongShuCrawler.get_note_detail_async_task] Note hidden: {note_id}, {ex}")
                await self._mark_content_status(note_id, "hidden")
                return None
            except (CaptchaError, IPBlockError, PlatformAccessError) as ex:
                # 外层兜底：任何在兜底通道里漏出来的风控错误，直接熔断本批。
                self._abort(f"详情接口触发风控：{type(ex).__name__}", type(ex))
                utils.logger.error(
                    f"[XiaoHongShuCrawler.get_note_detail_async_task] Access restricted while getting note {note_id}: {ex}. "
                    f"建议降低采集频率、更换 IP 或检查账号状态"
                )
                # 验证码/安全限制：等用户在真实浏览器里人工完成验证后重试一次
                if anti_risk.is_captcha_error(ex):
                    verified = await self._human_verify(
                        self._note_web_url(note_id, xsec_source, xsec_token))
                    if verified:
                        try:
                            retried = await self.xhs_client.get_note_by_id(
                                note_id, xsec_source, xsec_token)
                            if retried:
                                retried.update({"xsec_token": xsec_token,
                                                "xsec_source": xsec_source})
                                await anti_risk.xhs_sleep(
                                    f"note {note_id} 人工验证后重试间隔")
                                return retried
                            utils.logger.warning(
                                f"[XiaoHongShuCrawler] 人工验证后重试仍无数据: {note_id}")
                        except Exception as retry_err:
                            utils.logger.warning(
                                f"[XiaoHongShuCrawler] 人工验证后重试异常 {note_id}: {retry_err}")
                return None
            except DataFetchError as ex:
                utils.logger.error(f"[XiaoHongShuCrawler.get_note_detail_async_task] Get note detail error: {ex}")
                return None
            except KeyError as ex:
                utils.logger.error(f"[XiaoHongShuCrawler.get_note_detail_async_task] have not fund note detail note_id:{note_id}, err: {ex}")
                return None

    async def _mark_content_status(self, note_id: str, status: str) -> None:
        """平台侧内容状态变化时，把 v2 里的对应 story 标记为指定状态。

        status: "deleted"（已删除，code -510000）| "hidden"（作者已隐藏，code -510001）。
        只在配置库是数据库模式时才有意义；失败只记日志，不影响抓取主流程。
        """
        try:
            from tools.v2_sync import mark_story_status
            await mark_story_status("xhs", note_id, status)
        except Exception as e:
            utils.logger.warning(f"[xhs] 标记内容状态失败 {note_id}={status}: {e}")

    async def batch_get_note_comments(self, note_list: List[str], xsec_tokens: List[str]):
        """Batch get note comments"""
        if not config.ENABLE_GET_COMMENTS:
            utils.logger.info(f"[XiaoHongShuCrawler.batch_get_note_comments] Crawling comment mode is not enabled")
            return
        if self._risk_abort_reason:
            # 详情阶段已被风控熔断，评论就别再起了
            utils.logger.error(
                f"[XiaoHongShuCrawler.batch_get_note_comments] 风控已熔断，"
                f"跳过 {len(note_list)} 篇的评论抓取。{self.RISK_ABORT_MARKER} "
                f"原因：{self._risk_abort_reason}")
            return

        utils.logger.info(f"[XiaoHongShuCrawler.batch_get_note_comments] Begin batch get note comments, note list: {note_list}")

        # 单篇评论上限按「原帖显示评论量」推算（不再统一吃全局的 30）：
        # 声明 800 条的帖子就该抓 800 条，否则增量判定会把它当已归档。
        declared_map: Dict[str, int] = {}
        progress_map: Dict[str, tuple] = {}
        try:
            from tools.v2_sync import (get_comment_progress,
                                       get_declared_comment_counts)

            declared_map = await get_declared_comment_counts("xhs", note_list)
            progress_map = await get_comment_progress("xhs", note_list)
        except Exception as e:
            utils.logger.warning(
                f"[XiaoHongShuCrawler.batch_get_note_comments] 读取声明评论数失败，"
                f"按不限制处理: {type(e).__name__}: {e}")

        # ---- 抓取顺序：先排序，再一篇一篇抓到底（2026-10-29）----
        # 原先按列表原始顺序串行：一旦中途被风控打断，结果就是「每篇都抓了
        # 一点」（0%→40%、0%→1%、其余 0%），一轮下来没有任何一篇真正收口。
        # 现在按「完成度从高到低」排（见 comment_fetch_order）。
        order_key = comment_fetch_order(progress_map, declared_map)
        order = sorted(range(len(note_list)),
                       key=lambda i: order_key(note_list[i]))
        ordered_notes = [note_list[i] for i in order]
        ordered_tokens = [xsec_tokens[i] for i in order]
        if order and order[:5] != list(range(min(5, len(order)))):
            preview = []
            for i in order[:5]:
                f, d = progress_map.get(str(note_list[i]), (0, 0))
                preview.append(f"{note_list[i]} {f}/{d or '?'}")
            utils.logger.info(
                "[XiaoHongShuCrawler.batch_get_note_comments] 按完成度排序后优先抓："
                + "；".join(preview))

        semaphore = asyncio.Semaphore(_xhs_concurrency())
        task_list: List[Task] = []
        for index, note_id in enumerate(ordered_notes):
            task = asyncio.create_task(
                self.get_comments(note_id=note_id, xsec_token=ordered_tokens[index],
                                  semaphore=semaphore,
                                  declared=int(declared_map.get(str(note_id), 0) or 0)),
                name=note_id,
            )
            task_list.append(task)
        await asyncio.gather(*task_list, return_exceptions=True)

        # 抓完立刻把「已抓/声明」写回 v2 章节表：下一轮增量的跳过判定
        # （tools/v2_sync.get_archived_content_ids）与前端完整率都读它。
        try:
            from tools.v2_sync import refresh_comment_completeness

            updated = await refresh_comment_completeness("xhs", note_list)
            utils.logger.info(
                f"[XiaoHongShuCrawler.batch_get_note_comments] 已刷新 "
                f"{updated} 篇帖子的评论完整率（已抓/原帖显示）")
        except Exception as e:
            utils.logger.warning(
                f"[XiaoHongShuCrawler.batch_get_note_comments] 刷新评论完整率失败: "
                f"{type(e).__name__}: {e}")

        if self._risk_abort_reason:
            utils.logger.error(
                f"[XiaoHongShuCrawler.batch_get_note_comments] 风控中止："
                f"{self._risk_abort_reason}｜本轮 {len(note_list)} 篇里有 "
                f"{len(note_list) - self._comment_done_count} 篇没抓到评论。"
                f"{self.RISK_ABORT_MARKER} 请降低任务额度/拉长间隔，并在浏览器里"
                f"处理验证码或等冷却后再跑。")

    async def get_comments(self, note_id: str, xsec_token: str,
                           semaphore: asyncio.Semaphore, declared: int = 0):
        """Get note comments with keyword filtering and quantity limitation

        declared: 原帖显示的评论量（平台声明数）。单篇抓多少条由它决定 ——
        目标是把评论抓到声明量的 90% 以上，而不是固定抓 30 条。

        断点续抓（2026-10-29）：抓取前读取该帖上一次中断时保存的评论分页进度，
        从「最后一个成功抓取的评论之后」继续，而不是从头重翻；抓取中每翻完一页
        持久化一次，翻到底（reached_end）后清掉进度。风控熔断/异常中断时不清除，
        保留最近一页游标供下一轮恢复。
        """
        async with semaphore:
            # 熔断：前面已经连续被风控拦下，后面的笔记就别再撞了
            if self._risk_abort_reason:
                return
            utils.logger.info(f"[XiaoHongShuCrawler.get_comments] Begin get note id comments {note_id}")
            # Use randomized crawling interval (5-15s)
            crawl_interval = _xhs_crawl_interval()
            # 单篇上限：声明数 + 余量；XHS_COMMENT_MAX_PER_NOTE > 0 时再压一道硬顶。
            max_count = _comment_max_count(declared)

            # 断点续抓：读上一次中断保存的进度
            start_cursor = ""
            top_count_in = 0
            sub_cursors_in: Dict[str, str] = {}
            try:
                from tools.v2_sync import (
                    get_comment_resume, save_comment_resume, clear_comment_resume,
                )
                resume = await get_comment_resume("xhs", note_id)
                if resume:
                    start_cursor = resume.get("top_cursor") or ""
                    top_count_in = int(resume.get("top_count") or 0)
                    sub_cursors_in = resume.get("sub_cursors") or {}
                    if start_cursor or sub_cursors_in:
                        utils.logger.info(
                            f"[xhs] 续抓恢复 note {note_id}："
                            f"主评论续抓起点={'有' if start_cursor else '无'}，"
                            f"已抓一级评论 {top_count_in} 条，"
                            f"待续楼中楼 {len(sub_cursors_in)} 个根")
            except Exception as e:
                utils.logger.warning(
                    f"[xhs] 读取评论续抓进度失败 {note_id}: {e}")

            async def _on_progress(nid, state, finished):
                # page 级 / 结束级统一在这里持久化续抓进度
                try:
                    if finished and state.get("reached_end"):
                        # 真翻到底：评论抓全，清掉进度，避免下轮误续或误判
                        await clear_comment_resume("xhs", nid)
                    else:
                        await save_comment_resume(
                            "xhs", nid,
                            state.get("top_cursor", ""),
                            state.get("top_count", 0),
                            state.get("sub_cursors", {}),
                            reached_end=bool(state.get("reached_end", False)),
                        )
                except Exception as e:
                    utils.logger.warning(
                        f"[xhs] 续抓进度持久化失败 {nid}: {e}")

            utils.logger.info(
                f"[XiaoHongShuCrawler.get_comments] note {note_id} "
                f"原帖显示评论 {declared or '未知'} 条 → 本次最多抓 "
                f"{max_count or '不限'} 条一级评论"
                + (f"（续抓：已 {top_count_in} 条）" if top_count_in else ""))
            try:
                await self.xhs_client.get_note_all_comments(
                    note_id=note_id,
                    xsec_token=xsec_token,
                    crawl_interval=crawl_interval,
                    callback=xhs_store.batch_update_xhs_note_comments,
                    max_count=max_count,
                    start_cursor=start_cursor,
                    top_count_in=top_count_in,
                    sub_cursors=sub_cursors_in,
                    progress_callback=_on_progress,
                )
                self._comment_fail_streak = 0
                self._comment_done_count += 1
            except CaptchaError as e:
                # 验证码/风控：首次命中即熔断整批评论。继续打只会越打越死，
                # 而且评论接口是平台最先限的那一层。绝不累计重试。
                self._abort(f"评论接口触发风控：{type(e).__name__}", type(e))
                utils.logger.error(
                    f"[XiaoHongShuCrawler.get_comments] 触发验证码，跳过 {note_id}: {e}")
                # 弹出浏览器让用户人工过验证（每轮一次），通过后当场重试本篇评论
                if await self._human_verify(
                        self._note_web_url(note_id, "pc_search", xsec_token)):
                    try:
                        await self.xhs_client.get_note_all_comments(
                            note_id=note_id,
                            xsec_token=xsec_token,
                            crawl_interval=crawl_interval,
                            callback=xhs_store.batch_update_xhs_note_comments,
                            max_count=max_count,
                            start_cursor=start_cursor,
                            top_count_in=top_count_in,
                            sub_cursors=sub_cursors_in,
                            progress_callback=_on_progress,
                        )
                        self._comment_fail_streak = 0
                        self._comment_done_count += 1
                        utils.logger.info(
                            f"[XiaoHongShuCrawler.get_comments] 人工验证后评论重试成功: {note_id}")
                    except Exception as retry_err:
                        # 重试仍被拦：必须**重新**熔断。此前这里只记日志，
                        # 而 _human_verify 成功时已经清空了 _risk_abort_reason，
                        # 于是下一篇会毫无防备地再撞一次 461
                        # （2026-10-29：6acca231 就是这样多撞了一篇、只抓到 1%）。
                        self._abort(
                            f"人工验证后仍触发风控：{type(retry_err).__name__}",
                            type(retry_err))
                        utils.logger.warning(
                            f"[XiaoHongShuCrawler.get_comments] 人工验证后评论重试失败 "
                            f"{note_id}: {type(retry_err).__name__}（已重新熔断，不再连撞）")
                return
            except (IPBlockError, PlatformAccessError) as e:
                self._abort(f"评论接口触发风控：{type(e).__name__}", type(e))
                utils.logger.error(
                    f"[XiaoHongShuCrawler.get_comments] 被平台限制，跳过 {note_id}: "
                    f"{type(e).__name__}: {str(e)[:160]}")
                if anti_risk.is_captcha_error(e):
                    await self._human_verify(
                        self._note_web_url(note_id, "pc_search", xsec_token))
                return
            except RetryError as e:
                self._comment_fail_streak += 1
                # 评论接口必须带登录态（x-s 签名），登录失效时会被 tenacity 重试到
                # RetryError 抛出。这里必须拦住：一条笔记的评论抓不到，
                # 不应该让整轮抓取直接崩掉（正文/图片已经入库了）。
                # 属于「非风控」失败，累计到阈值再熔断，避免偶发抖动放弃整批。
                root = getattr(getattr(e, "last_attempt", None), "exception", None)
                utils.logger.warning(
                    f"[XiaoHongShuCrawler.get_comments] 评论抓取失败，跳过 {note_id}: "
                    f"{type(root).__name__ if root else 'RetryError'}: {str(root or e)[:200]}")
                self._maybe_trip_risk("评论接口连续失败")
                return
            except Exception as e:
                utils.logger.warning(
                    f"[XiaoHongShuCrawler.get_comments] 评论抓取异常，跳过 {note_id}: {type(e).__name__} {e}")
                return

            # Sleep after fetching comments（5-15s 随机，仅成功抓到才睡；
            # 失败已经白等过重试了，别再叠加）
            await anti_risk.interval_sleep(
                crawl_interval, f"note {note_id} 评论间隔")

    def _abort(self, reason: str, exc_type=None) -> None:
        """命中平台限制时决定怎么熔断。

        - **CaptchaError（461/471 验证码）= 账号已被平台风控**：*首次*命中即熔断
          整批。继续打只会越打越死，这是用户最痛的「触发风控后狂打把账号锁死」
          事故的止血点（2026-10-28：详情阶段被风控后每篇连发 4 个接口、
          零间隔每秒十几条 461，把账号彻底锁死）。
        - **IPBlockError / PlatformAccessError（403/429/IP 限流）/ 其它**：可能是
          单条偶发限流，*连续* RISK_FAIL_LIMIT 次才熔断 —— 避免偶发一条就
          让整批白跑；但连续命中说明账号确实被限流，再打也没用。
        """
        if self._risk_abort_reason:
            return
        is_captcha = exc_type is CaptchaError
        self._comment_fail_streak += 1
        if not is_captcha and self._comment_fail_streak < self.RISK_FAIL_LIMIT:
            # 非验证码：累计到阈值才熔断，偶发单条不限整批
            utils.logger.warning(
                f"[XiaoHongShuCrawler] 受限 {self._comment_fail_streak} 次"
                f"（{reason}），尚未熔断（阈值 {self.RISK_FAIL_LIMIT}）")
            return
        self._risk_abort_reason = reason
        if is_captcha:
            utils.logger.error(
                f"[XiaoHongShuCrawler] 命中验证码风控（461/471），立即熔断后续抓取"
                f" —— 本轮只入库已成功抓取的内容，其余留到冷却后再补。"
                f"原因：{reason}")
        else:
            utils.logger.error(
                f"[XiaoHongShuCrawler] 连续 {self._comment_fail_streak} 次受限"
                f"（{reason}），熔断后续抓取 —— 本轮只入库已成功抓取的内容，"
                f"其余留到冷却后再补。")

    def _maybe_trip_risk(self, reason: str) -> None:
        """*非风控类*失败（登录失效/网络抖动）连续 N 次才熔断。

        风控类错误（461/471/封禁）请用 `_abort` —— 那是要*首次即停*的。
        这里只兜底「登录态彻底失效」之类的非风控连续失败，避免偶发抖动
        就放弃整批，也避免无意义的反复重试浪费时间。
        """
        if self._risk_abort_reason:
            return
        if self._comment_fail_streak < self.RISK_FAIL_LIMIT:
            return
        self._risk_abort_reason = reason
        utils.logger.error(
            f"[XiaoHongShuCrawler] 已连续 {self._comment_fail_streak} 篇评论抓取失败"
            f"（{reason}），熔断后续评论抓取 —— 本轮只入库正文/媒体，"
            f"评论留到冷却后再补。")

    def _note_web_url(self, note_id: str, xsec_source: str,
                      xsec_token: str) -> str:
        """笔记的网页地址：撞风控时把浏览器导到这个页面，用户才看得到验证页。"""
        domain = getattr(self.xhs_client, "_domain", None) or "https://www.xiaohongshu.com"
        token = quote(str(xsec_token or ""), safe="")
        return (f"{domain}/explore/{note_id}?xsec_token={token}"
                f"&xsec_source={xsec_source or 'pc_search'}")

    def _is_headless_now(self) -> bool:
        """当前主流程是否在无头模式跑（无头时用户看不到主窗口，需要另开可见窗口）。

        优先用 `_browser_headless_actual`（启动时记录的实际值）。只按配置推断
        会在「CDP 配了有头、实际回退到标准无头模式」时判错，于是撞验证码时
        既不置前也另开窗口，用户什么都看不到（2026-10-29 踩过）。
        """
        actual = getattr(self, "_browser_headless_actual", None)
        if actual is not None:
            return bool(actual)
        try:
            use_cdp = bool(getattr(config, "XHS_ENABLE_CDP_MODE",
                                   config.ENABLE_CDP_MODE))
            if use_cdp:
                return bool(getattr(config, "XHS_CDP_HEADLESS",
                                    config.CDP_HEADLESS))
            return bool(getattr(config, "HEADLESS", True))
        except Exception:
            return False

    async def _human_verify(self, url: Optional[str] = None) -> bool:
        """撞风控后**弹出浏览器**让用户人工过验证（每轮最多一次）。

        为什么必须弹窗口：461/471 是接口层返回的，浏览器页面本身停在首页、
        看不出任何异常，用户根本不知道该去点什么。所以这里主动
        「窗口置前 + 导航到出事的那个页面」，让验证页/登录墙真的显现出来。

        验证结束后无论成败都做一次 Cookie 回灌（浏览器 → httpx + 账号池）：
        用户在窗口里做的任何操作都体现在 Cookie 上，不同步的话 httpx 还是
        拿旧 Cookie 继续撞 461，等于白点。

        Returns:
            True = 验证通过且熔断已解除，调用方可继续本轮抓取；
            False = 无窗口/没出现验证页/超时（调用方按熔断处理，等冷却）。
        """
        if self._human_verify_tried:
            utils.logger.warning(
                "[XiaoHongShuCrawler] 本轮已尝试过一次人工验证，不再重复弹窗"
                "（避免连续 461 反复打断你）")
            return False

        utils.logger.warning(
            f"[XiaoHongShuCrawler] 命中验证码 → 准备弹出浏览器（"
            f"主页面{'有' if self.context_page is not None else '无'} / "
            f"无头模式={self._is_headless_now()}）。"
            f"若窗口没出现，请检查是否被其它窗口盖住或被移到别的桌面。")

        page = self.context_page
        temp_ctx = None
        need_spawn = page is None or self._is_headless_now()
        if not need_spawn:
            # 主浏览器本身可见，但可能被最小化 / 被别的窗口压住 / 不在当前桌面。
            # 先尝试 OS 级置前；连窗口都找不到，就说明"可见"是假的（例如 CDP
            # 连的其实是个离屏实例），退回到专用可见窗口。
            try:
                await page.bring_to_front()
            except Exception:
                pass
            focused = anti_risk.focus_browser_window()
            if not focused:
                utils.logger.warning(
                    "[XiaoHongShuCrawler] 主浏览器窗口无法置前（找不到窗口句柄），"
                    "改为打开专用可见窗口供你操作")
                need_spawn = True
            else:
                utils.logger.warning(
                    "[XiaoHongShuCrawler] 已把浏览器窗口置前，请在其中完成验证/登录")
        if need_spawn:
            # 两种情况用户都“看不见窗口”，必须另开一个可见的：
            # 1) 无浏览器模式（enable_browser=false），压根没有页面；
            # 2) 主流程跑在无头模式（或窗口无法置前）。
            # 临时浏览器专供人工过验证，用完即关，不影响主流程。
            page, temp_ctx = await self._spawn_verify_browser()
            if page is None:
                utils.logger.warning(
                    "[XiaoHongShuCrawler] 没有可用浏览器，无法弹出验证窗口；"
                    "如需人工过验证，请检查本机 Chrome/Chromium 是否可用")
                return False
            try:
                await page.bring_to_front()
            except Exception:
                pass
            anti_risk.focus_browser_window()

        # 只有「真的有一个可操作窗口」才开始计一次弹窗配额。
        # 之前一进函数就置 True：若这次弹窗根本没成功（起浏览器失败/流程异常），
        # 后面整轮都不会再试，用户永远等不到窗口（2026-10-29）。
        self._human_verify_tried = True

        verified = False
        try:
            verified = await anti_risk.wait_for_human_verify(
                page,
                timeout_sec=_xhs_captcha_wait_sec(),
                url=url or self.index_url,
            )

            # Cookie 回灌：验证/登录动作的结果只存在于浏览器里
            ctx = temp_ctx or self.browser_context
            if ctx is not None:
                try:
                    if self.xhs_client is not None:
                        await self.xhs_client.update_cookies(ctx, self.cookie_urls)
                except Exception as e:
                    utils.logger.warning(
                        f"[XiaoHongShuCrawler] 验证后刷新 httpx Cookie 失败：{e}")
                try:
                    from tools.cookie_sync import sync_login_to_pool

                    await sync_login_to_pool("xhs", ctx, self.cookie_urls)
                    utils.logger.info(
                        "[XiaoHongShuCrawler] 已把浏览器最新登录态同步到账号池")
                except Exception as e:
                    utils.logger.warning(
                        f"[XiaoHongShuCrawler] 验证后同步账号池失败：{e}")
        except Exception as e:
            # 兜底：弹窗流程自身出错时按「未通过」处理，但**必须留痕**。
            # 曾出现「日志里只有重试失败、没有任何弹窗记录」的静默失效，
            # 排查时完全没法定位（2026-10-29）。
            utils.logger.error(
                f"[XiaoHongShuCrawler] 人工验证流程异常（按未通过处理）："
                f"{type(e).__name__}: {e}")
            verified = False
        finally:
            if temp_ctx is not None:
                try:
                    await temp_ctx.close()
                except Exception:
                    pass
                # 临时浏览器的 playwright driver 必须显式 stop，否则
                # 每弹一次窗就留一个 driver 进程（验证码场景会反复出现）
                pw = getattr(self, "_verify_playwright", None)
                if pw is not None:
                    try:
                        await pw.stop()
                    except Exception:
                        pass
                    self._verify_playwright = None

        if verified:
            # 验证通过 → 解除熔断，让本轮继续。只有这一次机会：
            # 若再次 461 就按真正的熔断处理，绝不无限弹窗重试。
            self._risk_abort_reason = ""
            self._comment_fail_streak = 0
            utils.logger.info(
                "[XiaoHongShuCrawler] 人工验证通过，已解除风控熔断，继续本轮抓取")
        return verified

    async def _spawn_verify_browser(self):
        """临时起一个**有头**浏览器，专门给用户过验证用。

        只在没有 context_page（无浏览器模式）时调用；返回
        (page, browser_context)，失败返回 (None, None)。
        """
        try:
            from playwright.async_api import async_playwright

            playwright = await async_playwright().start()
            self._verify_playwright = playwright
            kwargs = chromium_launch_kwargs(playwright.chromium)
            browser = await playwright.chromium.launch(headless=False, **kwargs)
            context = await browser.new_context(
                viewport={"width": 1440, "height": 900}, user_agent=self.user_agent)
            page = await context.new_page()

            # 尽量带上已有登录态，否则打开就是未登录页，验证/扫码都费劲
            cookie_str = ""
            try:
                raw = getattr(config, "COOKIES", "") or ""
                cookie_str = raw if isinstance(raw, str) else ""
            except Exception:
                cookie_str = ""
            if cookie_str:
                try:
                    from tools.crawler_util import convert_str_cookie_to_dict

                    await context.add_cookies([
                        {"name": k, "value": v, "url": self.index_url}
                        for k, v in convert_str_cookie_to_dict(cookie_str).items()
                    ])
                except Exception as e:
                    utils.logger.warning(
                        f"[XiaoHongShuCrawler] 临时浏览器注入 Cookie 失败：{e}")

            utils.logger.warning(
                "[XiaoHongShuCrawler] 已临时打开浏览器窗口用于人工过验证")
            return page, context
        except Exception as e:
            utils.logger.error(
                f"[XiaoHongShuCrawler] 临时浏览器启动失败：{type(e).__name__}: {e}")
            pw = getattr(self, "_verify_playwright", None)
            if pw is not None:
                try:
                    await pw.stop()
                except Exception:
                    pass
                self._verify_playwright = None
            return None, None

    async def create_xhs_client(self, httpx_proxy: Optional[str]) -> XiaoHongShuClient:
        """Create Xiaohongshu client"""
        utils.logger.info("[XiaoHongShuCrawler.create_xhs_client] Begin create Xiaohongshu API client ...")
        if self.browser_context:
            cookie_str, cookie_dict = await utils.convert_browser_context_cookies(
                self.browser_context,
                urls=self.cookie_urls,
            )
        else:
            # 无浏览器模式（#3）：Cookie 由配置提供
            cookie_str = config.COOKIES or ""
            cookie_dict = utils.convert_str_cookie_to_dict(cookie_str)
        xhs_client_obj = XiaoHongShuClient(
            proxy=httpx_proxy,
            headers={
                "accept": "application/json, text/plain, */*",
                "accept-language": "zh-CN,zh;q=0.9",
                "cache-control": "no-cache",
                "content-type": "application/json;charset=UTF-8",
                "origin": self.index_url,
                "pragma": "no-cache",
                "priority": "u=1, i",
                "referer": f"{self.index_url}/",
                "sec-ch-ua": '"Chromium";v="137", "Google Chrome";v="137", "Not.A/Brand";v="99"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-site",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
                "Cookie": cookie_str,
            },
            playwright_page=self.context_page,
            cookie_dict=cookie_dict,
            proxy_ip_pool=self.ip_proxy_pool,  # Pass proxy pool for automatic refresh
        )
        return xhs_client_obj

    async def launch_browser(
        self,
        chromium: BrowserType,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """Launch browser and create browser context"""
        utils.logger.info("[XiaoHongShuCrawler.launch_browser] Begin create browser context ...")
        # playwright 版本与本机浏览器版本不匹配时（要 1228 只有 1194），
        # 回退到已安装的最高版本 Chromium，避免 "Executable doesn't exist" 直接崩
        fallback_kwargs = chromium_launch_kwargs(chromium)
        fallback_exe = fallback_kwargs.get("executable_path")
        if fallback_exe:
            utils.logger.warning(
                f"[XiaoHongShuCrawler.launch_browser] 回退使用本机 Chromium: "
                f"{fallback_exe}（建议执行 python -m playwright install chromium）")
        # 反检测：剔除自动化测试标识（--enable-automation / AutomationControlled），
        # 标准模式只是回退路径，默认应走 CDP 连接真实浏览器
        stealth_args = [
            "--disable-blink-features=AutomationControlled",
            "--exclude-switches=enable-automation",
            "--disable-infobars",
        ]
        fallback_kwargs.setdefault("args", [])
        fallback_kwargs["args"] = list(fallback_kwargs["args"]) + [
            a for a in stealth_args if a not in fallback_kwargs["args"]]
        if config.SAVE_LOGIN_STATE:
            # feat issue #14
            # we will save login state to avoid login every time
            user_data_dir = os.path.join(os.getcwd(), "browser_data", config.USER_DATA_DIR % config.PLATFORM)  # type: ignore
            browser_context = await chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                accept_downloads=True,
                headless=headless,
                proxy=playwright_proxy,  # type: ignore
                viewport={
                    "width": 1920,
                    "height": 1080
                },
                user_agent=user_agent,
                **fallback_kwargs,
            )
            return browser_context
        else:
            browser = await chromium.launch(headless=headless, proxy=playwright_proxy,
                                            **fallback_kwargs)  # type: ignore
            browser_context = await browser.new_context(viewport={"width": 1920, "height": 1080}, user_agent=user_agent)
            return browser_context

    async def launch_browser_with_cdp(
        self,
        playwright: Playwright,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
        connect_existing: Optional[bool] = None,
        auto_close: Optional[bool] = None,
    ) -> BrowserContext:
        """Launch browser using CDP mode"""
        try:
            self.cdp_manager = CDPBrowserManager()
            browser_context = await self.cdp_manager.launch_and_connect(
                playwright=playwright,
                playwright_proxy=playwright_proxy,
                user_agent=user_agent,
                headless=headless,
                connect_existing=connect_existing,
                auto_close=auto_close,
            )

            # Display browser information
            browser_info = await self.cdp_manager.get_browser_info()
            utils.logger.info(f"[XiaoHongShuCrawler] CDP browser info: {browser_info}")

            return browser_context

        except Exception as e:
            utils.logger.error(f"[XiaoHongShuCrawler] CDP mode launch failed, falling back to standard mode: {e}")
            # Fall back to standard mode
            chromium = playwright.chromium
            return await self.launch_browser(chromium, playwright_proxy, user_agent, headless)

    async def close(self):
        """Close browser context

        小红书默认 XHS_AUTO_CLOSE_BROWSER=False：保持浏览器常开，
        登录态/指纹跨任务连续；需要收敛时把配置打开即可。
        """
        auto_close = bool(getattr(config, "XHS_AUTO_CLOSE_BROWSER",
                                  config.AUTO_CLOSE_BROWSER))
        # Special handling if using CDP mode
        if self.cdp_manager:
            await self.cdp_manager.cleanup(auto_close=auto_close)
            self.cdp_manager = None
        elif self.browser_context is not None:
            if auto_close:
                await self.browser_context.close()
            else:
                utils.logger.info(
                    "[XiaoHongShuCrawler.close] AUTO_CLOSE_BROWSER=False，"
                    "浏览器保持常开（登录态连续）")
        utils.logger.info("[XiaoHongShuCrawler.close] Browser context closed ...")

    async def download_media(self, note_detail: Dict) -> None:
        """下载笔记的媒体资源（封面、视频或图文图片）

        Args:
            note_detail: 笔记详情（原始 note_card 结构）
        """
        if not config.ENABLE_GET_MEDIA:
            return
        try:
            items = xhs_media.build_media_items(note_detail)
            if items:
                await self._get_media_downloader().download_all(items)
        except Exception as exc:
            # 媒体下载是旁路能力，解析异常/网络异常都不能中断爬取主流程
            utils.logger.error(f"[XiaoHongShuCrawler.download_media] 媒体下载异常: {exc}")

    def _get_media_downloader(self) -> MediaDownloader:
        """惰性创建媒体下载器，并同步最新的代理设置（代理池是就地刷新的）"""
        if self._media_downloader is None:
            self._media_downloader = MediaDownloader(
                platform="xhs",
                proxy=getattr(self.xhs_client, "proxy", None),
                extra_headers=self._media_headers(),
            )
        else:
            self._media_downloader.update_credentials(
                proxy=getattr(self.xhs_client, "proxy", None),
                extra_headers=self._media_headers(),
            )
        return self._media_downloader

    def _media_headers(self) -> Dict:
        """媒体请求头：平台 Referer（防盗链）+ UA"""
        return {
            "Referer": "https://www.xiaohongshu.com/",
            "User-Agent": self.user_agent,
        }
