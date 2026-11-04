# -*- coding: utf-8 -*-
"""独立抓取执行器（worker）—— 与 API 进程完全解耦。

为什么要拆
----------
原来 FastAPI 进程既提供查询接口，又跑定时调度 + 抓取子进程，于是：
  * 重启 API（改代码、调配置）会把正在跑了几小时的抓取一起打断；
  * 反过来，抓取卡住/占用事件循环也会拖慢接口；
  * 起第二个 API 实例还会执行「恢复残留 running 记录」，把对方正在跑的
    执行记录误标成失败（2026-11-04 实际踩到）。

现在的分工
----------
    API 进程   : 只做接口/前端，`SCHEDULER_IN_API=0`（默认）
    Worker 进程: 本文件。独占 logs/.worker.lock，跑 task_scheduler

两者通过「数据库 + 日志文件」通信：
  * API 手动触发任务 → 往 scheduled_task_run 写一条 status='queued'
  * Worker tick 时拾取 queued 记录并执行
  * Worker 的实时日志写入 logs/scheduler_live.jsonl，API 读取展示

启动方式
--------
    python -m worker            # 前台（调试）
    python worker.py            # 等价
    # 常驻：API 启动时会自动 detached 拉起一份（WORKER_AUTO_SPAWN=1）

重启约定
--------
  * 重启 API        → 不影响 worker，抓取继续
  * 重启 worker     → 不影响 API；未完成的帖靠断点续抓在下一轮继续
  * 停 worker       → Ctrl+C（SIGINT）/ SIGTERM，优雅收尾后释放锁
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

# 允许 `python worker.py` 直接跑（把项目根加入 sys.path）
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.proc_lock import (LOG_DIR, acquire_lock, release_channel,  # noqa: E402
                             release_lock)

WORKER_LOCK = LOG_DIR / ".worker.lock"
WORKER_LOG = LOG_DIR / "worker.out.log"
LIVE_LOG = LOG_DIR / "scheduler_live.jsonl"

_stop_event: asyncio.Event | None = None
_lock_fh = None


def _log(msg: str) -> None:
    """打印 + 落实时日志（本地实现，避免启动阶段引入重量级 api 包）。"""
    print(f"[worker] {msg}", flush=True)
    try:
        import json
        import time

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LIVE_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": time.strftime("%H:%M:%S"),
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "level": "info",
                "msg": msg,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _redirect_stdio() -> None:
    """把自身 stdout/stderr 落到 logs/worker.out.log。

    detached 启动时子进程没有控制台，父进程做文件重定向在本环境会静默丢
    输出（见 worker_control 里的说明），由**自己** dup2 到文件最稳。
    这样即使进程在 import 阶段就崩，traceback 也留得住。
    """
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = open(WORKER_LOG, "a", encoding="utf-8",
                  errors="replace", buffering=1)
        os.dup2(fh.fileno(), 1)
        os.dup2(fh.fileno(), 2)
        if sys.stdout is None:
            sys.stdout = open(1, "w", encoding="utf-8", errors="replace",
                              buffering=1, closefd=False)
        if sys.stderr is None:
            sys.stderr = open(2, "w", encoding="utf-8", errors="replace",
                              buffering=1, closefd=False)
    except Exception:
        pass


def _install_signals() -> None:
    """SIGINT/SIGTERM → 置停止事件（Windows 下 SIGTERM 支持有限，兜底 KeyboardInterrupt）。"""
    def _handler(*_a):
        if _stop_event is not None and not _stop_event.is_set():
            _log("收到退出信号，准备收尾…")
            _stop_event.set()

    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass


async def main() -> int:
    global _stop_event

    _stop_event = asyncio.Event()
    _install_signals()

    # 配置中心（配置页改的值走库，必须 load 一次）
    try:
        from config.runtime_config import runtime_config

        await runtime_config.load()
    except Exception as e:
        _log(f"加载运行时配置失败（回退常量）：{e}")

    import config
    from api.services.task_scheduler import task_scheduler
    from api.services import worker_control

    worker_control.write_worker_meta()
    # 执行器的实时日志统一走文件桥，API 侧 /api/crawler/logs 才能看到
    task_scheduler.log_sink = worker_control.append_live
    task_scheduler.runs_in_worker = True

    _log(f"启动：pid={os.getpid()} tick={getattr(config, 'SCHEDULER_TICK_SEC', 15)}s "
         f"并发={getattr(config, 'SCHEDULER_MAX_CONCURRENCY', 1)}")

    ok = await task_scheduler.start()
    if not ok:
        _log(f"调度器启动失败：{task_scheduler.last_error}")
        return 2

    _log("就绪：等待到点任务 / 手动触发排队")
    try:
        await _stop_event.wait()
    except KeyboardInterrupt:
        pass

    _log("正在停止调度器…")
    await task_scheduler.stop()
    _log("已退出")
    return 0


def run() -> int:
    global _lock_fh

    _redirect_stdio()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _lock_fh = acquire_lock(WORKER_LOCK)
    if _lock_fh is None:
        # 已有执行器在跑：直接退出（幂等，不报错）
        print("[worker] 已有执行器在运行（logs/.worker.lock 被占用），本进程退出",
              flush=True)
        return 0

    try:
        return asyncio.run(main())
    except KeyboardInterrupt:
        return 0
    finally:
        try:
            from api.services.worker_control import WORKER_META

            WORKER_META.unlink()
        except Exception:
            pass
        release_channel()
        release_lock(_lock_fh)
        _lock_fh = None


if __name__ == "__main__":
    sys.exit(run())
