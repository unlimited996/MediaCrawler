# -*- coding: utf-8 -*-
"""评论抓取顺序 + 弹窗可见性回归。

背景（2026-10-29 用户反馈）：
1. 「抓取时每篇抓一点评论，应该先排序再集中抓一篇」—— 原先按列表顺序串行，
   中途被风控打断后每篇都只抓了个开头（0%→40%、0%→1%、其余 0%），
   一轮下来没有任何一篇完整。
2. 「弹出验证码风控的时候浏览器没弹出来直接关闭了」—— 主浏览器被判成可见
   但实际不可见（CDP 配了有头、回退到标准无头模式），且弹窗流程自身出错时
   静默吞掉，日志里查不到任何痕迹。
"""
from __future__ import annotations

import asyncio

import pytest

from media_platform.xhs.core import XiaoHongShuCrawler, comment_fetch_order
from tools import anti_risk


def _crawler() -> XiaoHongShuCrawler:
    """绕开 __init__（不碰浏览器/配置），只测纯逻辑。"""
    obj = XiaoHongShuCrawler.__new__(XiaoHongShuCrawler)
    obj._browser_headless_actual = None
    return obj


# ---------------- 评论抓取顺序 ----------------

def test_almost_done_note_goes_first():
    """完成度高的必须排最前：它最可能在本轮被真正抓完（收口）。"""
    progress = {"A": (990, 1000), "B": (10, 1000), "C": (400, 800)}
    key = comment_fetch_order(progress, {})
    assert sorted(progress, key=key)[0] == "A"


def test_untouched_notes_come_after_started_ones():
    """已经抓过一部分的，优先于完全没开抓的（断点续抓更容易收口）。"""
    progress = {"started": (5, 1000), "fresh": (0, 1000)}
    key = comment_fetch_order(progress, {})
    assert sorted(progress, key=key) == ["started", "fresh"]


def test_smaller_comment_section_wins_on_same_ratio():
    """完成度相同时，评论区小的先抓（更快收口、风控暴露更少）。"""
    progress = {"big": (50, 1000), "small": (5, 100)}
    key = comment_fetch_order(progress, {})
    assert sorted(progress, key=key)[0] == "small"


def test_unknown_declared_goes_last():
    """没有声明数（无评论需求/数据缺失）的排最后，别占前面的额度。"""
    progress = {"known": (0, 500), "unknown": (0, 0)}
    key = comment_fetch_order(progress, {})
    assert sorted(progress, key=key)[-1] == "unknown"


def test_declared_map_used_as_fallback():
    """progress 里没有声明数时，用 declared_map 兜底。"""
    key = comment_fetch_order({"x": (0, 0)}, {"x": 300})
    assert key("x")[0] == 1  # 有声明数但还没开抓


# ---------------- 弹窗可见性 ----------------

def test_headless_detection_prefers_actual_state(monkeypatch):
    """实际启动状态优先于配置推断。

    CDP 配 XHS_CDP_HEADLESS=False、但实际回退到标准无头模式时，
    只按配置推断会误判成「窗口可见」→ 既不置前也不另开窗口，
    用户什么都看不到。
    """
    crawler = _crawler()
    crawler._browser_headless_actual = True
    assert crawler._is_headless_now() is True
    crawler._browser_headless_actual = False
    assert crawler._is_headless_now() is False


def test_banner_is_safe_when_page_lacks_evaluate():
    """页面不支持 evaluate（导航中/简易 page）时，横幅不能把等待流程打断。"""
    class _NoEval:
        pass

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        anti_risk._show_banner(_NoEval(), "test"))


def test_manual_window_defaults_to_timeout(monkeypatch):
    """不传 manual_window_sec 时，人工窗口时长跟随 timeout（不再固定 45s 一闪而过）。"""
    seen = {}

    class _Page:
        url = "https://www.xiaohongshu.com/"

        async def title(self):
            return "小红书 - 你的生活指南"

        async def inner_text(self, _s):
            return ""

        async def bring_to_front(self):
            pass

        async def goto(self, *a, **k):
            pass

        async def evaluate(self, *_a, **_k):
            pass

    async def _fake_sleep(sec):
        seen["total"] = seen.get("total", 0) + sec
        # 累计到 50s 就中止，避免真的等 300s
        if seen["total"] >= 50:
            raise _StopWait()

    class _StopWait(Exception):
        pass

    monkeypatch.setattr(anti_risk.asyncio, "sleep", _fake_sleep)
    try:
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            anti_risk.wait_for_human_verify(
                _Page(), timeout_sec=300, poll_sec=5, grace_sec=1))
    except _StopWait:
        pass
    # 只要没有在 45s 就收工即可：它应至少走到 timeout 量级的等待
    assert seen["total"] > 45
