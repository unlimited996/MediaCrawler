# -*- coding: utf-8 -*-
"""抓取实时计数：从 worker 的 JSONL 日志里解析「抓取」与「入库」。

拆分部署后 worker 是独立进程，日志只经 `logs/scheduler_live.jsonl` 传给
crawler API 进程；这里在 **API 进程内**累计，既供 WS 推给系统控制台，
也供 /scheduler/status 带出给执行记录页（前端原本就 5s 轮询一次）。

口径（都在日志里有明确标记，不做猜测）：
  * 抓取数 = 本轮向平台发起的数据抓取动作，形如
             `[XiaoHongShuCrawler.get_comments]` / `[XiaoHongShuClient._fetch_sub_pages]`
  * 入库数 = 本轮落库的数据条目，形如
             `[store.xhs.update_xhs_note]` / `[store.xhs.update_xhs_note_comment]`
  * worker 每轮开始打一条 `[MC_RUN_BEGIN]`，据此把计数归零并绑定 run_id。
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

#: 本轮开始标记（worker 侧 task_scheduler._execute 发出）
RUN_BEGIN_MARKER = "[MC_RUN_BEGIN]"
_BEGIN_RE = re.compile(r"\[MC_RUN_BEGIN\](?:\s+run_id=(\d+))?(?:\s+task_id=(\d+))?")
#: 抓取动作：[XxxCrawler.xxx] / [XxxClient.xxx]
_FETCH_RE = re.compile(r"\[[A-Za-z0-9_]*(?:Crawler|Client)\.[A-Za-z0-9_]+\]")
#: 落库条目：[store.<platform>.<func>]
_SAVE_RE = re.compile(r"\[store\.[A-Za-z0-9_.]+\]")

_state: Dict[str, Any] = {
    "fetched": 0,
    "saved": 0,
    "run_id": None,
    "task_id": None,
    "started_at": None,
    "updated_at": None,
}
_lock = threading.Lock()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _copy() -> Dict[str, Any]:
    out = dict(_state)
    out["type"] = "stats"
    return out


def snapshot() -> Dict[str, Any]:
    """当前快照（含 type=stats，可直接经 WS 广播）。"""
    with _lock:
        return _copy()


def feed(message: str) -> Optional[Dict[str, Any]]:
    """喂一行日志；计数有变化时返回新快照，否则返回 None。"""
    if not message:
        return None
    with _lock:
        begin = _BEGIN_RE.search(message)
        if begin:
            _state.update(
                fetched=0,
                saved=0,
                run_id=int(begin.group(1)) if begin.group(1) else None,
                task_id=int(begin.group(2)) if begin.group(2) else None,
                started_at=_now(),
                updated_at=_now(),
            )
            return _copy()

        n_fetch = len(_FETCH_RE.findall(message))
        n_save = len(_SAVE_RE.findall(message))
        if not n_fetch and not n_save:
            return None
        _state["fetched"] += n_fetch
        _state["saved"] += n_save
        _state["updated_at"] = _now()
        return _copy()


def rebuild(live_log: Path, limit: int = 4000) -> Dict[str, Any]:
    """服务重启后从 JSONL 尾部重建计数（只认最后一轮 MC_RUN_BEGIN 之后的日志）。"""
    try:
        if not live_log.exists():
            return snapshot()
        lines = live_log.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except Exception:
        return snapshot()

    last_begin = -1
    for i in range(len(lines) - 1, -1, -1):
        try:
            if RUN_BEGIN_MARKER in (json.loads(lines[i]).get("msg") or ""):
                last_begin = i
                break
        except Exception:
            continue

    with _lock:
        _state.update(fetched=0, saved=0, run_id=None, task_id=None,
                      started_at=None, updated_at=None)
    for line in lines[last_begin:] if last_begin >= 0 else []:
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict) and item.get("msg"):
            feed(item["msg"])
    return snapshot()
