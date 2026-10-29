# -*- coding: utf-8 -*-
"""评论「断点续抓」回归测试（2026-10-29）。

背景：抓取大量帖子时按评论量由少到多排序，评论量大的帖子（动辄数万条）单次会话
可能持续数小时，一旦被中断（风控熔断 / 子进程被杀 / 手动停止），下一次执行会从头
重翻该帖全部评论 —— 既浪费额度又拉长会话、更易触发风控。

本测试覆盖续抓核心语义：
  1. 顶层评论翻页中断后，下一轮从「保存的游标之后」继续，不复抓已抓评论、不重复入库
  2. 楼中楼（子评论）续抓原语：中断返回「最后有效游标」，从游标续抓到收尾、无重复
  3. 续抓进度持久化助手（get/save/clear_comment_resume）读写 story_chapter.ext 正确
"""
import asyncio
from contextlib import asynccontextmanager

import pytest

import config
from media_platform.xhs.client import XiaoHongShuClient
from media_platform.xhs.exception import CaptchaError, DataFetchError
from tools.v2_sync import (
    save_comment_resume, get_comment_resume, clear_comment_resume,
)


def _client() -> XiaoHongShuClient:
    return XiaoHongShuClient(
        proxy=None, headers={"Cookie": "test"},
        playwright_page=None, cookie_dict={},
    )


# ---------------------------------------------------------------------------
# 1. 顶层评论：中断后续抓、无重复
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_top_level_resume_continues_without_duplicates():
    """模拟第 4 页抓取时接口报错（风控/超时），验证续抓从保存游标继续且无重复。"""
    client = _client()
    total_top = 100
    page_size = 10

    state = {"pages": 0, "abort_after": 3}   # 第 4 页（pages==3 后）中断
    inserted_top: list = []

    async def _fetch(note_id, xsec_token, cursor=""):
        start = int(cursor or 0)
        if state["pages"] >= state["abort_after"]:
            raise CaptchaError("simulated abort on page 4")
        state["pages"] += 1
        ids = list(range(start, min(start + page_size, total_top)))
        return {
            "comments": [
                {"id": f"c{i}", "content": "x", "sub_comment_count": 0,
                 "sub_comments": [], "user_info": {"user_id": f"u{i}"}}
                for i in ids
            ],
            "has_more": start + page_size < total_top,
            "cursor": str(start + page_size),
        }

    async def _sub(comments, xsec_token, crawl_interval=0, callback=None,
                   note_id=None, start_sub_cursors=None, **kwargs):
        # 本测试没有楼中楼
        return [], {}

    async def _record(note_id, comments):
        inserted_top.extend(c["id"] for c in comments)

    saved = {}

    async def _progress(note_id, st, finished):
        saved.update(st)

    client.get_note_comments = _fetch
    client.get_comments_all_sub_comments = _sub

    # ---- 第一轮：翻到第 3 页后中断（CaptchaError 向上抛）----
    with pytest.raises(CaptchaError):
        await client.get_note_all_comments(
            note_id="n1", xsec_token="t", crawl_interval=0, max_count=0,
            callback=_record, progress_callback=_progress,
        )
    # 中断前已落库 0..29（3 页），保存的续抓游标应是第 4 页起点 "30"
    assert inserted_top == [f"c{i}" for i in range(0, 30)]
    assert saved.get("top_cursor") == "30"
    assert saved.get("top_count") == 30

    # ---- 第二轮：从游标 "30" 续抓，不再中断 ----
    state["pages"] = 0
    state["abort_after"] = 999
    inserted_top.clear()
    saved.clear()
    await client.get_note_all_comments(
        note_id="n1", xsec_token="t", crawl_interval=0, max_count=0,
        callback=_record, progress_callback=_progress,
        start_cursor="30", top_count_in=30,
    )
    # 续抓只拿到 30..99，且不与第一轮重叠
    assert inserted_top == [f"c{i}" for i in range(30, 100)]
    assert saved.get("top_cursor") == "100"   # 翻到底后的游标
    assert saved.get("reached_end") is True


