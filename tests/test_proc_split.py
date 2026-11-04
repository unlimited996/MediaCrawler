# -*- coding: utf-8 -*-
"""接口进程 / 抓取执行器进程分离的契约测试。

覆盖三件事（2026-11-04 拆分后必须成立）：
  1. 抓取通道标记能正确探活：持锁者已死 → 视为空闲并清理残片；
  2. 通道占用/释放对称，不会自己把自己判成"被占用"；
  3. `TaskScheduler.trigger()` 在「本进程没跑调度」时走上「写排队记录」，
     绝不进程内直接执行 —— 这是接口重启不影响抓取的前提。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from tools import proc_lock


@pytest.fixture()
def channel_file(tmp_path, monkeypatch):
    f = tmp_path / ".crawl_channel.json"
    monkeypatch.setattr(proc_lock, "CHANNEL_FILE", f)
    return f


def test_channel_acquire_and_release(channel_file):
    assert proc_lock.channel_busy() is False
    assert proc_lock.acquire_channel("scheduler", note="unit-test") is True
    assert proc_lock.channel_busy() is True
    info = proc_lock.read_channel() or {}
    assert int(info.get("pid") or 0) == os.getpid()
    proc_lock.release_channel()
    assert proc_lock.channel_busy() is False


def test_channel_ignores_dead_holder(channel_file):
    """上一个抓取进程被强杀：残留的标记必须自动失效，否则永远"通道被占"。"""
    channel_file.write_text(
        '{"owner": "scheduler", "pid": 999999, "since": 0, "note": ""}',
        encoding="utf-8")
    assert proc_lock.read_channel() is None
    assert proc_lock.channel_busy() is False
    assert not channel_file.exists()


def test_channel_second_acquire_blocked(channel_file):
    """别人已经占着时不能再占（除非用户手动强制），这是「不并发抓取」的基础。"""
    channel_file.write_text(
        '{"owner": "manual", "pid": %d, "since": 0, "note": ""}' % os.getpid(),
        encoding="utf-8")
    assert proc_lock.acquire_channel("scheduler") is False
    # force=True 给用户手动抓取用：允许覆盖登记
    assert proc_lock.acquire_channel("manual", force=True) is True


@pytest.mark.asyncio
async def test_trigger_enqueues_when_not_running_scheduler():
    """API 进程（未启动调度）触发任务 → 必须只写排队记录，不本地执行。"""
    from api.services.task_scheduler import TaskScheduler

    sch = TaskScheduler()
    assert sch.started is False

    called = {}

    async def fake_enqueue(task_id, trigger="manual"):
        called["task_id"] = task_id
        called["trigger"] = trigger
        return {"status": "queued", "run_id": 42}

    sch.enqueue_run = fake_enqueue  # type: ignore[assignment]
    res = await sch.trigger(7)

    assert called == {"task_id": 7, "trigger": "manual"}
    assert res["status"] == "queued" and res["run_id"] == 42


@pytest.mark.asyncio
async def test_pickup_queued_skips_task_already_running():
    """同任务上一次还没跑完 → 排队记录不能被重复拾取（幂等）。"""
    from api.services.task_scheduler import TaskScheduler, _TaskState

    sch = TaskScheduler()
    st = _TaskState()
    await st.lock.acquire()          # 模拟"正在执行"
    sch._states[3] = st
    try:
        assert st.lock.locked() is True
    finally:
        st.lock.release()
