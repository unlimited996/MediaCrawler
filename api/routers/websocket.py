# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/api/routers/websocket.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#
# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

import asyncio
import time
from pathlib import Path
from typing import Set, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..services import crawler_manager
from ..services.live_stats import (feed as stats_feed, rebuild as stats_rebuild,
                                  snapshot as stats_snapshot)

router = APIRouter(tags=["websocket"])


class ConnectionManager:
    """WebSocket connection manager"""

    def __init__(self):
        self.active_connections: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.add(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.discard(websocket)

    async def broadcast(self, message: dict):
        """Broadcast message to all connections"""
        if not self.active_connections:
            return

        disconnected = []
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception:
                disconnected.append(connection)

        # Clean up disconnected connections
        for conn in disconnected:
            self.disconnect(conn)


manager = ConnectionManager()


async def log_broadcaster():
    """Background task: read logs from queue and broadcast"""
    queue = crawler_manager.get_log_queue()
    while True:
        try:
            # Get log entry from queue
            entry = await queue.get()
            # Broadcast to all WebSocket connections
            await manager.broadcast(entry.model_dump())
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Log broadcaster error: {e}")
            await asyncio.sleep(0.1)


# Global broadcast task
_broadcaster_task: Optional[asyncio.Task] = None


# ---------------------------------------------------------------------------
# 跨进程日志桥：worker（定时任务执行器）把日志写进 logs/scheduler_live.jsonl，
# 这里把它「增量 tail 出来」推给前端。
#
# 为什么必须这么做：拆分后定时任务跑在 worker 进程里，它的日志不在本进程的
# crawler_manager 内存队列中，只靠内存队列的话，页面上定时任务跑起来是"没日志"的
# （用户反馈的「抓取页面未滚动显示日志」就是这个原因）。
# ---------------------------------------------------------------------------

#: live 日志条目的 id 从这里开始，避免和 crawler_manager 的自增 id 撞号
_LIVE_ID_BASE = 1_000_000
_live_seq = 0
_tailer_task: Optional[asyncio.Task] = None


def _live_entry(item: dict) -> dict:
    """把 JSONL 的一行转成前端认的 LogEntry 结构。"""
    global _live_seq
    _live_seq += 1
    return {
        "id": _LIVE_ID_BASE + _live_seq,
        "timestamp": item.get("ts") or "",
        "level": item.get("level") or "info",
        "message": item.get("msg") or "",
    }


def _read_live_tail(limit: int = 100) -> list:
    """读最近 N 条 worker 日志（新连接进来时补历史，避免页面空白）。"""
    try:
        from ..services.worker_control import tail_live

        return [_live_entry(e) for e in tail_live(limit)]
    except Exception:
        return []


async def _live_log_tailer() -> None:
    """每秒检查一次 live 日志文件，把新增行广播给所有连接。"""
    import os as _os

    try:
        from ..services.worker_control import LIVE_LOG
    except Exception:
        return

    pos = 0
    try:
        if _os.path.exists(LIVE_LOG):
            pos = _os.path.getsize(LIVE_LOG)   # 只推启动之后的新增，历史由连接时补发
    except Exception:
        pos = 0

    # 服务重启后从日志尾部重建当前这一轮的抓取/入库计数
    stats_rebuild(Path(LIVE_LOG))
    _last_stats_push = 0.0

    while True:
        await asyncio.sleep(1.0)
        try:
            if not _os.path.exists(LIVE_LOG):
                continue
            size = _os.path.getsize(LIVE_LOG)
            if size < pos:      # 文件被裁剪/轮转
                pos = 0
            if size == pos:
                continue
            import json as _json

            with open(LIVE_LOG, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(pos)
                chunk = fh.read()
                pos = fh.tell()
            snap = None
            for line in chunk.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    item = _json.loads(line)
                except Exception:
                    continue
                if isinstance(item, dict) and item.get("msg"):
                    await manager.broadcast(_live_entry(item))
                    snap = stats_feed(item["msg"])
                    if snap is not None:
                        # 一条日志一次广播会在补评论时刷屏，节流到 0.5s
                        now = time.monotonic()
                        if now - _last_stats_push >= 0.5:
                            await manager.broadcast(snap)
                            _last_stats_push = now
            # 本批结束时补一次最终值，避免节流把最后一次变化吞掉
            if snap is not None:
                await manager.broadcast(stats_snapshot())
        except Exception:
            continue


def start_broadcaster():
    """Start broadcast task"""
    global _broadcaster_task, _tailer_task
    if _broadcaster_task is None or _broadcaster_task.done():
        _broadcaster_task = asyncio.create_task(log_broadcaster())
    if _tailer_task is None or _tailer_task.done():
        _tailer_task = asyncio.create_task(_live_log_tailer())


@router.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    """WebSocket log stream"""
    print("[WS] New connection attempt")

    try:
        # Ensure broadcast task is running
        start_broadcaster()

        await manager.connect(websocket)
        print(f"[WS] Connected, active connections: {len(manager.active_connections)}")

        # Send existing logs（本进程内存：手动抓取）
        for log in crawler_manager.logs:
            try:
                await websocket.send_json(log.model_dump())
            except Exception as e:
                print(f"[WS] Error sending existing log: {e}")
                break

        # 补发 worker（定时任务）最近的日志，否则新开页面看不到定时任务在干什么
        for entry in _read_live_tail(100):
            try:
                await websocket.send_json(entry)
            except Exception:
                break

        # 补发当前实时统计，避免刚连上要等下一条日志才看到数字
        try:
            await websocket.send_json(stats_snapshot())
        except Exception:
            pass

        print(f"[WS] Sent {len(crawler_manager.logs)} existing logs, entering main loop")

        while True:
            # Keep connection alive, receive heartbeat or any message
            try:
                data = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=30.0
                )
                if data == "ping":
                    await websocket.send_text("pong")
            except asyncio.TimeoutError:
                # Send ping to keep connection alive
                try:
                    await websocket.send_text("ping")
                except Exception as e:
                    print(f"[WS] Error sending ping: {e}")
                    break

    except WebSocketDisconnect:
        print("[WS] Client disconnected")
    except Exception as e:
        print(f"[WS] Error: {type(e).__name__}: {e}")
    finally:
        manager.disconnect(websocket)
        print(f"[WS] Cleanup done, active connections: {len(manager.active_connections)}")


@router.websocket("/ws/status")
async def websocket_status(websocket: WebSocket):
    """WebSocket status stream"""
    await websocket.accept()

    try:
        while True:
            # Send status every second
            status = crawler_manager.get_status()
            await websocket.send_json(status)
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
