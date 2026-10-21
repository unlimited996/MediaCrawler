# -*- coding: utf-8 -*-
"""发现页：HomeFeed 与热搜榜单（方案 #10）。

热榜优先挑**公开接口**（无需登录/签名）实现，保证能真跑；
HomeFeed 各平台差异大，提供统一入口 + 平台扩展点，未实现时给出明确提示而不是静默失败。
"""
from .hot import fetch_hot_board, capture_hot_board, SUPPORTED_HOT_BOARDS
from .homefeed import fetch_homefeed, SUPPORTED_HOMEFEED

__all__ = [
    "fetch_hot_board", "capture_hot_board", "SUPPORTED_HOT_BOARDS",
    "fetch_homefeed", "SUPPORTED_HOMEFEED",
]
