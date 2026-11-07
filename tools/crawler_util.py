# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tools/crawler_util.py
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


# -*- coding: utf-8 -*-
# @Author  : relakkes@gmail.com
# @Time    : 2023/12/2 12:53
# @Desc    : Crawler utility functions

# 本模块被 tools.utils 用 `import *` 引入，而 tools.utils 又被 api/ 下的数据接口
# 大量使用（library 服务的启动链会加载它）。所以顶层只允许「轻量」依赖：
#   * playwright 仅作类型注解  -> TYPE_CHECKING + 延迟求值注解
#   * PIL 只在 show_qrcode（登录扫码）用 -> 函数内导入
# 这样「只跑数据接口 + 前端」的 library 镜像不必安装 playwright / pillow 之外的
# 浏览器依赖；crawler 镜像装了这些包，行为完全不变。
from __future__ import annotations

import base64
import json
import random
import re
import urllib
import urllib.parse
from io import BytesIO
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, cast

import httpx

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查/IDE
    from playwright.async_api import BrowserContext, Cookie, Page

from .httpx_util import make_async_client


async def find_login_qrcode(page: Page, selector: str) -> str:
    """find login qrcode image from target selector"""
    try:
        # 延迟导入：tools.utils 在本模块之上 `from .crawler_util import *`，
        # 顶层互相 import 会让「先 import 谁」决定成败（脆弱）。
        from . import utils

        elements = await page.wait_for_selector(
            selector=selector,
        )
        login_qrcode_img = str(await elements.get_property("src"))  # type: ignore
        if "http://" in login_qrcode_img or "https://" in login_qrcode_img:
            async with make_async_client(follow_redirects=True) as client:
                utils.logger.info(f"[find_login_qrcode] get qrcode by url:{login_qrcode_img}")
                resp = await client.get(login_qrcode_img, headers={"User-Agent": get_user_agent()})
                if resp.status_code == 200:
                    image_data = resp.content
                    base64_image = base64.b64encode(image_data).decode('utf-8')
                    return base64_image
                raise Exception(f"fetch login image url failed, response message:{resp.text}")
        return login_qrcode_img

    except Exception as e:
        print(e)
        return ""


async def find_qrcode_img_from_canvas(page: Page, canvas_selector: str) -> str:
    """
    find qrcode image from canvas element
    Args:
        page:
        canvas_selector:

    Returns:

    """

    # Wait for Canvas element to load
    canvas = await page.wait_for_selector(canvas_selector)

    # Take screenshot of Canvas element
    screenshot = await canvas.screenshot()

    # Convert screenshot to base64 format
    base64_image = base64.b64encode(screenshot).decode('utf-8')
    return base64_image


def show_qrcode(qr_code) -> None:  # type: ignore
    """parse base64 encode qrcode image and show it"""
    # PIL 只在这里用到：延迟导入，避免 library 服务（不扫码登录）被拖进图片库
    from PIL import Image, ImageDraw, ImageShow

    if "," in qr_code:
        qr_code = qr_code.split(",")[1]
    qr_code = base64.b64decode(qr_code)
    image = Image.open(BytesIO(qr_code))

    # Add a square border around the QR code and display it within the border to improve scanning accuracy.
    width, height = image.size
    new_image = Image.new('RGB', (width + 20, height + 20), color=(255, 255, 255))
    new_image.paste(image, (10, 10))
    draw = ImageDraw.Draw(new_image)
    draw.rectangle((0, 0, width + 19, height + 19), outline=(0, 0, 0), width=1)
    del ImageShow.UnixViewer.options["save_all"]
    new_image.show()


def get_user_agent() -> str:
    ua_list = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/104.0.5112.79 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/104.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/104.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/103.0.5060.53 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_3) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/99.0.4844.84 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.5112.79 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.5060.53 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_3) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.4844.84 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.5112.79 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.5060.53 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_3) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.4844.84 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.5112.79 Safari/537.36"
    ]
    return random.choice(ua_list)


def get_mobile_user_agent() -> str:
    ua_list = [
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1"
    ]
    return random.choice(ua_list)


def convert_cookies(cookies: Optional[List[Cookie]]) -> Tuple[str, Dict]:
    if not cookies:
        return "", {}
    cookies_str = ";".join([f"{cookie.get('name')}={cookie.get('value')}" for cookie in cookies])
    cookie_dict = dict()
    for cookie in cookies:
        cookie_dict[cookie.get('name')] = cookie.get('value')
    return cookies_str, cookie_dict


