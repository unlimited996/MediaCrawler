# -*- coding: utf-8 -*-
"""评论「抓够了吗」判定回归测试。

背景（2026-10-28 用户明确要求）：
  之前用「零评论帖最多重试 3 次」当兜底 —— 拍脑袋的次数上限，跟帖子到底
  有没有抓全毫无关系：撞风控的帖子 3 轮后同样被当成已归档，评论永远补不上；
  真·零评论的帖子则白跑 3 轮。
  现在改成数据驱动：**已抓评论数 / 原帖显示评论量 >= 90%** 才算抓全，
  未达标的帖子下一轮增量继续补，没有次数上限。

覆盖：
  1. 完整率计算与阈值边界
  2. 增量跳过判定（>=90% 跳过，<90% 重抓）
  3. 单篇抓取上限按「原帖显示评论量」推算（不再固定 30 条）
  4. get_note_all_comments 的 max_count 只约束一级评论，子评论不占额度
"""
import asyncio

import pytest

import config
from media_platform.xhs.client import XiaoHongShuClient
from media_platform.xhs.core import _comment_max_count
from tools.v2_sync import (
    comment_completeness, is_comment_complete, plan_backfill,
)


# ---------------------------------------------------------------------------
# 1. 完整率
# ---------------------------------------------------------------------------
def test_comment_completeness_ratio():
    assert comment_completeness(0, 100) == 0.0
    assert comment_completeness(90, 100) == pytest.approx(0.9)
    assert comment_completeness(100, 100) == 1.0
    # 抓到的比声明的还多（声明数滞后 / 含子评论）：封顶 1.0，不能出现 1.7
    assert comment_completeness(170, 100) == 1.0


def test_no_declared_means_complete():
    """平台声明数是 0/缺失：没有评论需求，天然算抓全，不该被反复重抓。"""
    assert comment_completeness(0, 0) == 1.0
    assert is_comment_complete(0, 0) is True


def test_threshold_is_90_percent():
    """90% 是达标线；差一条都不算（除非已经是可忽略的取整误差）。"""
    assert is_comment_complete(90, 100) is True
    assert is_comment_complete(89, 100) is False
    # 只有声明数很小的时候，1 条误差才会跨线
    assert is_comment_complete(9, 10) is True
    assert is_comment_complete(1, 2) is False


def test_risk_control_zero_comments_must_retry():
    """上一轮撞验证码只存了正文（0 评论）：必须继续重抓，不是「试 3 次就放过」。"""
    declared = 42
    for _ in range(10):          # 哪怕重试 10 轮仍然 0 条，也依然算未完成
        assert is_comment_complete(0, declared) is False
    # 抓到 38/42 = 90.4% 才算完成
    assert is_comment_complete(38, declared) is True
    assert is_comment_complete(37, declared) is False


# ---------------------------------------------------------------------------
# 2. 单篇抓取上限
# ---------------------------------------------------------------------------
def test_max_count_follows_declared(monkeypatch):
    monkeypatch.setattr(config, "XHS_COMMENT_MAX_PER_NOTE", 0, raising=False)
    monkeypatch.setattr(config, "XHS_COMMENT_COUNT_MARGIN", 30, raising=False)
    # 声明 800 条 → 抓 830 条（不再固定 30）
    assert _comment_max_count(800) == 830
    # 声明未知 → 0 表示不限制，翻到 has_more=False
    assert _comment_max_count(0) == 0


def test_max_count_hard_cap(monkeypatch):
    """XHS_COMMENT_MAX_PER_NOTE 可以压出硬顶（控制单篇耗时）。"""
    monkeypatch.setattr(config, "XHS_COMMENT_COUNT_MARGIN", 30, raising=False)
    monkeypatch.setattr(config, "XHS_COMMENT_MAX_PER_NOTE", 200, raising=False)
    assert _comment_max_count(800) == 200      # 声明数大 → 被硬顶压住
    assert _comment_max_count(50) == 80        # 声明数小 → 按声明数
    assert _comment_max_count(0) == 200        # 声明未知 → 用硬顶


# ---------------------------------------------------------------------------
# 3. 分页抓取的额度语义
# ---------------------------------------------------------------------------
def _client() -> XiaoHongShuClient:
    return XiaoHongShuClient(
        proxy=None, headers={"Cookie": "test"},
        playwright_page=None, cookie_dict={},
    )


