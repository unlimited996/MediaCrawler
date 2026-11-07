# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/config/base_config.py
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

import os


def _env_int(name: str, default: int) -> int:
    """读整数型环境变量（非法值回退默认）。

    容器部署时没法改这份文件，只能通过环境变量注入，所以关键数值参数都走这里。
    """
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    """读布尔型环境变量（1/true/yes/on 都算真）。

    容器部署时没法改这份文件，只能通过环境变量注入，所以关键开关都走这里。
    """
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_str(name: str, default: str) -> str:
    """读字符串型环境变量（空串也回退默认）。"""
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip()


def _env_float(name: str, default: float) -> float:
    """读浮点型环境变量（非法值回退默认）。"""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


# Basic configuration
PLATFORM = "xhs"  # Platform, xhs | dy | ks | bili | wb | tieba | zhihu

# 是否使用海外版小红书 (rednote.com)
# 开启后 API 走 webapi.rednote.com，cookie 域使用 .rednote.com
XHS_INTERNATIONAL = False

# 搜索关键词：**不设默认值**。搜索模式下为空会直接拒绝启动，
# 避免出现"用户没填却默默用内置关键词抓取"的诡异行为。
KEYWORDS = ""  # Keyword search configuration, separated by English commas
LOGIN_TYPE = "qrcode"  # qrcode or phone or cookie
COOKIES = ""
CRAWLER_TYPE = (
    "search"  # Crawling type, search (keyword search) | detail (post details) | creator (creator homepage data)
)
# Whether to enable IP proxy
ENABLE_IP_PROXY = False

# Number of proxy IP pools
IP_PROXY_POOL_COUNT = 2

# Proxy IP provider name
IP_PROXY_PROVIDER_NAME = "kuaidaili"  # kuaidaili | wandouhttp | static

# Static proxy configuration (used when IP_PROXY_PROVIDER_NAME is set to "static")
# Format: "http://your_home_domain:port" or "http://user:password@your_home_domain:port"
STATIC_PROXY_URL = ""

# Setting to True will not open the browser (headless browser)
# Setting False will open a browser
# If Xiaohongshu keeps scanning the code to log in but fails, open the browser and manually pass the sliding verification code.
# If Douyin keeps prompting failure, open the browser and see if mobile phone number verification appears after scanning the QR code to log in. If it does, manually go through it and try again.
# 支持环境变量覆盖：容器里没有图形界面，必须 headless（Dockerfile / compose 里设 HEADLESS=true）
HEADLESS = _env_bool("HEADLESS", False)

# Whether to save login status
SAVE_LOGIN_STATE = True

# ==================== CDP (Chrome DevTools Protocol) 配置 ====================
# 是否启用 CDP 模式 - 使用用户本地的 Chrome/Edge 浏览器进行爬取，具有更好的反检测能力
# 开启后，会自动检测并启动用户的 Chrome/Edge 浏览器，通过 CDP 协议进行控制
# 该方式使用真实浏览器环境，包括用户的扩展、Cookie 和设置，大幅降低被风控检测的风险
ENABLE_CDP_MODE = False

# CDP 调试端口，用于与浏览器通信
# 如果端口被占用，系统会自动尝试下一个可用端口
CDP_DEBUG_PORT = 9222

# CDP 浏览器所在的主机。默认 localhost = 本机开发（连这台机器上开的 Chrome）。
# 容器里想连局域网/宿主机另一台电脑上的 Chrome 时，把它设成那台机器的 IP，
# 例如 CDP_HOST=192.168.10.50；容器里的 localhost 指的是容器自己，
# 不设的话永远连不到外部浏览器。
CDP_HOST = _env_str("CDP_HOST", "localhost")

# 自定义浏览器路径（可选）
# 如果为空，系统会自动检测 Chrome/Edge 的安装路径
# Windows 示例: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
# macOS 示例: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CUSTOM_BROWSER_PATH = ""

# 是否在 CDP 模式下启用无头模式
# 注意：即使设置为 True，某些反检测功能在无头模式下可能无法正常工作
CDP_HEADLESS = False

# 浏览器启动超时时间（秒）
BROWSER_LAUNCH_TIMEOUT = 60