@pytest.mark.asyncio
async def test_resume_cumulative_no_recount():
    """断点续抓：每轮只翻 2 页就中断（模拟风控/被杀），靠持久化游标续抓，
    累计抓全 120 条主评论、不重复、不漏、不重头翻。"""
    client = _client()
    total_top = 120
    run_pages = {"n": 0}

    async def _fetch(note_id, xsec_token, cursor=""):
        start = int(cursor or 0)
        run_pages["n"] += 1
        if run_pages["n"] > 2:
            # 模拟本轮跑到第 3 页时接口报错 / 进程被杀
            raise DataFetchError("interrupt mid-run")
        return {
            "comments": [{"id": f"c{i}", "content": "x", "sub_comment_count": 0,
                          "sub_comments": [], "user_info": {"user_id": "u"}}
                         for i in range(start, min(start + 10, total_top))],
            "has_more": start + 10 < total_top, "cursor": str(start + 10),
        }

    async def _sub(comments, **kwargs):
        return [], {}

    client.get_note_comments = _fetch
    client.get_comments_all_sub_comments = _sub

    collected_ids: list = []
    saved: dict = {}

    async def _collect(note_id, comments):
        collected_ids.extend(c["id"] for c in comments)

    async def _progress(note_id, st, finished):
        saved.update(st)

    ctx = {"cursor": "", "top": 0}
    # 120 条 / 每轮 2 页×10 = 6 轮（最后一页 has_more=False 收尾）
    for _ in range(8):
        run_pages["n"] = 0
        saved.clear()
        try:
            await client.get_note_all_comments(
                note_id="n", xsec_token="t", crawl_interval=0, max_count=0,
                start_cursor=ctx["cursor"], top_count_in=ctx["top"],
                callback=_collect, progress_callback=_progress,
            )
        except DataFetchError:
            pass
        ctx["cursor"] = saved.get("top_cursor", "")
        ctx["top"] = saved.get("top_count", 0)
    assert len(collected_ids) == len(set(collected_ids)), "存在重复评论"
    assert set(collected_ids) == {f"c{i}" for i in range(120)}
    assert ctx["top"] == 120
    assert saved.get("reached_end") is True


# ---------------------------------------------------------------------------
# 2. 楼中楼续抓原语
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fetch_sub_pages_open_cursor_and_resume():
    """_fetch_sub_pages 翻页报错时返回「最后有效游标」；从该游标续抓到收尾、无重复。"""
    client = _client()
    calls = {"n": 0}
    collected = set()

    async def _sub(note_id, root_comment_id, xsec_token, num=10, cursor=""):
        calls["n"] += 1
        if calls["n"] == 2:
            # 模拟接口报错（DataFetchError）：翻页中断，但已抓部分要能续
            raise DataFetchError("transient fetch error")
        if cursor == "":
            return {"comments": [{"id": "s1"}, {"id": "s2"}],
                    "has_more": True, "cursor": "c1"}
        return {"comments": [{"id": "s3"}], "has_more": False, "cursor": "c2"}

    client.get_note_sub_comments = _sub

    async def cb(nid, c):
        collected.update(x["id"] for x in c)

    # 第一轮：从第 1 页 [s1,s2] 开始，第 2 页报错 → 返回最后有效游标 "c1"
    result, still = await client._fetch_sub_pages(
        note_id="n", root_comment_id="r1", xsec_token="t", crawl_interval=0,
        callback=cb, seen_sub_ids=set(), initial_has_more=True, initial_cursor="",
    )
    assert still == "c1"
    assert {x["id"] for x in result} == {"s1", "s2"}
    assert "s3" not in collected

    # 第二轮：从 "c1" 续抓 → 拿到 s3，翻到底（still=None）
    result2, still2 = await client._fetch_sub_pages(
        note_id="n", root_comment_id="r1", xsec_token="t", crawl_interval=0,
        callback=cb, seen_sub_ids=set(), initial_has_more=True, initial_cursor="c1",
    )
    assert still2 is None
    assert {x["id"] for x in result2} == {"s3"}
    assert collected == {"s1", "s2", "s3"}


