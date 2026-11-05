# -*- coding: utf-8 -*-
"""跨进程「停止当前抓取」信号。

为什么需要它
------------
拆分后，**手动抓取**跑在 crawler 服务进程里，**定时任务**跑在 worker 进程里。
前端只连得到 crawler（经网关），点「停止抓取」时，worker 那边正在跑的任务它
根本够不着 —— 两个进程之间没有任何共享内存。

所以用一个**共享目录里的信号文件**（compose 里 `./logs` 同时挂给两个容器）：

    API/UI 点停止  →  写 logs/.stop_crawl（带时间戳）
    worker 的抓取  →  每秒轮询这个文件，命中就 kill 掉子进程并把本轮记为 stopped
                      （消费后立即删除，避免影响下一轮）

语义：**只对「此刻正在跑」的抓取有效**。每次开始新一轮抓取时会先清掉残留标记，
避免「点了停止但当时没在跑」导致下一轮刚起来就被杀。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
STOP_FILE = LOG_DIR / ".stop_crawl"

#: 超过这个时间还没被消费就视为过期（没人跑的时候点了停止）
TTL_SEC = 120


def request_stop(reason: str = "") -> bool:
    """请求停止当前抓取（幂等）。"""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        STOP_FILE.write_text(json.dumps({
            "ts": time.time(),
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "by": os.getpid(),
            "reason": reason or "用户手动停止",
        }, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception:
        return False


def stop_requested() -> Optional[dict]:
    """检查是否有**未过期**的停止请求；有则返回信息。"""
    try:
        if not STOP_FILE.exists():
            return None
        info = json.loads(STOP_FILE.read_text(encoding="utf-8") or "{}")
    except Exception:
        return None
    ts = float(info.get("ts") or 0)
    if ts and (time.time() - ts) > TTL_SEC:
        clear_stop()
        return None
    return info or {"reason": "用户手动停止"}


def clear_stop() -> None:
    """清掉标记（新抓取开始 / 已消费时调用）。"""
    try:
        STOP_FILE.unlink()
    except Exception:
        pass
