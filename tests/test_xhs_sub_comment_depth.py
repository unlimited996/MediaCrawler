"""
楼中楼（子评论）翻页深度上限回归：XHS_SUB_COMMENT_MAX_PAGES。

背景（2026-10-31 实跑实测）：
sub/page **服务端固定只返回 5 条/页**（请求 num=30 也只给 5，和主评论固定
10 条/页同理）。于是声明 39 条回复的 root 要翻 8 页、51 条要翻 10 页，
按 5-15s 页间隔算单个 root 就要 1.5-2 分钟 —— 这是请求量爆炸的真凶，
也是撞 461 的根因之一。所以加深度上限主动放弃深翻。

关键语义（容易写错，必须锁住）：
1. 触顶截断是**主动放弃**而非失败 → 不留游标（返回 None）。
   若沿用旧逻辑留游标，下一轮只会再翻同样的 N 页、永远翻不到底，
   帖子永远凑不满完整率 → 每轮重抓 → 反而打了更多请求。
2. 上报「声明数 - 已抓数」作为放弃条数，由完整率判定从分母扣掉。
3. 接口报错中断属于**失败**，与策略截断不同 → 必须保留游标供续抓。
"""

import pytest

import config
from media_platform.xhs.client import XiaoHongShuClient

SUB = "/api/sns/web/v2/comment/sub/page"


def _client():
    return XiaoHongShuClient(headers={}, playwright_page=None, cookie_dict={})


def _sub_always_more(client, calls, per_page=1, limit=100):
    """楼中楼接口永远 has_more=True，逼出深度上限。"""
    async def fake_get(uri, params=None, **kwargs):
        calls.append(uri)
        n = len([u for u in calls if u == SUB])
        return {
            "comments": [{"id": f"s{n}-{i}"} for i in range(per_page)],
            "has_more": n < limit,
            "cursor": f"cur{n}",
        }
    client.get = fake_get
    return client


@pytest.mark.asyncio
async def test_depth_cap_stops_paging(monkeypatch):
    """达到 XHS_SUB_COMMENT_MAX_PAGES 后停止翻页，不再继续打请求。"""
    monkeypatch.setattr(config, "XHS_SUB_COMMENT_MAX_PAGES", 3)
    client = _client()
    calls = []
    _sub_always_more(client, calls)

    result, still, skipped = await client._fetch_sub_pages(
        note_id="n", root_comment_id="r1", xsec_token="t", crawl_interval=0,
        callback=None, seen_sub_ids=set(), sub_count=51,
        initial_has_more=True, initial_cursor="",
    )
    assert len(calls) == 3, f"上限 3 页应只打 3 次，实际 {len(calls)}"
    # 每页 1 条 → 抓到 3 条，声明 51 → 放弃 48
    assert len(result) == 3
    assert skipped == 48, f"放弃条数应为 51-3=48，实际 {skipped}"


@pytest.mark.asyncio
async def test_depth_cap_returns_no_cursor(monkeypatch):
    """触顶时不留游标：留了下轮只会重复同样几页，永远翻不到底。"""
    monkeypatch.setattr(config, "XHS_SUB_COMMENT_MAX_PAGES", 2)
    client = _client()
    calls = []
    _sub_always_more(client, calls)

    _result, still, skipped = await client._fetch_sub_pages(
        note_id="n", root_comment_id="r1", xsec_token="t", crawl_interval=0,
        callback=None, seen_sub_ids=set(), sub_count=40,
        initial_has_more=True, initial_cursor="",
    )
    assert still is None, "策略截断必须返回 None，不能留游标供续抓"
    assert skipped == 38


@pytest.mark.asyncio
async def test_error_keeps_cursor_unlike_cap(monkeypatch):
    """接口报错属「中断」，与策略截断相反：必须保留游标供续抓。"""
    from media_platform.xhs.exception import DataFetchError

    monkeypatch.setattr(config, "XHS_SUB_COMMENT_MAX_PAGES", 5)
    client = _client()
    calls = []

    async def fake_get(uri, params=None, **kwargs):
        calls.append(uri)
        if len([u for u in calls if u == SUB]) == 1:
            return {"comments": [{"id": "s1"}], "has_more": True, "cursor": "c1"}
        raise DataFetchError("transient")

    client.get = fake_get
    result, still, skipped = await client._fetch_sub_pages(
        note_id="n", root_comment_id="r1", xsec_token="t", crawl_interval=0,
        callback=None, seen_sub_ids=set(), sub_count=40,
        initial_has_more=True, initial_cursor="",
    )
    assert still == "c1", "报错中断要保留游标"
    assert skipped == 0, "报错不是策略性放弃，不应计入 skipped"


@pytest.mark.asyncio
async def test_zero_means_unlimited(monkeypatch):
    """0 = 不限制，回退到旧行为（一直翻到 has_more=False）。"""
    monkeypatch.setattr(config, "XHS_SUB_COMMENT_MAX_PAGES", 0)
    client = _client()
    calls = []
    _sub_always_more(client, calls, limit=7)  # 7 页后 has_more=False

    result, still, skipped = await client._fetch_sub_pages(
        note_id="n", root_comment_id="r1", xsec_token="t", crawl_interval=0,
        callback=None, seen_sub_ids=set(), sub_count=40,
        initial_has_more=True, initial_cursor="",
    )
    assert len(calls) == 7, f"不限制时应翻到 has_more=False，实际 {len(calls)}"
    assert still is None and skipped == 0


@pytest.mark.asyncio
async def test_no_cap_when_fully_fetched(monkeypatch):
    """本来就翻得完的楼中楼：不触发上限，skipped 必须为 0。"""
    monkeypatch.setattr(config, "XHS_SUB_COMMENT_MAX_PAGES", 3)
    client = _client()
    calls = []
    _sub_always_more(client, calls, limit=2)  # 2 页就翻完

    result, still, skipped = await client._fetch_sub_pages(
        note_id="n", root_comment_id="r1", xsec_token="t", crawl_interval=0,
        callback=None, seen_sub_ids=set(), sub_count=2,
        initial_has_more=True, initial_cursor="",
    )
    assert still is None and skipped == 0, "没触顶就不该有放弃条数"
