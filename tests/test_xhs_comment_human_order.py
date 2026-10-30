"""
评论抓取顺序回归：必须是「先翻完所有主评论，再逐条展开楼中楼」的两阶段形态。

背景（参考 xhs-note-archive skill 的风控结论）：
小红书判定机器人的**不是签名，而是请求的节奏与形态**。旧实现每翻一页主评论
就立刻翻这一页的楼中楼，两个 endpoint 交替（page → sub → page → sub…），
服务端看到的就是「顺着 cursor 链一路走到底 + 两个 endpoint 来回切」，
是典型爬虫画像；真人看帖是先把评论列表滚到底（page, page, …），
再逐条点开「展开 N 条回复」（sub, sub, …）。

本测试把 endpoint 调用序列录下来，断言两类请求**完全分离**：
第一个 sub/page 之前必须已经打完**所有** comment/page。
同时验证「没有回复的主楼不会被白问一遍」（省请求量 = 降风控）。
"""

import pytest

import config
from media_platform.xhs.client import XiaoHongShuClient

MAIN = "/api/sns/web/v2/comment/page"
SUB = "/api/sns/web/v2/comment/sub/page"


@pytest.fixture(autouse=True)
def _enable_sub_comments(monkeypatch):
    """楼中楼抓取默认关闭，不开的话整个阶段二会被跳过，测不到顺序。"""
    monkeypatch.setattr(config, "ENABLE_GET_SUB_COMMENTS", True)


def _make_client(calls, main_pages=2, per_page=2):
    """构造一个把 get() 换掉、只记录 uri 序列的 client。

    每页主评论里：第 0 条声明 3 条回复且 has_more=True（要翻楼中楼），
    其余条 sub_comment_count=0 且无 has_more（不该产生任何 sub 请求）。
    """
    client = XiaoHongShuClient(
        headers={}, playwright_page=None, cookie_dict={})

    async def fake_get(uri, params=None, **kwargs):
        calls.append(uri)
        if uri == MAIN:
            page = sum(1 for u in calls if u == MAIN)
            return {
                "comments": [
                    {
                        "id": f"c{page}-{i}",
                        "note_id": "note1",
                        "sub_comment_count": 3 if i == 0 else 0,
                        "sub_comment_has_more": (i == 0),
                        "sub_comments": [],
                    }
                    for i in range(per_page)
                ],
                "has_more": page < main_pages,
                "cursor": f"cur{page}",
            }
        # 楼中楼：一页给完，has_more=False
        return {
            "comments": [{"id": f"s-{len(calls)}", "note_id": "note1"}],
            "has_more": False,
            "cursor": "",
        }

    client.get = fake_get
    return client


@pytest.mark.asyncio
async def test_main_comments_all_fetched_before_any_sub_comment():
    """两阶段：所有 comment/page 必须排在第一个 sub/page 之前。"""
    calls = []
    client = _make_client(calls)

    result = await client.get_note_all_comments(
        note_id="note1", xsec_token="tok", crawl_interval=0, max_count=0)

    assert calls.count(MAIN) == 2, f"主评论应翻 2 页，实际 {calls}"
    assert calls.count(SUB) >= 1, "有回复的主楼必须展开楼中楼"

    first_sub = calls.index(SUB)
    mains_before = calls[:first_sub].count(MAIN)
    assert mains_before == calls.count(MAIN), (
        f"主评论请求必须与楼中楼完全分离（先全部 page 再全部 sub），"
        f"实际序列：{calls}")


@pytest.mark.asyncio
async def test_no_request_for_roots_without_replies():
    """没有回复的主楼不该被白问一遍（省下的请求量是降风控的关键）。"""
    calls = []
    client = _make_client(calls)

    await client.get_note_all_comments(
        note_id="note1", xsec_token="tok", crawl_interval=0, max_count=0)

    # 2 页 × 2 条 = 4 条主评论，其中只有每页第 0 条有回复 → 恰好 2 次 sub 请求
    assert calls.count(SUB) == 2, (
        f"只有 2 个主楼声明了回复，不该对其余主楼发 sub/page，实际 {calls}")


@pytest.mark.asyncio
async def test_inline_sub_comments_are_kept_without_extra_request():
    """主评论响应里内嵌的回复预览要直接入库，不额外发请求。"""
    calls = []
    client = XiaoHongShuClient(headers={}, playwright_page=None, cookie_dict={})

    async def fake_get(uri, params=None, **kwargs):
        calls.append(uri)
        if uri == MAIN:
            page = sum(1 for u in calls if u == MAIN)
            return {
                "comments": [{
                    "id": f"c{page}",
                    "note_id": "note1",
                    "sub_comment_count": 2,
                    "sub_comment_has_more": False,
                    # 声明 2 条且内嵌已给全 2 条 → 不该再翻页
                    "sub_comments": [
                        {"id": f"inline{page}-0", "note_id": "note1"},
                        {"id": f"inline{page}-1", "note_id": "note1"},
                    ],
                }],
                "has_more": page < 2,
                "cursor": f"cur{page}",
            }
        return {"comments": [], "has_more": False, "cursor": ""}

    client.get = fake_get
    got = []

    async def cb(note_id, comments):
        got.extend(comments)

    await client.get_note_all_comments(
        note_id="note1", xsec_token="tok", crawl_interval=0,
        max_count=0, callback=cb)

    inline = [c for c in got if str(c.get("id", "")).startswith("inline")]
    assert len(inline) == 4, f"4 条内嵌回复都应入库，实际 {len(inline)}"
    assert calls.count(SUB) == 0, (
        f"内嵌预览已给全声明数，不该再发 sub/page，实际 {calls}")
