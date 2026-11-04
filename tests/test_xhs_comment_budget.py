"""评论抓取「无数量上限」回归（2026-11-03 移除单轮请求硬上限后）。

背景与变更：
- 2026-10-31 曾引入 XHS_COMMENT_BUDGET_PER_ROUND 硬上限，意图在撞 461 前主动停手。
- 但 2026-11-03 用户明确：正常爬取就该「慢慢抓、抓全」，不要用数量上限截断；
  真正的浪费是「已完成帖被反复重抓」（见 run#127：100 篇仅净新增 1 条）。
- 因此移除详情/评论单轮请求数量上限，风控改由「慢速随机间隔 5-15s + 撞 461 退避
  并断点续抓」承担。

本文件锁住两点：
1. 没有数量上限后，评论翻页必须一直翻到 has_more=False（把评论抓全），不得被
   任何残留预算逻辑提前截断。
2. build_crawler_env 仍在每次任务启动时 refresh() 一次（保证 next_task 类配置的
   页面改动本轮生效 —— 这是 2026-11-02 的修复，不能因移除预算回归）。
"""

import asyncio
import pytest

import config
from media_platform.xhs.client import XiaoHongShuClient

SUB = "/api/sns/web/v2/comment/sub/page"
TOP = "/api/sns/web/v2/comment/page"


def _client():
    return XiaoHongShuClient(headers={}, playwright_page=None, cookie_dict={})


def test_save_comment_resume_signature_contract():
    """契约锁：save_comment_resume 不接受 skipped_sub_count；

    core._on_progress 曾误把 skipped_sub_count 传给 save_comment_resume
    （它只属于 save_comment_skipped_sub），导致每页评论都「续抓进度持久化失败」、
    进度从不落库、下轮从第一页重抓、白打请求加速撞 461。改回正确形参后锁住接口，
    避免后人再误传。
    """
    import inspect
    from tools.v2_sync import save_comment_resume, save_comment_skipped_sub

    resume_params = set(inspect.signature(save_comment_resume).parameters)
    assert "skipped_sub_count" not in resume_params, (
        "save_comment_resume 不应接受 skipped_sub_count，"
        "该参数只属于 save_comment_skipped_sub")
    # save_comment_skipped_sub 必须保留「放弃条数」形参（实际名 skipped），否则口径漂移
    skip_params = set(inspect.signature(save_comment_skipped_sub).parameters)
    assert "skipped" in skip_params


def test_backfill_per_post_constant_not_accessed_via_config_package():
    """回归（2026-11-03）：下划线常量不能通过 `config.<name>` 访问。

    `config/base_config.py` 用 `from .xhs_config import *` 导出，星号导入**跳过
    下划线开头**的名字。代码曾写 `config._XHS_COMMENT_BACKFILL_PER_POST` →
    AttributeError → `get_archived_content_ids` 与 `get_backfill_candidates` 双双
    崩溃 → 退回「全量模式」每次都从头翻整个点赞列表（现象：重跑还在翻页、不补
    未完成帖）。必须直接从 `config.xhs_config` 导入。
    """
    import config
    from config.xhs_config import _XHS_COMMENT_BACKFILL_PER_POST as const

    assert isinstance(const, int) and const > 0
    # config 包本身不应有该私有名（这正是不能走 config._xxx 的原因）
    assert not hasattr(config, "_XHS_COMMENT_BACKFILL_PER_POST")
    # core 必须已在模块级导入该常量，而不是运行时去 config 上找
    import media_platform.xhs.core as core
    assert getattr(core, "_XHS_COMMENT_BACKFILL_PER_POST", None) == const


def test_budget_attrs_removed():
    """单轮请求预算的计数器/方法已彻底移除（防止死灰复燃）。"""
    c = _client()
    for attr in ("_comment_req_budget", "_comment_req_used", "_comment_budget_hit",
                 "_detail_req_budget", "_detail_req_used", "_detail_budget_hit"):
        assert not hasattr(c, attr), f"预算属性 {attr} 应已移除"
    for meth in ("_comment_req_tick", "_comment_budget_exhausted",
                 "_detail_req_tick", "_detail_budget_exhausted"):
        assert not hasattr(c, meth), f"预算方法 {meth} 应已移除"


@pytest.mark.asyncio
async def test_sub_pages_reads_to_end_no_cap(monkeypatch):
    """楼中楼无数量上限：一直翻到 has_more=False（抓全），不被截断。"""
    monkeypatch.setattr(config, "XHS_SUB_COMMENT_MAX_PAGES", 0)  # 关掉深度上限隔离变量
    client = _client()

    calls = []

    async def fake_sub(uri, params=None, **kwargs):
        calls.append(uri)
        n = len([u for u in calls if u == SUB])
        return {"comments": [{"id": f"s{n}"}], "has_more": n < 6, "cursor": f"cur{n}"}

    client.get = fake_sub
    result, still, skipped = await client._fetch_sub_pages(
        note_id="n", root_comment_id="r1", xsec_token="t", crawl_interval=0,
        callback=None, seen_sub_ids=set(), sub_count=99,
        initial_has_more=True, initial_cursor="",
    )
    assert len(calls) == 6, f"应翻到 has_more=False(6 页)，实际 {len(calls)} 次"
    assert still is None, "翻到底后不应保留游标"


@pytest.mark.asyncio
async def test_main_pages_reads_to_end_no_cap():
    """主评论无数量上限：一直翻到 has_more=False，把评论抓全。"""
    client = _client()

    top_calls = []

    async def fake_top(uri, params=None, **kwargs):
        top_calls.append(uri)
        n = len(top_calls)
        return {
            "comments": [{"id": f"c{n}"}],
            "has_more": n < 4,   # 第 4 页开始 has_more=False
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
    assert len(top_calls) == 4, f"应翻 4 页到 has_more=False，实际 {len(top_calls)}"
    assert len(result) == 4, f"应收集到 4 条评论，实际 {len(result)}"


@pytest.mark.asyncio
async def test_build_crawler_env_refreshes_next_task_config(monkeypatch):
    """回归（2026-11-02 修复保留）：任务启动必须先 refresh() 一次读最新库值。

    移除预算后该 refresh 仍必须存在 —— next_task 类配置（抓取上限、增量等）
    否则编辑后不生效。这里只锁「任务启动会刷新」，不依赖已移除的预算注入。
    """
    from api.services import profile_crawler

    class _FakeRC:
        def __init__(self):
            self.refresh_calls = 0

        async def refresh(self):
            self.refresh_calls += 1

        def get_int(self, key, default=0):
            return int(default)

        def get_float(self, key, default=0.0):
            return float(default)

        def get_bool(self, key, default=False):
            return bool(default)

    fake = _FakeRC()
    monkeypatch.setattr("config.runtime_config.runtime_config", fake)

    env = await profile_crawler.build_crawler_env({"platform": "xhs"})

    assert fake.refresh_calls == 1, "任务启动必须重新拉取库表配置"
    assert env["MAX_CONCURRENCY_NUM"] == "1"
    # 预算 env 已不再注入
    assert "XHS_COMMENT_BUDGET_PER_ROUND" not in env
    assert "XHS_DETAIL_BUDGET_PER_ROUND" not in env
    # 抓取节奏 / 验证码等待 env 必须注入（慢速抓全、避免撞 461）
    assert "XHS_SLEEP_MIN_SEC" in env
    assert "XHS_SLEEP_MAX_SEC" in env
    assert "XHS_CAPTCHA_WAIT_SEC" in env