# 是否连接用户已打开的浏览器，而不是启动新的浏览器
# 开启后，程序会连接一个已经启用了远程调试的浏览器
# 用户需要在 Chrome 中开启远程调试：chrome://inspect/#remote-debugging
# 或者使用命令行参数启动 Chrome：--remote-debugging-port=9222
# 这种方式反检测效果最好，因为直接使用用户真实浏览器的所有 Cookie、扩展和浏览历史
CDP_CONNECT_EXISTING = True

# 程序结束时是否自动关闭浏览器
# 设置为 False 可以保持浏览器运行，方便调试
AUTO_CLOSE_BROWSER = True

# Data saving type option configuration, supports: csv, db, json, jsonl, sqlite, excel, postgres. It is best to save to DB, with deduplication function.
SAVE_DATA_OPTION = "jsonl"  # csv or db or json or jsonl or sqlite or excel or postgres

# Data saving path, if not specified by default, it will be saved to the data folder.
SAVE_DATA_PATH = ""

# Browser file configuration cached by the user's browser
USER_DATA_DIR = "%s_user_data_dir"  # %s will be replaced by platform name

# The number of pages to start crawling starts from the first page by default
START_PAGE = 1

# Control the number of crawled videos/posts
# 主页/创作者全量拉取时默认不限量（翻页到 has_more=false 为止），
# 定时任务可在任务配置里单独覆盖
CRAWLER_MAX_NOTES_COUNT = _env_int("CRAWLER_MAX_NOTES_COUNT", 15)

# Controlling the number of concurrent crawlers
# 定时任务按任务的「并发数」配置通过环境变量注入
MAX_CONCURRENCY_NUM = _env_int("MAX_CONCURRENCY_NUM", 1)

# 是否启用媒体下载（封面、视频，以及图文帖的图片），默认关闭。
# 开启后媒体文件按 {SAVE_DATA_PATH 或 data}/{platform}/media/{内容ID}/ 目录聚合存放。
# 支持的平台：xhs / dy / ks / bili / wb（tieba、zhihu 的数据结构中没有媒体字段，不支持）。
# 命令行开关：--get_media
ENABLE_GET_MEDIA = False

# Whether to enable comment crawling mode. Comment crawling is enabled by default.
ENABLE_GET_COMMENTS = True

# Control the number of crawled first-level comments (single video/post)
# 原默认 10 只覆盖小红书评论接口的第一页，热门帖（上千条评论）几乎必然漏采。
# 30 = 首页 + 一页翻页的典型量，请求数可控。需要更多可在配置面板/CLI 覆盖。
CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES = 30

# 评论完整率目标：已抓评论数 / 原帖显示评论量 达到该比例即视为「抓全了」，
# 增量任务下次不再重抓；未达标则继续补抓（不再有「最多重试 N 次」的硬上限）。
# 用途见 tools/v2_sync.get_archived_content_ids()。
# 可用环境变量覆盖（配置页 comment_completeness_target 经 build_crawler_env 注入）
COMMENT_COMPLETENESS_TARGET = _env_float("COMMENT_COMPLETENESS_TARGET", 0.9)

# Whether to enable the mode of crawling second-level comments. By default, crawling of second-level comments is not enabled.
# If the old version of the project uses db, you need to refer to schema/tables.sql line 287 to add table fields.
ENABLE_GET_SUB_COMMENTS = False

# word cloud related
# Whether to enable generating comment word clouds
ENABLE_GET_WORDCLOUD = False
# Custom words and their groups
# Add rule: xx:yy where xx is a custom-added phrase, and yy is the group name to which the phrase xx is assigned.
CUSTOM_WORDS = {
    "零几": "年份",  # Recognize "zero points" as a whole
    "高频词": "专业术语",  # Example custom words
}

# Deactivate (disabled) word file path
STOP_WORDS_FILE = "./docs/hit_stopwords.txt"

# Chinese font file path
FONT_PATH = "./docs/STZHONGS.TTF"

# Crawl interval
# 主页翻页 / 详情请求之间的 sleep 秒数，即「限流」间隔。
# 定时任务按任务的「限流间隔」配置通过环境变量注入。
CRAWLER_MAX_SLEEP_SEC = _env_int("CRAWLER_MAX_SLEEP_SEC", 2)