def _mock_pages(client, total_top: int, sub_per_comment: int = 0, page: int = 10):
    """造一份分页数据：共 total_top 条一级评论，每条带 sub_per_comment 条子评论。"""
    state = {"pages": 0}

    async def _fetch(note_id, xsec_token, cursor=""):
        start = int(cursor or 0)
        ids = list(range(start, min(start + page, total_top)))
        state["pages"] += 1
        return {
            "comments": [
                {"id": f"c{i}", "content": "x",
                 "sub_comment_count": sub_per_comment,
                 "sub_comments": [{"id": f"c{i}-s{j}"} for j in range(sub_per_comment)],
                 "user_info": {"user_id": f"u{i}"}}
                for i in ids
            ],
            "has_more": start + page < total_top,
            "cursor": str(start + page),
        }

    async def _sub(comments, xsec_token, crawl_interval=0, callback=None,
                   note_id=None, start_sub_cursors=None, **kwargs):
        out = []
        for c in comments:
            out.extend(c.get("sub_comments") or [])
        if callback and out:
            await callback(note_id or "note", out)
        return out, {}

    client.get_note_comments = _fetch
    client.get_comments_all_sub_comments = _sub
    return state


@pytest.mark.asyncio
async def test_unlimited_when_declared_zero():
    """max_count=0：一直翻到 has_more=False。"""
    client = _client()
    _mock_pages(client, total_top=45)
    got = await client.get_note_all_comments(
        note_id="n1", xsec_token="t", crawl_interval=0, max_count=0)
    assert len(got) == 45


@pytest.mark.asyncio
async def test_max_count_applies_to_top_level_only():
    """子评论不占额度：上限 20 条时，一级评论仍能抓到 20 条。"""
    client = _client()
    _mock_pages(client, total_top=100, sub_per_comment=3)
    got = await client.get_note_all_comments(
        note_id="n1", xsec_token="t", crawl_interval=0, max_count=20)
    top = [c for c in got if "-s" not in c["id"]]
    assert len(top) == 20
    # 20 条一级 + 每条 3 条子评论
    assert len(got) == 20 + 60


@pytest.mark.asyncio
async def test_full_crawl_reaches_90_percent():
    """声明 100 条、上限按声明数给：抓完必须 >= 90%（这是增量跳过的前提）。"""
    client = _client()
    _mock_pages(client, total_top=100)
    got = await client.get_note_all_comments(
        note_id="n1", xsec_token="t", crawl_interval=0,
        max_count=_comment_max_count(100))
    assert is_comment_complete(len(got), 100) is True


# ---------------------------------------------------------------------------
# 4. 每轮补抓预算（不是重试次数，只是工作量切片）
# ---------------------------------------------------------------------------
def _row(cid, fetched, declared):
    return (cid, fetched, declared, comment_completeness(fetched, declared))


def test_backfill_prefers_small_gap():
    """差得越少越先补：差 5 条的排在差 5000 条的前面。"""
    rows = [_row("big", 100, 5100), _row("small", 95, 100), _row("mid", 50, 300)]
    selected, deferred = plan_backfill(rows, backfill_limit=2)
    assert [r[0] for r in selected] == ["small", "mid"]
    assert [r[0] for r in deferred] == ["big"]


def test_backfill_budget_by_note_limit():
    rows = [_row(f"c{i}", 0, 10) for i in range(30)]
    selected, deferred = plan_backfill(rows, backfill_limit=20)
    assert len(selected) == 20 and len(deferred) == 10
    # 顺延的帖子仍是「未达标」状态，下轮会重新进入候选
    assert all(r[3] < 0.9 for r in deferred)


def test_backfill_budget_by_comment_count():
    """条数预算：评论为 0 的帖子（huge）优先级最高，先占名额；其余按差多少升序。

    300 条预算下 huge(0/6000) 先入选（首篇不受预算限制，避免死锁），
    后续 a/b/c 因累计超 300 顺延。
    """
    rows = [_row("a", 900, 1000), _row("b", 900, 1000),
            _row("c", 900, 1000), _row("huge", 0, 6000)]
    selected, deferred = plan_backfill(rows, backfill_max_comments=300)
    assert [r[0] for r in selected] == ["huge"]
    assert [r[0] for r in deferred] == ["a", "b", "c"]


def test_backfill_comment_empty_first():
    """评论为 0 的帖子必须排在所有「有评论但没满」的帖子之前。"""
    rows = [_row("near", 95, 100), _row("empty_small", 0, 10),
            _row("empty_big", 0, 9000), _row("half", 50, 100)]
    selected, _ = plan_backfill(rows)
    assert [r[0] for r in selected] == ["empty_small", "empty_big", "near", "half"]


def test_backfill_never_deadlocks_on_huge_gap():
    """预算小于单篇缺口时也要放行第一篇，否则永远补不动。"""
    selected, deferred = plan_backfill([_row("huge", 0, 6000)],
                                       backfill_max_comments=100)
    assert [r[0] for r in selected] == ["huge"]
    assert deferred == []


def test_no_limit_selects_everything():
    rows = [_row(f"c{i}", 0, 10) for i in range(50)]
    selected, deferred = plan_backfill(rows)
    assert len(selected) == 50 and deferred == []
