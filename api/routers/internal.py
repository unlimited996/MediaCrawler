# -*- coding: utf-8 -*-
"""服务间内部接口（不对外暴露，网关不转发 `/internal/**`）。

目前只有一个用途：crawler 服务在「一轮抓取结束」时通知 data 服务，
让数据侧有机会做缓存失效/统计刷新。设计成**单向、可丢、超时短**：
通知失败只打日志，绝不影响抓取。
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any, Deque, Dict

from fastapi import APIRouter

router = APIRouter(prefix="/internal", tags=["internal"])

#: 最近收到的事件（内存环形缓冲，仅供排障查看）
_events: Deque[Dict[str, Any]] = deque(maxlen=200)
_counters: Dict[str, int] = {}


@router.post("/events")
async def receive_event(payload: Dict[str, Any]):
    """接收 crawler 侧的运行事件。

    典型 payload：{"event": "run_finished", "payload": {...}, "ts": 1700000000}
    """
    event = str(payload.get("event") or "unknown")
    _counters[event] = _counters.get(event, 0) + 1
    _events.append({
        "event": event,
        "payload": payload.get("payload") or {},
        "ts": payload.get("ts") or int(time.time()),
        "received_at": int(time.time()),
    })
    # 这里按需接缓存失效逻辑（当前无跨进程缓存，只做记账）
    return {"ok": True, "event": event, "total": _counters[event]}


@router.get("/events")
async def list_events(limit: int = 50):
    """排障用：最近收到的跨服务事件。"""
    items = list(_events)[-max(1, min(limit, 200)):]
    return {"count": len(_events), "counters": _counters, "items": items}