# 打开首页的超时时间（秒）。弱网 / 代理环境下小红书首页资源多，
# Playwright 默认的 30s + wait_until='load' 经常卡死导致整个抓取崩在第一步。
# 代码里已改成 domcontentloaded 并重试 3 次，这里只控制单次等待上限。
PAGE_GOTO_TIMEOUT_SEC = _env_int("PAGE_GOTO_TIMEOUT_SEC", 45)

# ---------------------------------------------------------------------------
# 定时拉取任务（api/services/task_scheduler.py）
# ---------------------------------------------------------------------------
# 调度器总开关：关掉后已存任务不自动执行（手动触发仍可用）
SCHEDULER_ENABLED = _env_bool("SCHEDULER_ENABLED", True)
# 调度器跑在哪个进程（2026-11-04 拆分）：
#   0（默认）→ 交给独立执行器 `python -m worker`，API 只提供接口。
#              好处：重启 API 不会打断抓取，重启执行器也不会让接口掉线。
#   1        → 老行为，调度器跑在 API 进程内（单进程调试/旧部署兼容）。
SCHEDULER_IN_API = _env_bool("SCHEDULER_IN_API", False)
# API 启动时若发现执行器没在跑，是否自动拉起（detached，父进程退出不带走）。
# 设为 0 则要求手动 `python -m worker`，适合用 systemd/supervisor 托管。
WORKER_AUTO_SPAWN = _env_bool("WORKER_AUTO_SPAWN", True)

# ---------------------------------------------------------------------------
# 微服务拆分 / Nacos 服务发现（2026-11-04）
# ---------------------------------------------------------------------------
# 两个独立应用，URL 里的服务名决定打到哪个应用：
#   /crawler/api/**  -> crawler 服务（抓取 + 调度 + 账号）
#   /library/api/**  -> library 内容库（帖子/评论/媒体/标签/菜谱 + 配置 + S3）
# 每个进程用 SERVICE_NAME 表明自己是谁，启动时注册到 Nacos。
SERVICE_NAME = _env_str("SERVICE_NAME", "mediacrawler")
# 本进程监听端口（容器内/宿主端口由部署决定）
SERVICE_PORT = _env_int("SERVICE_PORT", 8081)
# 注册到 Nacos 的 IP；留空自动探测（容器里就是容器 IP）
SERVICE_IP = _env_str("SERVICE_IP", "")

# Nacos 开关与地址（注册/发现都走它；关掉则退化为静态地址）
NACOS_ENABLED = _env_bool("NACOS_ENABLED", True)
# ⚠️ 这里填 **OpenAPI 端口**（Nacos 的 8848，宿主映射 9000），不是控制台端口。
# Nacos 3.x 的控制台在 8080（宿主 8085），把它填进来会注册失败。
NACOS_SERVER = _env_str("NACOS_SERVER", "www.joyeuse.cn:9000")
# 客户端 API 版本：auto（默认，自动探测）/ v3 / v1
NACOS_API = _env_str("NACOS_API", "auto")
# 保活间隔（秒）：Nacos 3.x 没有 beat 接口，靠「定时重新注册」续期；
# 实测 5s 一次可稳定保持 healthy（>15s 变 unhealthy，>30s 被摘除）
NACOS_HEARTBEAT_SEC = _env_float("NACOS_HEARTBEAT_SEC", 5)
NACOS_NAMESPACE = _env_str("NACOS_NAMESPACE", "public")
NACOS_GROUP = _env_str("NACOS_GROUP", "DEFAULT_GROUP")
NACOS_USERNAME = _env_str("NACOS_USERNAME", "")
NACOS_PASSWORD = _env_str("NACOS_PASSWORD", "")

# 两个服务的注册名（Nacos 里的 serviceName），也是网关路由的目标
CRAWLER_SERVICE_NAME = _env_str("CRAWLER_SERVICE_NAME", "mediacrawler-crawler")
LIBRARY_SERVICE_NAME = _env_str("LIBRARY_SERVICE_NAME", "mediacrawler-library")

