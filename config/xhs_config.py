# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/config/xhs_config.py
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


# Xiaohongshu platform configuration

# Sorting method, the specific enumeration value is in media_platform/xhs/field.py
SORT_TYPE = "popularity_descending"

# Specify the note URL list, which must carry the xsec_token parameter
XHS_SPECIFIED_NOTE_URL_LIST = [
    "https://www.xiaohongshu.com/explore/6aa3d827000000002802c5c8?xsec_token=YBIq8sY-0_K3BACQ7z57J9xJcdflrd8BAf5_zBeJtFMOQ=&xsec_source=pc_creatormng"
    # ........................
]

# Specify the creator URL list, which needs to carry xsec_token and xsec_source parameters.

XHS_CREATOR_ID_LIST = [
    "https://www.xiaohongshu.com/user/profile/5f58bd990000000001003753?xsec_token=ABYVg1evluJZZzpMX-VWzchxQ1qSNVW3r-jOEnKqMcgZw=&xsec_source=pc_search"
    # ........................
]

# ---- 环境变量助手（与 base_config 的同名实现保持一致，避免循环导入）----
import os as _os


def _env_int(name: str, default: int) -> int:
    raw = _os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _os.getenv(name)
    if raw is None or raw == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = _os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


# ==================== 小红书反检测 / 反风控配置 ====================
# 目标：最大化复用本地真实浏览器的登录态与浏览器指纹，把「机器特征」
# 降到最低；请求节奏全部随机化，避免固定间隔被风控识别。
#
# 以下每一项都可用环境变量覆盖（便于 Docker / 临时切换），
# 也可在 WebUI 配置面板（crawl.cdp_* 系列）调整。

# 是否默认走 CDP 模式：连接本地真实 Chrome/Edge（复用登录态、扩展与历史），
# 不带 Playwright 的自动化测试标识（--enable-automation 等已剔除）
XHS_ENABLE_CDP_MODE = _env_bool("XHS_ENABLE_CDP_MODE", True)

# True = 连接用户已经开着远程调试的浏览器（反检测效果最好）；
# False = 由程序自己拉起一个带调试端口的浏览器
XHS_CDP_CONNECT_EXISTING = _env_bool("XHS_CDP_CONNECT_EXISTING", True)

# CDP 模式是否无头。小红书默认关闭无头（有头窗口风控阈值最低，
# 且验证码弹出时用户能第一时间看到并处理）
XHS_CDP_HEADLESS = _env_bool("XHS_CDP_HEADLESS", False)

# 抓取结束后是否关闭浏览器。小红书默认 False：保持浏览器常开，
# 登录态/Cookie/指纹跨任务连续，避免每次冷启动
XHS_AUTO_CLOSE_BROWSER = _env_bool("XHS_AUTO_CLOSE_BROWSER", False)

# 并发数。小红书默认 1：串行请求最像真人，风控最低
XHS_MAX_CONCURRENCY_NUM = _env_int("XHS_MAX_CONCURRENCY_NUM", 1)

# 抓取请求之间的随机间隔（秒）：每次在 [MIN, MAX] 里随机取一个值
XHS_SLEEP_MIN_SEC = _env_float("XHS_SLEEP_MIN_SEC", 5)
XHS_SLEEP_MAX_SEC = _env_float("XHS_SLEEP_MAX_SEC", 15)

# 触发安全验证时，等待用户人工完成验证的最长时间（秒）
XHS_CAPTCHA_WAIT_SEC = _env_int("XHS_CAPTCHA_WAIT_SEC", 300)
