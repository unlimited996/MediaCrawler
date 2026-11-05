# -*- coding: utf-8 -*-
"""「停止当前抓取」的统一入口（跨进程）。

一次点击要覆盖两种正在跑的东西：
  1. 本进程的手动抓取（crawler_manager 里的子进程）；
  2. 独立 worker 进程里的定时任务（够不着，只能投信号文件，由它自己杀）。

两者都可能是"没有在跑"，所以这个函数**永远返回 200 结构体**，
由前端按 `stopped` 字段决定提示文案，不再抛 400 让用户以为出错了。
"""
from __future__ import annotations

from typing import Any, Dict


async def stop_current_crawl(reason: str = "用户手动停止") -> Dict[str, Any]:
    from .crawler_manager import crawler_manager

    local_stopped = False
    local_running = False
    try:
        local_running = bool(crawler_manager.is_running())
        if local_running:
            local_stopped = bool(await crawler_manager.stop())
    except Exception:
        local_stopped = False

    signalled = False
    try:
        from tools.crawl_stop import request_stop

        signalled = bool(request_stop(reason))
    except Exception:
        signalled = False

    worker_alive = False
    try:
        from .worker_control import worker_info

        worker_alive = bool(worker_info().get("alive"))
    except Exception:
        pass

    stopped = local_stopped or (signalled and worker_alive)
    if local_stopped:
        message = "已停止当前手动抓取"
    elif signalled and worker_alive:
        message = "已通知抓取执行器停止本次任务（约 1~2 秒内生效）"
    elif not local_running and not worker_alive:
        message = "当前没有正在执行的抓取"
    else:
        message = "已发出停止请求"

    return {
        "status": "ok",
        "stopped": stopped,
        "local_crawl_stopped": local_stopped,
        "worker_alive": worker_alive,
        "stop_signalled": signalled,
        "message": message,
    }