async def convert_browser_context_cookies(
    browser_context: BrowserContext, urls: Optional[List[str]] = None
) -> Tuple[str, Dict]:
    cookies = (
        await browser_context.cookies(urls=urls)
        if urls
        else await browser_context.cookies()
    )
    return convert_cookies(cookies)


def convert_str_cookie_to_dict(cookie_str: str) -> Dict:
    cookie_dict: Dict[str, str] = dict()
    if not cookie_str:
        return cookie_dict
    for cookie in cookie_str.split(";"):
        cookie = cookie.strip()
        if not cookie:
            continue
        cookie_list = cookie.split("=")
        if len(cookie_list) != 2:
            continue
        cookie_value = cookie_list[1]
        if isinstance(cookie_value, list):
            cookie_value = "".join(cookie_value)
        cookie_dict[cookie_list[0]] = cookie_value
    return cookie_dict


def match_interact_info_count(count_str: str) -> int:
    if not count_str:
        return 0

    match = re.search(r'\d+', count_str)
    if match:
        number = match.group()
        return int(number)
    else:
        return 0


def safe_int(value, default: int = 0) -> int:
    """把平台接口返回的计数字段安全地转成 int。

    平台返回的计数经常是**字符串**（如 "0"），直接 `x == 0` 判断会漏掉它
    （Python 中非空字符串 truthy 且 "0" != 0），再拿去 `//` 整除会直接 TypeError。
    结果是：对没有回复的评论也逐个发起请求，白白放大请求量并触发风控。

    另外平台爱用中文万/亿或 k/w 缩写做展示值（小红书 "2万"、B站 "1.2万"），
    这类值必须按单位换算，否则 "2万" 会变成 2，排序/统计全错。
    """
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        pass
    # 形如 "1.2万" / "3k" / "1,234" 的展示值：提取数值并按单位换算
    match = re.search(r"(\d+(?:\.\d+)?)\s*(亿|万|w|W|k|K|千)?", str(value))
    if not match:
        return default
    num = float(match.group(1))
    unit = match.group(2)
    if unit == "亿":
        num *= 100_000_000
    elif unit in ("万", "w", "W"):
        num *= 10_000
    elif unit in ("k", "K", "千"):
        num *= 1_000
    return int(num)


def format_proxy_info(ip_proxy_info) -> Tuple[Optional[Dict], Optional[str]]:
    """format proxy info for playwright and httpx"""
    # fix circular import issue
    from proxy.proxy_ip_pool import IpInfoModel
    ip_proxy_info = cast(IpInfoModel, ip_proxy_info)

    # Playwright proxy server should be in format "host:port" without protocol prefix
    server = f"{ip_proxy_info.ip}:{ip_proxy_info.port}"
    
    playwright_proxy = {
        "server": server,
    }
    
    # Only add username and password if they are not empty
    if ip_proxy_info.user and ip_proxy_info.password:
        playwright_proxy["username"] = ip_proxy_info.user
        playwright_proxy["password"] = ip_proxy_info.password
    
    # httpx 0.28.1 requires passing proxy URL string directly, not a dictionary
    if ip_proxy_info.user and ip_proxy_info.password:
        httpx_proxy = f"http://{ip_proxy_info.user}:{ip_proxy_info.password}@{ip_proxy_info.ip}:{ip_proxy_info.port}"
    else:
        httpx_proxy = f"http://{ip_proxy_info.ip}:{ip_proxy_info.port}"
    return playwright_proxy, httpx_proxy


def extract_text_from_html(html: str) -> str:
    """Extract text from HTML, removing all tags."""
    if not html:
        return ""

    # Remove script and style elements
    clean_html = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html, flags=re.DOTALL)
    # Remove all other tags
    clean_text = re.sub(r'<[^>]+>', '', clean_html).strip()
    return clean_text

def extract_url_params_to_dict(url: str) -> Dict:
    """Extract URL parameters to dict"""
    url_params_dict = dict()
    if not url:
        return url_params_dict
    parsed_url = urllib.parse.urlparse(url)
    url_params_dict = dict(urllib.parse.parse_qsl(parsed_url.query))
    return url_params_dict