@pytest.mark.asyncio
async def test_resume_finishes_open_sub_roots(monkeypatch):
    """续抓收尾：上一轮没翻完的根评论楼中楼，下一轮单独补齐、无重复。"""
    # 楼中楼抓取默认关闭，显式打开（与生产配置一致）
    monkeypatch.setattr(config, "ENABLE_GET_SUB_COMMENTS", True, raising=False)
    client = _client()
    # 主评论 r1 的楼中楼：内嵌 s1，翻页 s2（报错中断留待续抓），再翻页 s3 收尾
    calls = {"n": 0}
    collected = set()
    raise_on_second = {"flag": False}

    async def _sub(note_id, root_comment_id, xsec_token, num=10, cursor=""):
        calls["n"] += 1
        if raise_on_second["flag"] and calls["n"] == 2:
            raise DataFetchError("transient fetch error on page 2")
        if cursor == "":
            return {"comments": [{"id": "s2"}], "has_more": True, "cursor": "sc1"}
        return {"comments": [{"id": "s3"}], "has_more": False, "cursor": "sc2"}

    client.get_note_sub_comments = _sub

    async def cb(nid, c):
        collected.update(x["id"] for x in c)

    # 场景 1：上一轮 r1 楼中楼翻到 sc1 中断，本轮从 sc1 续翻并收尾
    raise_on_second["flag"] = True
    calls["n"] = 0
    result, open_sub = await client.get_comments_all_sub_comments(
        comments=[], xsec_token="t", crawl_interval=0, callback=cb,
        note_id="n", start_sub_cursors={"r1": "sc1"},
    )
    # r1 的楼中楼从 sc1 续翻，补齐 s3；收尾后 open_sub 不再含 r1
    assert {x["id"] for x in result} == {"s3"}
    assert "r1" not in open_sub
    assert collected == {"s3"}

    # 场景 2：完整抓（内嵌 s1 + 翻页 s2/s3），不应报错，全部收齐
    raise_on_second["flag"] = False
    calls["n"] = 0
    collected.clear()
    result2, open_sub2 = await client.get_comments_all_sub_comments(
        comments=[{"id": "r1", "sub_comment_count": 3,
                   "sub_comments": [{"id": "s1"}]}],
        xsec_token="t", crawl_interval=0, callback=cb, note_id="n",
    )
    assert {x["id"] for x in result2} == {"s1", "s2", "s3"}
    assert open_sub2 == {}


# ---------------------------------------------------------------------------
# 3. 持久化助手（不依赖真实数据库）
# ---------------------------------------------------------------------------
def test_save_get_clear_comment_resume(monkeypatch):
    chapter = type("C", (), {"ext": {}})()
    committed = {"n": 0}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *a, **k):
            class Res:
                def scalar_one_or_none(self):
                    return chapter
            return Res()

        async def commit(self):
            committed["n"] += 1

    async def fake_session():
        yield FakeSession()

    monkeypatch.setattr("tools.v2_sync._v2_session",
                        asynccontextmanager(fake_session))
    async def _fake_site_id(s, p):
        return 1

    monkeypatch.setattr("tools.v2_sync._get_site_id", _fake_site_id)

    # save
    asyncio.run(save_comment_resume(
        "xhs", "n1", "cur30", 30, {"r1": "sc1"}, reached_end=False))
    assert committed["n"] == 1
    resume = chapter.ext["comment_resume"]
    assert resume["top_cursor"] == "cur30"
    assert resume["top_count"] == 30
    assert resume["sub_cursors"] == {"r1": "sc1"}
    assert resume["reached_end"] is False

    # get
    got = asyncio.run(get_comment_resume("xhs", "n1"))
    assert got["top_cursor"] == "cur30"

    # empty / None cursor 不进 sub_cursors
    asyncio.run(save_comment_resume("xhs", "n2", "", 0, {"r2": ""}, reached_end=True))
    assert chapter.ext["comment_resume"]["sub_cursors"] == {}

    # clear
    asyncio.run(clear_comment_resume("xhs", "n1"))
    assert "comment_resume" not in chapter.ext
