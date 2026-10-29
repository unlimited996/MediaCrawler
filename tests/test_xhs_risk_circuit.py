# -*- coding: utf-8 -*-
"""评论抓取的「风控熔断」回归测试。

背景（2026-10-28）：account 模式一次性抓 100 篇点赞帖，全部撞上
HTTP 461 验证码。旧代码对每篇只记一条 warning 后继续，且验证码异常是裸
Exception → 被 tenacity 重试 3 次，等于对着风控连打 300 次请求；跑完仍报
success，用户只能看到「帖子进来了、评论是空的」。

更严重的是：详情阶段被风控后，每篇帖子仍级联 4 个接口（feed → html+cookie
→ html-cookie → SSR），零间隔每秒十几条 461，把账号彻底锁死。

现在的行为：
  - 461/471 抛专门的 CaptchaError（不重试，一次即失败）
  - **验证码（461/471）首次命中即熔断整批** —— 这是账号级风控，继续打只会
    越打越死；绝不累计到阈值、绝不级联兜底
  - 403/429/IP 限流（非验证码）连续 RISK_FAIL_LIMIT 次才熔断，避免偶发单条
    限流误杀整批
"""
import asyncio

import pytest
from tenacity import RetryError

import config
from media_platform.xhs.core import XiaoHongShuCrawler
from media_platform.xhs.exception import CaptchaError


class _FakeClient:
    def __init__(self, fail_times: int = 999):
        self.fail_times = fail_times
        self.calls = 0

    async def get_note_all_comments(self, note_id, xsec_token="", crawl_interval=0,
                                   callback=None, max_count=0, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise CaptchaError("CAPTCHA appeared, request failed, Verifytype: 217")
        return []


@pytest.mark.asyncio
async def test_circuit_breaker_stops_remaining_notes():
    """验证码（461）首次命中就熔断，剩余笔记不再发任何请求（不放大风控）。"""
    crawler = XiaoHongShuCrawler()
    crawler.xhs_client = _FakeClient(fail_times=999)     # 永远失败
    config.ENABLE_GET_COMMENTS = True

    await crawler.batch_get_note_comments(
        [f"note{i}" for i in range(20)], [""] * 20)

    # 验证码首次即熔断：只该发 1 个请求，而不是 20 篇各撞一遍，
    # 更不会累计到阈值才停（那样会放大风控）
    assert crawler.xhs_client.calls == 1
    assert crawler._risk_abort_reason
    assert crawler._comment_done_count == 0


class _FakeDetailClient:
    """详情接口直接抛验证码异常（模拟账号被打进 461 的状态）。"""

    def __init__(self):
        self.calls = 0

    async def get_note_by_id(self, note_id, xsec_source="", xsec_token=""):
        self.calls += 1
        raise CaptchaError("CAPTCHA appeared, request failed, Verifytype: 217")

    async def get_note_by_id_from_html(self, *args, **kwargs):
        return None


@pytest.mark.asyncio
async def test_captcha_in_detail_task_does_not_crash():
    """详情阶段撞验证码必须被吞掉并熔断，不能把 asyncio.gather 打崩。

    回归点：CaptchaError 改成「不重试」后，原先只 catch RetryError 的详情
    任务会让它直接往外抛，整个抓取进程挂掉（2026-10-28 实测）。
    """
    crawler = XiaoHongShuCrawler()
    crawler.xhs_client = _FakeDetailClient()

    # gather 不带 return_exceptions：任何漏网的异常都会在这里冒出来
    results = await asyncio.gather(*[
        crawler.get_note_detail_async_task(
            note_id=f"note{i}", xsec_source="pc_feed", xsec_token="",
            semaphore=asyncio.Semaphore(1))
        for i in range(5)
    ])

    assert results == [None] * 5
    assert crawler._risk_abort_reason, "连续失败后必须熔断"


@pytest.mark.asyncio
async def test_success_resets_fail_streak():
    """*非验证码*的偶发失败（登录失效/网络）连续次数未达上限时不熔断，
    中间成功一次就清零计数，不能误杀整批。"""
    crawler = XiaoHongShuCrawler()

    class _FakeRetryClient:
        def __init__(self, fail_times: int = 2):
            self.fail_times = fail_times
            self.calls = 0

        async def get_note_all_comments(self, note_id, xsec_token="", crawl_interval=0,
                                       callback=None, max_count=0, **kwargs):
            self.calls += 1
            if self.calls <= self.fail_times:
                raise RetryError("login expired / network jitter")
            return []

    client = _FakeRetryClient(fail_times=2)               # 前两次失败，之后成功
    crawler.xhs_client = client
    config.ENABLE_GET_COMMENTS = True

    await crawler.batch_get_note_comments(
        [f"note{i}" for i in range(6)], [""] * 6)

    assert not crawler._risk_abort_reason
    assert client.calls == 6
    assert crawler._comment_done_count == 4
