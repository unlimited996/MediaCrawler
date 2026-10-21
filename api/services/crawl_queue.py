# -*- coding: utf-8 -*-
"""抓取队列：爬虫正忙时新提交的 URL 排队，前一个跑完自动续跑下一个。

以「单个 URL」为粒度入队，方便增删；真正执行时把连续的同配置任务
合并成一批（一次子进程跑多条），避免每条链接都重新拉起一次浏览器。

队列是进程内内存态：API 服务重启会清空。抓取任务本身不适合跨重启恢复
（登录态、浏览器上下文都是临时的），所以不做持久化。
"""

import asyncio
import itertools
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Optional

from ..schemas import QueueItem as QueueItemSchema

# 一次子进程最多合并多少条同配置任务（太多会拖长单次失败的影响面）
DEFAULT_BATCH_SIZE = 5
# 已完成/失败/取消的历史条数上限，防止内存无限增长
MAX_HISTORY = 200
# 单个 URL 长度上限（防止误粘贴一大段文本进来）
MAX_URL_LEN = 2000

TERMINAL_STATUSES = ("done", "failed", "cancelled")


@dataclass
class QueueItem:
    """队列内的一条任务。"""
    id: int
    url: str
    platform: str
    crawler_type: str
    options: dict
    status: str = "pending"
    added_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error: Optional[str] = None
    batch_id: Optional[int] = None

    @property
    def signature(self) -> str:
        """同配置的判定依据：只有配置完全一致的任务才能合进同一批子进程。"""
        o = self.options or {}
        return "|".join([
            self.platform,
            self.crawler_type,
            str(o.get("login_type", "")),
            str(o.get("start_page", 1)),
            str(bool(o.get("enable_comments", True))),
            str(bool(o.get("enable_sub_comments", False))),
            str(bool(o.get("enable_media", False))),
            str(o.get("save_option", "")),
            str(o.get("headless", True)),
            str(o.get("max_notes_count")),
            str(o.get("max_comments_count")),
        ])

    def to_schema(self) -> QueueItemSchema:
        return QueueItemSchema(
            id=self.id,
            url=self.url,
            platform=self.platform,
            crawler_type=self.crawler_type,
            status=self.status,  # type: ignore[arg-type]
            added_at=self.added_at.isoformat() if self.added_at else None,
            started_at=self.started_at.isoformat() if self.started_at else None,
            finished_at=self.finished_at.isoformat() if self.finished_at else None,
            error=self.error,
            batch_id=self.batch_id,
        )


