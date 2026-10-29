# -*- coding: utf-8 -*-
"""验证码人工介入（弹出浏览器）行为回归。

背景（2026-10-29）：撞 461 时旧逻辑要么根本不弹浏览器（内层捕获直接 return），
要么弹了却"页面还没渲染出验证页"就立刻判「已完成」，下一发请求又撞 461。
这里锁住三点：
- 有验证页 → 必须等它消失才放行；
- 页面无验证页 → 给人工窗口后按「Cookie 不同步」返回 True，让调用方重试一次；
- 没有页面 → 直接 False，不空等。
"""
from __future__ import annotations

import asyncio

import pytest

from tools import anti_risk


class _FakePage:
    """只实现 page_has_captcha 用到的接口。"""

    def __init__(self, titles):
        self._titles = list(titles)
        self.url = "https://www.xiaohongshu.com/explore/abc"
        self.focused = False
        self.goto_urls = []

    async def title(self):
        return self._titles.pop(0) if len(self._titles) > 1 else self._titles[0]

    async def inner_text(self, _selector):
        return ""

    async def bring_to_front(self):
        self.focused = True

    async def goto(self, url, **_kwargs):
        self.goto_urls.append(url)


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_no_page_returns_false_immediately():
    """没有可操作页面时立刻返回 False，不空等。"""
    assert _run(anti_risk.wait_for_human_verify(None, timeout_sec=300)) is False


def test_captcha_must_disappear_before_passing():
    """出现验证页后，必须等它消失才算通过。"""
    page = _FakePage(["安全验证", "安全验证", "小红书 - 你的生活指南"])
    ok = _run(anti_risk.wait_for_human_verify(
        page, timeout_sec=30, poll_sec=0, grace_sec=0, manual_window_sec=0))
    assert ok is True
    assert page.focused is True, "必须把窗口置前，否则用户看不到要操作什么"


def test_page_without_captcha_opens_manual_window_then_true():
    """页面正常（浏览器侧能访问）时给人工窗口，然后返回 True 让调用方重试一次。"""
    page = _FakePage(["小红书 - 你的生活指南"])
    ok = _run(anti_risk.wait_for_human_verify(
        page, timeout_sec=30, poll_sec=0, grace_sec=0, manual_window_sec=0))
    assert ok is True


def test_navigates_to_failing_url():
    """必须导航到出事的页面，否则页面上不会出现验证码。"""
    page = _FakePage(["安全验证", "小红书"])
    _run(anti_risk.wait_for_human_verify(
        page, timeout_sec=20, poll_sec=0, grace_sec=0, manual_window_sec=0,
        url="https://www.xiaohongshu.com/explore/xyz"))
    assert page.goto_urls == ["https://www.xiaohongshu.com/explore/xyz"]


def test_timeout_when_captcha_never_clears(monkeypatch):
    """验证页一直不消失 → 超时返回 False（调用方按熔断处理）。"""
    page = _FakePage(["安全验证"])
    ok = _run(anti_risk.wait_for_human_verify(
        page, timeout_sec=2, poll_sec=1, grace_sec=0, manual_window_sec=0))
    assert ok is False


def test_focus_window_helper_is_safe_on_any_platform():
    """窗口置前在非 Windows / 无窗口时不能抛异常。"""
    assert isinstance(anti_risk.focus_browser_window(), bool)