def system_playwright_proxy() -> Optional[Dict]:
    """无代理池配置时，回退读取系统代理环境变量，转成 playwright proxy 格式。

    背景：Chromium 不像 httpx/requests 那样读 http_proxy 环境变量。在需要
    代理的网络环境（公司网络/软路由/科学上网）下，不显式给浏览器传代理会导致
    Page.goto 超时、整个抓取崩溃；而同一环境里 httpx 客户端却能正常走代理，
    故障现象非常割裂（头像代理成功、浏览器打不开网页）。
    """
    import os
    from urllib.parse import urlparse

    raw = (os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
           or os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY")
           or "").strip()
    if not raw or raw.lower() in ("none", "direct"):
        return None
    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    if not parsed.hostname or not parsed.port:
        return None
    proxy: Dict[str, str] = {"server": f"{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password or ""
    return proxy


def chromium_launch_kwargs(browser_type=None) -> Dict[str, Any]:
    """给 chromium.launch*() 补启动参数。

    两件事：
    1. 容器内以 root 跑 Chromium 必须关沙箱，否则进程直接退出（报 "Target closed"
       或 "Running as root without --no-sandbox is not supported"）。
       由环境变量 PLAYWRIGHT_NO_SANDBOX=1 打开，本地 Windows 不设就不受影响。
    2. 解决「浏览器版本对不上」——见下方原注释。

    用法：
        kwargs = chromium_launch_kwargs(chromium)
        await chromium.launch_persistent_context(..., **kwargs)
    """
    import os

    kwargs: Dict[str, Any] = {}

    # ① 容器（Docker / root）环境：关沙箱 + 避坑选项
    if str(os.environ.get("PLAYWRIGHT_NO_SANDBOX", "")).strip().lower() in ("1", "true", "yes", "on"):
        kwargs["args"] = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",  # 容器 /dev/shm 默认 64M，不关会崩
            "--disable-gpu",
        ]

    # ② 浏览器可执行文件路径兜底
    expected = None
    if browser_type is not None:
        try:
            expected = browser_type.executable_path  # playwright 期望的路径（未必存在）
        except Exception:
            expected = None
    if expected and os.path.exists(expected):
        return kwargs                                 # 版本匹配，用默认即可

    candidates: List[Tuple[int, str]] = []

    def _add(path: str, rank: int) -> None:
        if path and os.path.exists(path):
            candidates.append((rank, path))

    # 1) ms-playwright 目录下所有 chromium-<rev>
    #    Windows 布局是 chrome-win/chrome.exe，Linux 是 chrome-linux/chrome
    root = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
            or os.path.join(os.path.expanduser("~"), "AppData", "Local", "ms-playwright"))
    if os.path.isdir(root):
        for name in os.listdir(root):
            is_full = name.startswith("chromium-")
            is_shell = name.startswith("chromium_headless_shell-")
            if not (is_full or is_shell):
                continue
            try:
                # 两种目录都以修订号结尾：chromium-1248 /
                # chromium_headless_shell-1248，用 rsplit 才都能取到 1248
                rev = int(name.rsplit("-", 1)[-1])
            except (IndexError, ValueError):
                rev = 0
            base = os.path.join(root, name)
            if is_shell:
                # 为省镜像体积，构建时常常只装 chromium-headless-shell。
                # 它同样支持 launch_persistent_context（已实测），
                # 不认识这个目录就会退回默认的完整 chromium 路径而崩溃。
                for sub in ("chrome-headless-shell-linux64",
                            "chrome-headless-shell-win64",
                            "chrome-headless-shell-mac-x64",
                            "chrome-headless-shell-mac-arm64"):
                    _add(os.path.join(base, sub, "chrome-headless-shell"), rev * 10)
                    _add(os.path.join(base, sub, "chrome-headless-shell.exe"), rev * 10)
            else:
                # 同版本时完整 chromium 优先（rank 高 1），功能更全
                for sub in ("chrome-win", "chrome-win64",
                            "chrome-linux", "chrome-linux64"):
                    _add(os.path.join(base, sub, "chrome.exe"), rev * 10 + 1)
                    _add(os.path.join(base, sub, "chrome"), rev * 10 + 1)

    # 2) 系统安装的 Chrome / Edge（兜底）
    for p in (os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                           "Google", "Chrome", "Application", "chrome.exe"),
              os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                           "Google", "Chrome", "Application", "chrome.exe"),
              os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                           "Microsoft", "Edge", "Application", "msedge.exe")):
        _add(p, -1)

    if not candidates:
        return kwargs
    candidates.sort(key=lambda x: x[0], reverse=True)
    kwargs["executable_path"] = candidates[0][1]
    return kwargs