class CrawlQueue:
    """线程/协程安全的 FIFO 抓取队列。"""

    def __init__(self, batch_size: int = DEFAULT_BATCH_SIZE):
        self._items: Dict[int, QueueItem] = {}
        self._order: List[int] = []  # 入队顺序（含已结束项，用于展示）
        self._seq = itertools.count(1)
        self._batch_seq = itertools.count(1)
        self._lock = asyncio.Lock()
        self.batch_size = batch_size
        # 队列自动续跑总开关：stop() 会关掉它，避免"点了停止还在跑"
        self.auto_run = True

    # ---------- 写操作 ----------

    async def add(self, urls: List[str], platform: str, crawler_type: str,
                  options: Optional[dict] = None,
                  ) -> Dict[str, List[int]]:
        """批量入队。

        已在队列里等待/执行中的同 URL 同平台任务会被跳过（去重）。
        返回 {"added": [id...], "duplicates": [url...]}。
        """
        opts = dict(options or {})
        added: List[int] = []
        duplicates: List[str] = []

        async with self._lock:
            for raw in urls:
                url = (raw or "").strip()
                if not url:
                    continue
                if len(url) > MAX_URL_LEN:
                    url = url[:MAX_URL_LEN]
                if self._find_active(url, platform, crawler_type) is not None:
                    duplicates.append(url)
                    continue
                item = QueueItem(
                    id=next(self._seq),
                    url=url,
                    platform=platform,
                    crawler_type=crawler_type,
                    options=opts,
                    added_at=datetime.now(),
                )
                self._items[item.id] = item
                self._order.append(item.id)
                added.append(item.id)
            self._trim_history()
        return {"added": added, "duplicates": duplicates}

    async def remove(self, item_ids: List[int]) -> List[int]:
        """删除指定任务。正在执行的不允许删（避免状态错乱），返回真正删除的 id。"""
        removed: List[int] = []
        async with self._lock:
            for iid in item_ids:
                item = self._items.get(iid)
                if item is None or item.status == "running":
                    continue
                self._items.pop(iid, None)
                if iid in self._order:
                    self._order.remove(iid)
                removed.append(iid)
        return removed

    async def clear(self, statuses: Optional[List[str]] = None) -> List[int]:
        """按状态清空。statuses 为空表示清空全部（不含 running）。"""
        targets = set(statuses) if statuses else {"pending", "done", "failed", "cancelled"}
        removed: List[int] = []
        async with self._lock:
            for iid in list(self._order):
                item = self._items.get(iid)
                if item is None or item.status == "running":
                    continue
                if item.status in targets:
                    self._items.pop(iid, None)
                    self._order.remove(iid)
                    removed.append(iid)
        return removed

    async def requeue(self, item_ids: List[int]) -> List[int]:
        """把失败/取消的任务重新放回队尾等待。"""
        requeued: List[int] = []
        async with self._lock:
            for iid in item_ids:
                item = self._items.get(iid)
                if item is None or item.status not in ("failed", "cancelled"):
                    continue
                item.status = "pending"
                item.started_at = None
                item.finished_at = None
                item.error = None
                item.batch_id = None
                requeued.append(iid)
        return requeued

    # ---------- 读操作 ----------

    async def snapshot(self, statuses: Optional[List[str]] = None,
                       limit: int = 0) -> List[QueueItemSchema]:
        async with self._lock:
            ids = list(self._order)
        items = [self._items[i] for i in ids if i in self._items]
        if statuses:
            items = [i for i in items if i.status in set(statuses)]
        if limit and len(items) > limit:
            items = items[-limit:]
        return [i.to_schema() for i in items]

    async def stats(self) -> Dict[str, int]:
        async with self._lock:
            return self._stats_locked()

    def _stats_locked(self) -> Dict[str, int]:
        """同步版统计（无 await 点，供同步上下文安全读取）。"""
        out = {"pending": 0, "running": 0, "done": 0, "failed": 0, "cancelled": 0}
        try:
            for item in list(self._items.values()):
                out[item.status] = out.get(item.status, 0) + 1
        except RuntimeError:  # 极端并发下字典正在变更，退化成 0 而不是抛异常
            pass
        return out

    async def next_batch(self) -> List[QueueItem]:
        """取出下一批待执行任务。

        只取与队首配置完全一致的连续项，最多 batch_size 条，
        并把它们标记为 running、绑定同一个 batch_id。
        """
        async with self._lock:
            pending = [self._items[i] for i in self._order
                       if i in self._items and self._items[i].status == "pending"]
            if not pending:
                return []
            sig = pending[0].signature
            batch = [i for i in pending if i.signature == sig][: self.batch_size]
            bid = next(self._batch_seq)
            now = datetime.now()
            for item in batch:
                item.status = "running"
                item.started_at = now
                item.batch_id = bid
                item.error = None
            return batch

    async def finish_batch(self, batch_id: int, ok: bool, error: str = "") -> List[int]:
        """结束一批任务，统一置 done / failed。返回受影响的 id。"""
        affected: List[int] = []
        async with self._lock:
            now = datetime.now()
            for item in self._items.values():
                if item.batch_id != batch_id or item.status != "running":
                    continue
                item.status = "done" if ok else "failed"
                item.finished_at = now
                item.error = (error or "")[:500] or None
                affected.append(item.id)
            self._trim_history()
        return affected

    async def cancel_running(self) -> List[int]:
        """把仍在 running 的任务标成 cancelled（stop 时调用）。"""
        affected: List[int] = []
        async with self._lock:
            now = datetime.now()
            for item in self._items.values():
                if item.status == "running":
                    item.status = "cancelled"
                    item.finished_at = now
                    item.error = "已被手动终止"
                    affected.append(item.id)
            self._trim_history()
        return affected

    # ---------- 内部 ----------

    def _find_active(self, url: str, platform: str, crawler_type: str) -> Optional[QueueItem]:
        """找队列里还没结束的同目标（pending / running）。"""
        for item in self._items.values():
            if (item.status in ("pending", "running")
                    and item.url == url
                    and item.platform == platform
                    and item.crawler_type == crawler_type):
                return item
        return None

    def _trim_history(self):
        """只保留最近 MAX_HISTORY 条已结束任务，pending/running 一律保留。"""
        finished = [i for i in self._order
                    if i in self._items and self._items[i].status in TERMINAL_STATUSES]
        if len(finished) <= MAX_HISTORY:
            return
        for iid in finished[: len(finished) - MAX_HISTORY]:
            self._items.pop(iid, None)
            self._order.remove(iid)


# 全局单例
crawl_queue = CrawlQueue()