# Nacos 不可用时的静态兜底地址（本地开发/单机起两个进程最常用）
CRAWLER_BASE_URL = _env_str("CRAWLER_BASE_URL", "http://127.0.0.1:8001")
LIBRARY_BASE_URL = _env_str("LIBRARY_BASE_URL", "http://127.0.0.1:8081")
# 内容库的 API 前缀。story.py 拼「媒体/原始文件」链接时必须用同一份前缀，
# 否则拆分部署下前端拿到的还是老的 /api/data/... → 404。
LIBRARY_API_PREFIX = _env_str("LIBRARY_API_PREFIX", "/library/api")
# 网关（服务名路由 + 静态页）是否随 data 服务一起启动
GATEWAY_ENABLED = _env_bool("GATEWAY_ENABLED", True)
# 跨服务调用/代理的超时（秒）
SERVICE_CALL_TIMEOUT = _env_float("SERVICE_CALL_TIMEOUT", 30)
# 同时执行的定时任务上限。抓取是单子进程模型，默认 1 最稳
SCHEDULER_MAX_CONCURRENCY = _env_int("SCHEDULER_MAX_CONCURRENCY", 1)
# 调度循环粒度（秒）：每个 tick 检查哪些任务到点
SCHEDULER_TICK_SEC = _env_int("SCHEDULER_TICK_SEC", 15)
# 到点时爬虫正忙：等待腾出抓取通道的最长时间（秒），超时记 skipped。
# 一次手动重抓（整帖+楼中楼）常要 15-25 分钟，300s 太短会让刚点的任务
# 静悄悄被跳过，故默认放宽到 30 分钟；等待期间每 30s 打一条排队日志。
SCHEDULER_BUSY_WAIT_SEC = _env_int("SCHEDULER_BUSY_WAIT_SEC", 1800)
# 单次执行的运行记录保留条数（按任务）
SCHEDULER_RUN_HISTORY = _env_int("SCHEDULER_RUN_HISTORY", 100)

# ---- 反风控（调度侧）----
# 任务只允许在 [START, END) 点执行（本地时区）。默认 6-24 点：
# 深夜不抓，避开平台风控值守时段，也省账号。
SCHEDULER_ACTIVE_HOUR_START = _env_int("SCHEDULER_ACTIVE_HOUR_START", 6)
SCHEDULER_ACTIVE_HOUR_END = _env_int("SCHEDULER_ACTIVE_HOUR_END", 24)
# 任务真正发起前的随机抖动区间（秒）：每次错开 1-5s，
# 避免多个任务整点齐步走打接口（固定节奏是风控特征）
SCHEDULER_JITTER_MIN_SEC = _env_float("SCHEDULER_JITTER_MIN_SEC", 1)
SCHEDULER_JITTER_MAX_SEC = _env_float("SCHEDULER_JITTER_MAX_SEC", 5)
# 窗口外到点的任务推迟到窗口起点时，再叠加的随机分散上限（秒），
# 防止开窗瞬间所有任务同时起跑
SCHEDULER_WINDOW_SPREAD_SEC = _env_int("SCHEDULER_WINDOW_SPREAD_SEC", 300)
# 风控冷却期（小时）：一轮任务因验证码/限流熔断后，同平台（同账号）在
# 这段时间内不再自动发起新任务。平台对 461/471 的惩罚窗口通常以小时计，
# 冷却期内继续跑只会把账号越推越死（2026-10-29：#27 熔断 32 分钟后又跑，
# 43 篇就被二次拦住）。设 0 表示不启用冷却。
RISK_COOLDOWN_HOURS = _env_int("RISK_COOLDOWN_HOURS", 6)

# ---- S3 媒体备份联动删除 ----
# 原贴被硬删除时，是否把该帖的 S3 备份对象一并删除（默认开）。
# 关掉后保留「帖子删了仍可靠 object_key 取回」的旧语义；
# 删除失败的对象转 orphaned 状态，由后台循环自动重试 / 管理页手动清理。
S3_BACKUP_PURGE_WITH_STORY = _env_bool("S3_BACKUP_PURGE_WITH_STORY", True)

# 是否禁用 SSL 证书验证。仅在使用企业代理、Burp Suite、mitmproxy 等会注入自签名证书的中间人代理时设为 True。
# 警告：禁用 SSL 验证将使所有流量暴露于中间人攻击风险，请勿在生产环境中开启。
DISABLE_SSL_VERIFY = False

from .bilibili_config import *
from .xhs_config import *
from .dy_config import *
from .ks_config import *
from .weibo_config import *
from .tieba_config import *
from .zhihu_config import *
