# -*- coding: utf-8 -*-
"""风控异常传播回归测试。

背景（2026-10-29）：`get_comments_all_sub_comments` 曾用裸 `except Exception`
兜底每条根评论的楼中楼抓取，把 461/471 的 `CaptchaError` 当成「单条评论处理
失败」→ `continue` 到下一个根评论，零间隔再打一发。

后果：
  - 同一秒能连撞好几发 461（日志实测 13:57:45 出现多条），把账号推得更死；
  - 整批不熔断，`_risk_abort_reason` 永远不被设置 → 任务被判定 success
    （RUN 99 出现 45 条 461 仍显示成功），下一轮自然毫无防备地继续跑。

这里锁死两条约定：
  1. 风控异常（CaptchaError / IPBlockError / PlatformAccessError）必须上抛；
  2. 普通异常可以跳过该根评论，但必须先退避再继续，不能零间隔连打。
"""
import asyncio

import pytest

from media_platform.xhs.client import XiaoHongShuClient
from media_platform.xhs.exception import CaptchaError, IPBlockError


class _DummyCtx:
    """占位：这些测试不碰浏览器/网络。"""


@pytest.fixture(autouse=True)
def _enable_sub_comments(monkeypatch):
    """测试默认环境 ENABLE_GET_SUB_COMMENTS 可能是关的，这里强制打开。"""
    import media_platform.xhs.client as client_mod

    monkeypatch.setattr(client_mod.config, "ENABLE_GET_SUB_COMMENTS", True, raising=False)


def _client() -> XiaoHongShuClient:
    client = XiaoHongShuClient(
        proxy=None,
        headers={"Cookie": "test"},
        playwright_page=None,
        cookie_dict={},
    )
    return client


def _comments(n: int):
    return [{"note_id": "N1", "id": f"R{i}", "sub_comment_count": 3,
             "sub_comment_has_more": True, "sub_comments": []}
            for i in range(n)]


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [CaptchaError("461"), IPBlockError("471")])
async def test_risk_error_propagates(monkeypatch, exc):
    """风控异常必须穿透，不能被「继续下一条评论」吞掉。"""
    client = _client()

    async def _boom(**kwargs):
        raise exc

    monkeypatch.setattr(client, "_fetch_sub_pages", _boom)

    with pytest.raises(type(exc)):
        await client.get_comments_all_sub_comments(
            comments=_comments(3), xsec_token="tok", crawl_interval=0)


@pytest.mark.asyncio
async def test_risk_error_after_partial_success_still_raises(monkeypatch):
    """前几条根评论正常、后一条撞风控：仍要整批上抛（不能只丢那一条）。"""
    client = _client()
    calls = {"n": 0}

    async def _first_ok_then_boom(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return [], None
        raise CaptchaError("461")

    monkeypatch.setattr(client, "_fetch_sub_pages", _first_ok_then_boom)

    with pytest.raises(CaptchaError):
        await client.get_comments_all_sub_comments(
            comments=_comments(3), xsec_token="tok", crawl_interval=0)
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_plain_error_skips_but_backs_off(monkeypatch):
    """普通异常：跳过该根评论，但每次失败都要退避，不能零间隔连打。"""
    import media_platform.xhs.client as client_mod

    client = _client()
    sleeps = []

    async def _fake_sleep(reason=""):
        sleeps.append(reason)
        return 0.0

    monkeypatch.setattr(client_mod.anti_risk, "xhs_sleep", _fake_sleep)

    async def _boom(**kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(client, "_fetch_sub_pages", _boom)

    result, open_sub = await client.get_comments_all_sub_comments(
        comments=_comments(3), xsec_token="tok", crawl_interval=0)

    assert result == [] and open_sub == {}
    assert len(sleeps) == 3, f"3 条根评论失败应退避 3 次，实际 {sleeps}"
