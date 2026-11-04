# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/api/services/crawler_manager.py
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
import subprocess
import signal
import os
from typing import Optional, List, Dict, Any
from datetime import datetime
from pathlib import Path

from ..schemas import CrawlerStartRequest, LogEntry, LoginTypeEnum
from .crawl_queue import crawl_queue
from tools.cst import cst_now


class CrawlerManager:
    """Crawler process manager

    单进程执行模型：同一时刻只有一个 main.py 子进程在跑。
    忙时提交的任务进入 crawl_queue 排队，子进程结束后自动取下一批续跑，
    实现"前一个没跑完也能继续加 URL"。
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self.process: Optional[subprocess.Popen] = None
        self.status = "idle"
        self.started_at: Optional[datetime] = None
        self.current_config: Optional[CrawlerStartRequest] = None
        # 最近一次启动失败的原因（供 API 层返回具体错误，而非笼统 500）
        self.last_error: Optional[str] = None
        # 扫码登录的二维码（base64 data url），由子进程日志里的 [QRCODE] 行填充。
        # 后台服务里用户看不到 PIL 弹窗，必须能在 WebUI 上直接扫。
        self.qr_code: Optional[Dict[str, Any]] = None
        self._log_id = 0
        self._logs: List[LogEntry] = []
        self._read_task: Optional[asyncio.Task] = None
        # Project root directory
        self._project_root = Path(__file__).parent.parent.parent
        # Log queue - for pushing to WebSocket
        self._log_queue: Optional[asyncio.Queue] = None
        # 当前这一批来自队列的任务（None 表示本轮是手动直接启动的）
        self._current_batch_id: Optional[int] = None
        self._current_batch_items: List[Any] = []
        # 队列连续启动失败次数，超过阈值就暂停续跑，避免刷屏
        self._queue_failures: int = 0
        self.QUEUE_MAX_FAILURES = 3

    @property
    def logs(self) -> List[LogEntry]:
        return self._logs

    def get_log_queue(self) -> asyncio.Queue:
        """Get or create log queue"""
        if self._log_queue is None:
            self._log_queue = asyncio.Queue()
        return self._log_queue

    def _create_log_entry(self, message: str, level: str = "info") -> LogEntry:
        """Create log entry"""
        self._log_id += 1
        entry = LogEntry(
            id=self._log_id,
            timestamp=cst_now().strftime("%H:%M:%S"),
            level=level,
            message=message
        )
        self._logs.append(entry)
        # Keep last 500 logs
        if len(self._logs) > 500:
            self._logs = self._logs[-500:]
        return entry

    async def _push_log(self, entry: LogEntry):
        """Push log to queue"""
        if self._log_queue is not None:
            try:
                self._log_queue.put_nowait(entry)
            except asyncio.QueueFull:
                pass

    def push_external_log_sync(self, message: str, level: str = "info") -> None:
        """同步版：供定时任务的流式日志回调使用。

        必须在事件循环线程内调用（只做内存 append + queue put_nowait，无 IO）。
        """
        if level not in ("info", "warning", "error", "success", "debug"):
            level = "info"
        entry = self._create_log_entry(message, level)
        if self._log_queue is not None:
            try:
                self._log_queue.put_nowait(entry)
            except asyncio.QueueFull:
                pass

    async def push_external_log(self, message: str, level: str = "info"):
        """把外部模块（定时任务调度器）的日志推进控制台。

        定时任务的子进程输出也走这里，保证手动抓取和定时任务在 WebUI
        控制台是同一个视图，不用切页面就能看到两边在干什么。
        """
        self.push_external_log_sync(message, level)

    def _parse_log_level(self, line: str) -> str:
        """Parse log level"""
        line_upper = line.upper()
        if "ERROR" in line_upper or "FAILED" in line_upper:
            return "error"
        elif "WARNING" in line_upper or "WARN" in line_upper:
            return "warning"
        elif "SUCCESS" in line_upper or "完成" in line or "成功" in line:
            return "success"
        elif "DEBUG" in line_upper:
            return "debug"
        return "info"

    def is_running(self) -> bool:
        """是否已有子进程在跑"""
        return bool(self.process and self.process.poll() is None)

    async def start(self, config: CrawlerStartRequest) -> Dict[str, Any]:
        """启动抓取：空闲则直接跑，正忙则排进队列等前一个跑完自动续跑。

        返回 {"mode": "started"|"queued"|"error", ...}
        """
        async with self._lock:
            if self.is_running():
                res = await self._enqueue_request(config, reason="爬虫正在运行")
                return {"mode": "queued", **res}

            crawl_queue.auto_run = True
            self._queue_failures = 0
            ok = await self._start_locked(config, clear_logs=True)
            return {"mode": "started" if ok else "error"}

    async def start_queue(self) -> Dict[str, Any]:
        """手动触发队列续跑（停止后想继续跑剩下的任务时用）。"""
        async with self._lock:
            if self.is_running():
                return {"mode": "running", "message": "爬虫正在运行"}
            crawl_queue.auto_run = True
            self._queue_failures = 0
            ok = await self._drain_queue_locked()
            if ok:
                return {"mode": "started"}
            stats = crawl_queue._stats_locked()
            return {"mode": "empty", "message": "队列里没有待执行的任务", "pending": stats.get("pending", 0)}

    async def _enqueue_request(self, config: CrawlerStartRequest, reason: str = "") -> Dict[str, Any]:
        """把一个启动请求拆成 URL 粒度排进队列。"""
        urls = self._split_targets(config)
        if not urls:
            return {"added": [], "duplicates": [], "queued": 0}
        res = await crawl_queue.add(urls, config.platform.value,
                                    config.crawler_type.value,
                                    self._options_from_config(config))
        prefix = f"[{reason}] " if reason else ""
        if res["added"]:
            entry = self._create_log_entry(
                f"{prefix}已加入抓取队列 {len(res['added'])} 条，当前任务结束后按顺序执行", "info")
            await self._push_log(entry)
        if res["duplicates"]:
            entry = self._create_log_entry(
                f"队列里已存在 {len(res['duplicates'])} 条相同目标，已跳过", "warning")
            await self._push_log(entry)
        return {"added": res["added"], "duplicates": res["duplicates"], "queued": len(res["added"])}

    @staticmethod
    def _split_targets(config: CrawlerStartRequest) -> List[str]:
        """按抓取模式取出要排队的目标列表。"""
        if config.crawler_type.value == "detail":
            raw = config.specified_ids or ""
        elif config.crawler_type.value == "creator":
            raw = config.creator_ids or ""
        else:
            # 搜索模式：关键词整体作为一条（关键词本身允许逗号，交给 main.py 再拆）
            kw = (config.keywords or "").strip()
            return [kw] if kw else []
        return [u.strip() for u in raw.split(",") if u.strip()]

    @staticmethod
    def _options_from_config(config: CrawlerStartRequest) -> Dict[str, Any]:
        """把启动参数拍平成队列任务可复用的 options。"""
        return {
            "login_type": config.login_type.value,
            "start_page": config.start_page,
            "enable_comments": config.enable_comments,
            "enable_sub_comments": config.enable_sub_comments,
            "enable_media": config.enable_media,
            "save_option": config.save_option.value,
            "headless": config.headless,
            "max_notes_count": config.max_notes_count,
            "max_comments_count": config.max_comments_count,
            "cookies": config.cookies or "",
        }

    @staticmethod
    def _config_from_batch(batch: List[Any]) -> CrawlerStartRequest:
        """把一批同配置队列任务重新组装成一次启动请求（多条 URL 逗号拼接）。"""
        first = batch[0]
        opts = first.options or {}
        urls = ",".join(i.url for i in batch)
        data: Dict[str, Any] = {
            "platform": first.platform,
            "login_type": opts.get("login_type", "cookie"),
            "crawler_type": first.crawler_type,
            "start_page": opts.get("start_page", 1),
            "enable_comments": opts.get("enable_comments", True),
            "enable_sub_comments": opts.get("enable_sub_comments", False),
            "enable_media": opts.get("enable_media", False),
            "save_option": opts.get("save_option", "jsonl"),
            "headless": opts.get("headless", True),
            "cookies": opts.get("cookies", "") or "",
        }
        if first.crawler_type == "detail":
            data["specified_ids"] = urls
        elif first.crawler_type == "creator":
            data["creator_ids"] = urls
        else:
            data["keywords"] = urls
        if opts.get("max_notes_count") is not None:
            data["max_notes_count"] = opts["max_notes_count"]
        if opts.get("max_comments_count") is not None:
            data["max_comments_count"] = opts["max_comments_count"]
        return CrawlerStartRequest(**data)

    async def _drain_queue_locked(self) -> bool:
        """在已持锁的前提下取下一批任务并启动。返回是否真的启动了。"""
        if not crawl_queue.auto_run:
            return False
        if self._queue_failures >= self.QUEUE_MAX_FAILURES:
            return False
        batch = await crawl_queue.next_batch()
        if not batch:
            return False
        config = self._config_from_batch(batch)
        entry = self._create_log_entry(
            f"[队列] 开始执行 {len(batch)} 条任务（剩余待执行 "
            f"{(await crawl_queue.stats()).get('pending', 0)} 条）", "info")
        await self._push_log(entry)
        for item in batch:
            entry = self._create_log_entry(f"[队列 #{item.id}] {item.url[:120]}", "info")
            await self._push_log(entry)
        ok = await self._start_locked(config, clear_logs=False,
                                      batch_id=batch[0].batch_id, batch_items=batch)
        return ok

    async def _start_locked(self, config: CrawlerStartRequest, clear_logs: bool = True,
                            batch_id: Optional[int] = None,
                            batch_items: Optional[List[Any]] = None) -> bool:
        """真正拉起子进程（调用方必须已持有 self._lock）。"""
        # 每次启动重置上次错误
        self.last_error = None
        self.qr_code = None

        # 登录态兜底：请求没带 cookie 时，从账号池取该平台的活跃 cookie 注入。
        # 否则爬虫子进程每次都要扫码（后台服务里用户扫不了码），
        # 表现为"重新拉取"跑不完、登录超时崩溃。
        if not (config.cookies or "").strip():
            pool_cookie = await self._get_pool_cookie(config.platform.value)
            if pool_cookie:
                config.cookies = pool_cookie
                config.login_type = LoginTypeEnum.COOKIE
                entry = self._create_log_entry(
                    f"已从账号池注入 {config.platform.value} 登录态（cookie 模式）", "info")
                await self._push_log(entry)

        # 队列续跑时保留上一批的日志（便于连着看），手动启动才清空
        if clear_logs:
            self._logs = []
            self._log_id = 0

        # Clear pending queue (don't replace object to avoid WebSocket broadcast coroutine holding old queue reference)
        if self._log_queue is None:
            self._log_queue = asyncio.Queue()
        else:
            try:
                while True:
                    self._log_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass

        # 存储前置检查：数据库不可达时直接拦住，
        # 否则子进程会在 init_db 阶段抛一大串 traceback 后退出
        storage_error = await self._check_storage_available(config.save_option.value)
        if storage_error:
            entry = self._create_log_entry(storage_error, "error")
            await self._push_log(entry)
            entry = self._create_log_entry(
                "提示：jsonl / csv / json / excel 为文件存储，无需数据库；"
                "如需 MySQL，请先启动本地 MySQL，或在 .env 中修改 MYSQL_DB_HOST 指向可用实例",
                "warning",
            )
            await self._push_log(entry)
            self.last_error = storage_error
            self.status = "error"
            # 队列任务启动即失败：置 failed 并计一次连续失败，
            # 达上限就暂停续跑，避免同一环境问题把整条队列刷完
            if batch_id is not None:
                await crawl_queue.finish_batch(batch_id, ok=False, error=storage_error)
                self._queue_failures += 1
                if self._queue_failures >= self.QUEUE_MAX_FAILURES:
                    crawl_queue.auto_run = False
                    entry = self._create_log_entry(
                        f"连续 {self._queue_failures} 次启动失败，队列续跑已暂停，"
                        f"修好后点「继续队列」恢复", "error")
                    await self._push_log(entry)
            return False

        # Build command line arguments
        cmd = self._build_command(config)

        # Log start information
        entry = self._create_log_entry(f"Starting crawler: {' '.join(cmd)}", "info")
        await self._push_log(entry)

        try:
            # Start subprocess
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                bufsize=1,
                cwd=str(self._project_root),
                env={**os.environ, "PYTHONUNBUFFERED": "1"}
            )

            self.status = "running"
            self.started_at = datetime.now()
            self.current_config = config
            self._current_batch_id = batch_id
            self._current_batch_items = batch_items or []
            self._queue_failures = 0

            entry = self._create_log_entry(
                f"Crawler started on platform: {config.platform.value}, type: {config.crawler_type.value}",
                "success"
            )
            await self._push_log(entry)

            # Start log reading task
            self._read_task = asyncio.create_task(self._read_output())

            # 跨进程抓取通道：登记「本进程正在抓取」。执行器（worker）那边的
            # 定时任务会看到并排队避让，避免两个浏览器同时打同一个账号。
            # force=True：手动抓取是用户明确指令，即使定时任务在跑也照常登记
            # （此时只提示风险，不强行拦截）。
            try:
                from tools.proc_lock import acquire_channel, read_channel

                cur = read_channel()
                if cur and cur.get("owner") != "manual":
                    entry = self._create_log_entry(
                        "注意：定时任务正在抓取，同时开启手动抓取会加大平台风控风险",
                        "warning")
                    await self._push_log(entry)
                acquire_channel("manual",
                                note=f"{config.platform.value}/{config.crawler_type.value}",
                                force=True)
            except Exception:
                pass

            return True
        except Exception as e:
            self.status = "error"
            entry = self._create_log_entry(f"Failed to start crawler: {str(e)}", "error")
            await self._push_log(entry)
            if batch_id is not None:
                await crawl_queue.finish_batch(batch_id, ok=False, error=str(e))
                self._queue_failures += 1
            return False

    async def stop(self) -> bool:
        """Stop crawler process"""
        async with self._lock:
            if not self.process or self.process.poll() is not None:
                return False

            self.status = "stopping"
            entry = self._create_log_entry("Sending SIGTERM to crawler process...", "warning")
            await self._push_log(entry)

            # 停止 = 停掉整条队列的续跑，而不是只停当前这一批；
            # 队列里剩余的 pending 保留着，用户点「继续队列」可以接着跑
            crawl_queue.auto_run = False

            try:
                self.process.send_signal(signal.SIGTERM)

                # Wait for graceful exit (up to 15 seconds)
                for _ in range(30):
                    if self.process.poll() is not None:
                        break
                    await asyncio.sleep(0.5)

                # If still not exited, force kill
                if self.process.poll() is None:
                    entry = self._create_log_entry("Process not responding, sending SIGKILL...", "warning")
                    await self._push_log(entry)
                    self.process.kill()

                entry = self._create_log_entry("Crawler process terminated", "info")
                await self._push_log(entry)

            except Exception as e:
                entry = self._create_log_entry(f"Error stopping crawler: {str(e)}", "error")
                await self._push_log(entry)

            # Cancel log reading task（先掐掉，否则它检测到进程退出会立刻去取下一批）
            if self._read_task:
                self._read_task.cancel()
                self._read_task = None

            cancelled = await crawl_queue.cancel_running()
            if cancelled:
                entry = self._create_log_entry(
                    f"已终止 {len(cancelled)} 条进行中的队列任务（可重试）", "warning")
                await self._push_log(entry)

            self.status = "idle"
            self.current_config = None
            self._current_batch_id = None
            self._current_batch_items = []
            try:
                from tools.proc_lock import release_channel

                release_channel()
            except Exception:
                pass

            return True

    async def _on_process_finished(self, exit_code: int):
        """子进程结束后的收尾：结掉当前批次，再自动取下一批。

        单独加锁（而不是复用 _read_output 的执行上下文），
        因为 start/stop 也要竞争同一把锁，必须保证互斥。
        """
        async with self._lock:
            batch_id = self._current_batch_id
            self._current_batch_id = None
            self._current_batch_items = []

            if batch_id is not None:
                await crawl_queue.finish_batch(
                    batch_id, ok=(exit_code == 0),
                    error="" if exit_code == 0 else f"子进程退出码 {exit_code}")
                entry = self._create_log_entry(
                    f"[队列] 本批任务{'执行成功' if exit_code == 0 else '执行失败（可重试）'}",
                    "success" if exit_code == 0 else "warning")
                await self._push_log(entry)

            entry = self._create_log_entry(
                "Crawler completed successfully" if exit_code == 0
                else f"Crawler exited with code: {exit_code}",
                "success" if exit_code == 0 else "warning")
            await self._push_log(entry)

            # 清掉句柄，is_running() 才会如实返回 False，下一批才能起
            self.process = None
            try:
                from tools.proc_lock import release_channel

                release_channel()
            except Exception:
                pass

            started = await self._drain_queue_locked()
            if not started:
                self.status = "idle"
                self.current_config = None
                stats = await crawl_queue.stats()
                if stats.get("pending", 0):
                    entry = self._create_log_entry(
                        f"队列里还有 {stats['pending']} 条待执行任务（续跑已暂停）", "warning")
                    await self._push_log(entry)
                else:
                    entry = self._create_log_entry("[队列] 所有任务执行完毕", "success")
                    await self._push_log(entry)

    def get_status(self) -> dict:
        """Get current status"""
        stats = crawl_queue._stats_locked()
        return {
            "status": self.status,
            "platform": self.current_config.platform.value if self.current_config else None,
            "crawler_type": self.current_config.crawler_type.value if self.current_config else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "error_message": None,
            "queue_pending": stats.get("pending", 0),
            "queue_running": stats.get("running", 0),
        }

    def get_qrcode(self) -> Optional[Dict[str, Any]]:
        """取当前等待扫码的二维码（5 分钟内有效，过期返回 None）。"""
        if not self.qr_code:
            return None
        try:
            from datetime import datetime, timedelta
            from tools.cst import CST
            at = datetime.fromisoformat(self.qr_code.get("at") or "")
            if datetime.now(CST) - at > timedelta(minutes=5):
                return None
        except Exception:
            return None
        return self.qr_code

    async def _get_pool_cookie(self, platform: str) -> Optional[str]:
        """从账号池取该平台的活跃登录态（解密后的 cookie 串）。

        失败返回 None（不阻塞启动，调用方会退回默认登录方式）。
        """
        try:
            from sqlalchemy import select
            from sqlalchemy.ext.asyncio import AsyncSession

            from account.crypto import decrypt_cookie
            from config.db_config import resolve_config_db_type
            from database.db_session import get_async_engine
            from database.models_v2 import Account

            engine = get_async_engine(resolve_config_db_type())
            if engine is None:
                return None
            try:
                async with AsyncSession(engine, expire_on_commit=False) as session:
                    row = (await session.execute(
                        select(Account).where(Account.platform == platform,
                                              Account.status == "active")
                        .order_by(Account.updated_at.desc())
                    )).scalars().first()
                    if row is None or not row.cookie_cipher:
                        return None
                    return decrypt_cookie(row.cookie_cipher)
            finally:
                await engine.dispose()
        except Exception as e:
            print(f"[crawler_manager] 取账号池 cookie 失败: {e}")
            return None

    async def _check_storage_available(self, save_option: str) -> Optional[str]:
        """启动前探测数据库连通性。

        返回 None 表示可用；否则返回人类可读的错误信息。
        只探测需要真实数据库服务的存储方式（db/mysql/postgres/sqlite），
        文件类存储（jsonl/json/csv/excel）与 mongodb 不在此拦截。
        """
        db_type = {"db": "mysql", "mysql": "mysql", "postgres": "postgres", "sqlite": "sqlite"}.get(save_option)
        if not db_type:
            return None
        try:
            from database.db_session import get_async_engine
            from sqlalchemy import text

            engine = get_async_engine(db_type)
            if engine is None:
                return f"存储方式 {save_option} 未正确配置（无法创建数据库连接）"
            try:
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
            finally:
                await engine.dispose()
            return None
        except Exception as e:
            first_line = str(e).strip().split("\n")[0]
            if len(first_line) > 220:
                first_line = first_line[:220] + "..."
            return f"数据库连接失败（{save_option}）：{first_line}"

    def _build_command(self, config: CrawlerStartRequest) -> list:
        """Build main.py command line arguments

        用 API 服务自身的解释器启动，保证与 WebUI 同一套环境（项目 venv）。
        原先写死 `uv run`，uv 会用自己的 .venv 并每次重新解析依赖：
        既慢（几十秒），又可能和项目 venv 的依赖版本不一致。
        """
        import sys

        cmd = [sys.executable, "main.py"]

        cmd.extend(["--platform", config.platform.value])
        cmd.extend(["--lt", config.login_type.value])
        cmd.extend(["--type", config.crawler_type.value])
        cmd.extend(["--save_data_option", config.save_option.value])

        # Pass different arguments based on crawler type
        if config.crawler_type.value == "search" and config.keywords:
            cmd.extend(["--keywords", config.keywords])
        elif config.crawler_type.value == "detail" and config.specified_ids:
            cmd.extend(["--specified_id", config.specified_ids])
        elif config.crawler_type.value == "creator" and config.creator_ids:
            cmd.extend(["--creator_id", config.creator_ids])

        if config.start_page != 1:
            cmd.extend(["--start", str(config.start_page)])

        cmd.extend(["--get_comment", "true" if config.enable_comments else "false"])
        cmd.extend(["--get_sub_comment", "true" if config.enable_sub_comments else "false"])
        cmd.extend(["--get_media", "true" if config.enable_media else "false"])

        if config.max_notes_count is not None:
            cmd.extend(["--crawler_max_notes_count", str(config.max_notes_count)])

        if config.max_comments_count is not None:
            cmd.extend(["--max_comments_count_singlenotes", str(config.max_comments_count)])

        if config.cookies:
            cmd.extend(["--cookies", config.cookies])

        cmd.extend(["--headless", "true" if config.headless else "false"])

        return cmd

    async def _read_output(self):
        """Asynchronously read process output"""
        loop = asyncio.get_event_loop()

        try:
            while self.process and self.process.poll() is None:
                # Read a line in thread pool
                line = await loop.run_in_executor(
                    None, self.process.stdout.readline
                )
                if line:
                    line = line.strip()
                    if line:
                        # 二维码行：logging 会给每行加时间戳前缀，所以用包含匹配；
                        # base64 很长，不进日志列表，单独存
                        qr_idx = line.find("[QRCODE]")
                        if qr_idx >= 0:
                            self.qr_code = {
                                "image": line[qr_idx + len("[QRCODE]"):],
                                "at": cst_now().isoformat(),
                            }
                            continue
                        level = self._parse_log_level(line)
                        entry = self._create_log_entry(line, level)
                        await self._push_log(entry)

            # Read remaining output
            if self.process and self.process.stdout:
                remaining = await loop.run_in_executor(
                    None, self.process.stdout.read
                )
                if remaining:
                    for line in remaining.strip().split('\n'):
                        line = line.strip()
                        if not line:
                            continue
                        qr_idx = line.find("[QRCODE]")
                        if qr_idx >= 0:
                            self.qr_code = {
                                "image": line[qr_idx + len("[QRCODE]"):],
                                "at": cst_now().isoformat(),
                            }
                            continue
                        level = self._parse_log_level(line)
                        entry = self._create_log_entry(line, level)
                        await self._push_log(entry)

            # Process ended
            if self.status == "running":
                exit_code = self.process.returncode if self.process else -1
                await self._on_process_finished(exit_code)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            entry = self._create_log_entry(f"Error reading output: {str(e)}", "error")
            await self._push_log(entry)


# Global singleton
crawler_manager = CrawlerManager()
