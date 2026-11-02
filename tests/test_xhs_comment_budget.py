"""
单轮评论接口请求量硬上限回归：XHS_COMMENT_BUDGET_PER_ROUND。

背景（2026-10-31 实跑实测）：
小红书对「评论接口」的限流是**累计请求量**触发，不是纯速率。一轮里热门帖的
深层楼中楼把评论请求推到 ~90 次就 461，导致整轮报废。5-15s 页间隔压不住单轮总量。
所以加一个明显低于触发阈值的预算，在撞 461 之前主动停手，剩余帖子留到下一轮。

关键语义（必须锁住）：
1. 预算按「主评论页 + 楼中楼页」累计，达到上限立即停止继续打评论接口。
2. 上限触顶属于**主动停止**（不是失败）：楼中楼返回 last_cursor 供下轮续抓，
   不要当成异常中断。
3. 预算=0 时不限制（向后兼容，交给熔断 + 冷却兜底）。
4. 预算在「同一次运行」内跨批次累计（新帖批次 + 补抓批次共享账号/IP），
   保证整轮总量受控；跨运行（新子进程）自然归零。
"""

import asyncio
import pytest

import config
from media_platform.xhs.client import XiaoHongShuClient
from media_platform.xhs.core import XiaoHongShuCrawler

SUB = "/api/sns/web/v2/comment/sub/page"
TOP = "/api/sns/web/v2/comment/page"


def _client(budget=0):
    c = XiaoHongShuClient(headers={}, playwright_page=None, cookie_dict={})
    c._comment_req_budget = budget
    c._comment_req_used = 0
    c._comment_budget_hit = False
    return c


@pytest.mark.asyncio
async def test_sub_pages_stop_when_budget_exhausted():
    """楼中楼翻页达到单轮预算上限后停止，并保留游标供续抓。"""
    client = _client(budget=2)

    calls = []

    async def fake_sub(uri, params=None, **kwargs):
        calls.append(uri)
        n = len([u for u in calls if u == SUB])
        return {"comments": [{"id": f"s{n}"}], "has_more": True, "cursor": f"cur{n}"}

    client.get = fake_sub
    result, still, skipped = await client._fetch_sub_pages(
        note_id="n", root_comment_id="r1", xsec_token="t", crawl_interval=0,
        callback=None, seen_sub_ids=set(), sub_count=99,
        initial_has_more=True, initial_cursor="",
    )
    assert len(calls) == 2, f"预算 2 应只打 2 次楼中楼，实际 {len(calls)}"
    # 触顶不是失败：必须保留游标供下轮续抓（不能 None）
    assert still == "cur2", f"主动停止应保留 last_cursor，实际 {still}"
    assert client._comment_budget_hit is True


@pytest.mark.asyncio
async def test_main_pages_stop_when_budget_exhausted():
    """主评论翻页达到单轮预算上限后，阶段1 提前结束。"""
    client = _client(budget=3)

    top_calls = []

    async def fake_top(uri, params=None, **kwargs):
        top_calls.append(uri)
        n = len(top_calls)
        return {
            "comments": [{"id": f"c{n}"}],
            "has_more": True,  # 永远有更多，逼出预算上限
            "cursor": f"tc{n}",
        }

    async def fake_sub(uri, params=None, **kwargs):
        return {"comments": [], "has_more": False, "cursor": ""}

    client.get = lambda uri, *a, **kw: (
        fake_top(uri, *a, **kw) if uri == TOP else fake_sub(uri, *a, **kw))
    # 让主评论不触发楼中楼阶段
    client._sub_needs_paging = lambda c: False

    result = await client.get_note_all_comments(
        note_id="n", xsec_token="t", crawl_interval=0, callback=None, max_count=0,
    )
    assert len(top_calls) == 3, f"预算 3 应只翻 3 页主评论，实际 {len(top_calls)}"
    assert client._comment_budget_hit is True


@pytest.mark.asyncio
async def test_budget_zero_means_unlimited(monkeypatch):
    """预算=0 时不限制（向后兼容）。同时关掉默认深度上限以隔离变量。"""
    monkeypatch.setattr(config, "XHS_SUB_COMMENT_MAX_PAGES", 0)
    client = _client(budget=0)

    calls = []

    async def fake_sub(uri, params=None, **kwargs):
        calls.append(uri)
        n = len([u for u in calls if u == SUB])
        return {"comments": [{"id": f"s{n}"}], "has_more": n < 5, "cursor": f"cur{n}"}

    client.get = fake_sub
    result, still, skipped = await client._fetch_sub_pages(
        note_id="n", root_comment_id="r1", xsec_token="t", crawl_interval=0,
        callback=None, seen_sub_ids=set(), sub_count=99,
        initial_has_more=True, initial_cursor="",
    )
    assert len(calls) == 5, f"预算=0 应翻到 has_more=False(5 页)，实际 {len(calls)}"
    assert client._comment_budget_hit is False


def _detail_client(detail_budget=0):
    c = XiaoHongShuClient(headers={}, playwright_page=None, cookie_dict={})
    c._detail_req_budget = detail_budget
    c._detail_req_used = 0
    c._detail_budget_hit = False
    return c


def test_detail_budget_counter():
    """笔记详情预算按篇累计，达到上限即触顶（独立计数，与评论预算互不影响）。"""
    c = _detail_client(detail_budget=2)
    c._detail_req_tick()
    c._detail_req_tick()
    assert c._detail_budget_exhausted() is True
    # 评论预算应独立不受影响
    assert getattr(c, "_comment_budget_hit", False) is False


@pytest.mark.asyncio
async def test_detail_skips_when_budget_exhausted():
    """详情预算耗尽后，剩余帖子直接跳过（不发起详情请求），留到下一轮。"""

    class _Stub:
        _risk_abort_reason = ""
        def __init__(self, hit):
            self.xhs_client = _detail_client(detail_budget=1)
            self.xhs_client._detail_budget_hit = hit
        # 绑定真实方法：跳过分支只读 _risk_abort_reason 与 xhs_client，不触及其他依赖
        get_note_detail_async_task = XiaoHongShuCrawler.get_note_detail_async_task

    crawler = _Stub(hit=True)
    res = await crawler.get_note_detail_async_task(
        note_id="n1", xsec_source="pc_feed", xsec_token="t",
        semaphore=asyncio.Semaphore(1))
    assert res is None, "详情预算耗尽应直接跳过返回 None"


@pytest.mark.asyncio
async def test_detail_zero_means_unlimited():
    """详情预算=0 时不限制（向后兼容）。"""
    c = _detail_client(detail_budget=0)
    for _ in range(5):
        c._detail_req_tick()
    assert c._detail_budget_exhausted() is False
    assert c._detail_budget_hit is False

