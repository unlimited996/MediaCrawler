# -*- coding: utf-8 -*-
"""点赞 / 收藏列表分页与额度回归测试。

背景（2026-10-28 修的两个 bug）：
  1) `get_all_behavior_notes` 曾用全局 `len(result) < CRAWLER_MAX_NOTES_COUNT`
     （默认 15）当翻页条件，点赞到 20 条（2 页）就被截断；
  2) 点赞一旦超过 15 条，收藏的 while 条件直接为假 —— 收藏一条都不抓。

这里不连网、不连库：直接替换 fetcher 造分页数据，验证
  - 列表能翻到 has_more=False（不再被 15 截断）
  - 两个行为额度互相独立
  - 回调 return False 能叫停翻页
  - 已归档的老帖被跳过时，翻页仍会继续往后走（增量模式的关键）
"""
import asyncio

import pytest

from media_platform.xhs.client import XiaoHongShuClient

PAGE = 10


def _client() -> XiaoHongShuClient:
    return XiaoHongShuClient(
        proxy=None,
        headers={"Cookie": "test"},
        playwright_page=None,
        cookie_dict={},
    )


def _fake_fetcher(total: int, prefix: str):
    async def _fetch(user_id, cursor="", page_size=30):
        start = int(cursor or 0)
        ids = list(range(start, min(start + PAGE, total)))
        return {
            "notes": [{"note_id": f"{prefix}{i}", "type": "normal"} for i in ids],
            "has_more": start + PAGE < total,
            "cursor": str(start + PAGE),
        }

    return _fetch


@pytest.mark.asyncio
async def test_pages_to_end_without_note_count_cap():
    """点赞 495 条 / 收藏 37 条：两个行为都要翻到底（旧实现是 20 / 0）。"""
    client = _client()
    client.get_user_liked_notes = _fake_fetcher(495, "L")
    client.get_user_collected_notes = _fake_fetcher(37, "C")
    got = {"likes": 0, "collects": 0}

    async def cb(batch, label="likes"):
        got[label] += len(batch)
        return True

    notes = await client.get_all_behavior_notes(
        "uid", behaviors=["likes", "collects"], crawl_interval=0, callback=cb)

    assert got["likes"] == 495
    assert got["collects"] == 37
    assert len(notes) == 495 + 37


@pytest.mark.asyncio
async def test_budget_is_per_behavior():
    """额度按行为独立：点赞用满不影响收藏继续抓。"""
    client = _client()
    client.get_user_liked_notes = _fake_fetcher(495, "L")
    client.get_user_collected_notes = _fake_fetcher(37, "C")
    budget = 25
    queued = {"likes": 0, "collects": 0}

    async def cb(batch, label="likes"):
        for _ in batch:
            if queued[label] >= budget:
                return False
            queued[label] += 1
        return queued[label] < budget

    await client.get_all_behavior_notes(
        "uid", behaviors=["likes", "collects"], crawl_interval=0, callback=cb)

    assert queued == {"likes": budget, "collects": budget}


@pytest.mark.asyncio
async def test_skip_archived_still_advances_pages():
    """前 200 条已归档 + 额度 30：要能跳过老帖继续翻到第 21 页之后。"""
    client = _client()
    client.get_user_liked_notes = _fake_fetcher(495, "L")
    known = {f"L{i}" for i in range(200)}
    budget = 30
    queued = []

    async def cb(batch, label="likes"):
        for note in batch:
            if len(queued) >= budget:
                return False
            if note["note_id"] in known:
                continue
            queued.append(note["note_id"])
        return len(queued) < budget

    await client.get_all_behavior_notes(
        "uid", behaviors=["likes"], crawl_interval=0, callback=cb)

    assert len(queued) == budget
    assert queued[0] == "L200", "应跳过前 200 条已归档的帖子"


@pytest.mark.asyncio
async def test_stops_when_cursor_does_not_advance():
    """游标不前进时必须停止翻页，避免空转。"""
    client = _client()
    calls = {"n": 0}

    async def _stuck(user_id, cursor="", page_size=30):
        calls["n"] += 1
        return {
            "notes": [{"note_id": f"X{calls['n']}", "type": "normal"}],
            "has_more": True,
            "cursor": "same-cursor",
        }

    client.get_user_liked_notes = _stuck
    notes = await client.get_all_behavior_notes(
        "uid", behaviors=["likes"], crawl_interval=0)

    assert calls["n"] == 2  # 第二页发现游标没动就停
    assert len(notes) == 2


@pytest.mark.asyncio
async def test_empty_first_page_respected():
    """列表为空（无点赞/无权限）时不应抛错，直接返回空。"""
    client = _client()

    async def _empty(user_id, cursor="", page_size=30):
        return {"notes": [], "has_more": False, "cursor": ""}

    client.get_user_liked_notes = _empty
    notes = await client.get_all_behavior_notes(
        "uid", behaviors=["likes"], crawl_interval=0)
    assert notes == []


@pytest.mark.asyncio
async def test_dict_callback_contract_queued_is_zero_does_not_stop():
    """新契约：回调返回 {"stop","queued"}，queued=0（整页已归档）时
    不应停止翻页，仍能翻过老帖继续往后走。"""
    client = _client()
    client.get_user_liked_notes = _fake_fetcher(495, "L")
    known = {f"L{i}" for i in range(460)}  # 前 460 条已归档
    budget = 30
    queued = []

    async def cb(batch, label="likes"):
        appended = 0
        for note in batch:
            if len(queued) >= budget:
                return {"stop": True, "queued": appended}
            if note["note_id"] in known:
                continue
            queued.append(note["note_id"])
            appended += 1
        return {"stop": len(queued) >= budget, "queued": appended}

    await client.get_all_behavior_notes(
        "uid", behaviors=["likes"], crawl_interval=0, callback=cb)

    assert len(queued) == budget
    assert queued[0] == "L460", "应跳过前 460 条已归档的帖子继续翻页"


# ---------------------------------------------------------------------------
# 翻页随机休眠的取舍（2026-10-29）
#   背景：account 备份任务翻 50 页点赞，绝大部分页是视频帖/已归档帖，
#   每页都睡 5-15s 纯属白等；而末页回来时 has_more 已经是 False，
#   再睡一轮（实测 12.81s）后才收工，完全是浪费。
# ---------------------------------------------------------------------------

def _patch_sleep(monkeypatch):
    """拦截真实休眠，记录被调用的次数与原因。"""
    import media_platform.xhs.client as client_mod

    calls = []

    async def _no_sleep(spec, reason=""):
        calls.append(reason)
        return 0.0

    monkeypatch.setattr(client_mod.anti_risk, "interval_sleep", _no_sleep)
    return calls


@pytest.mark.asyncio
async def test_no_sleep_on_last_page(monkeypatch):
    """末页（has_more=False）不再做翻页休眠：3 页只该睡 2 次。"""
    sleeps = _patch_sleep(monkeypatch)
    client = _client()
    client.get_user_liked_notes = _fake_fetcher(30, "L")  # 3 页

    async def cb(batch, label="likes"):
        return {"stop": False, "queued": len(batch)}

    await client.get_all_behavior_notes(
        "uid", behaviors=["likes"], crawl_interval=0, callback=cb)

    assert len(sleeps) == 2, f"3 页最多睡 2 次（末页不睡），实际 {sleeps}"


@pytest.mark.asyncio
async def test_no_sleep_when_page_has_no_targets(monkeypatch):
    """整页都是视频帖/已归档帖（queued=0）时，翻页不应休眠。"""
    sleeps = _patch_sleep(monkeypatch)
    client = _client()
    client.get_user_liked_notes = _fake_fetcher(30, "L")

    async def cb(batch, label="likes"):
        return {"stop": False, "queued": 0}

    await client.get_all_behavior_notes(
        "uid", behaviors=["likes"], crawl_interval=0, callback=cb)

    assert sleeps == [], f"没有抓取目标就不该休眠，实际 {sleeps}"


@pytest.mark.asyncio
async def test_empty_fresh_page_does_not_inherit_previous_queued(monkeypatch):
    """中间某页去重后为空时，不能沿用上一页的 queued 值去休眠。"""
    sleeps = _patch_sleep(monkeypatch)
    client = _client()
    page = {"n": 0}

    async def _fetch(user_id, cursor="", page_size=30):
        page["n"] += 1
        n = page["n"]
        if n == 1:
            ids, has_more = range(0, PAGE), True        # 新帖 L0..L9
        elif n == 2:
            ids, has_more = range(0, PAGE), True        # 全与第一页重复
        else:
            ids, has_more = range(PAGE, PAGE * 2), False
        return {
            "notes": [{"note_id": f"L{i}", "type": "normal"} for i in ids],
            "has_more": has_more,
            "cursor": str(n),
        }

    client.get_user_liked_notes = _fetch

    async def cb(batch, label="likes"):
        return {"stop": False, "queued": len(batch)}

    await client.get_all_behavior_notes(
        "uid", behaviors=["likes"], crawl_interval=0, callback=cb)

    # 第 1 页 queued=10 睡一次；第 2 页 fresh=0（不该沿用 10 去睡）；
    # 第 3 页是末页（不睡）
    assert len(sleeps) == 1, f"只应在第 1 页睡 1 次，实际 {sleeps}"

