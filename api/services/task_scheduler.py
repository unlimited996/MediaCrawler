# -*- coding: utf-8 -*-
"""定时拉取任务调度器。

设计要点：
- 纯 asyncio 实现，不引入 APScheduler 等新依赖（保持 Docker 构建稳定）；
  调度粒度由 SCHEDULER_TICK_SEC（默认 15s）控制，cron 精确到分钟。
- 两种调度方式：interval（秒）与 cron（5 段标准表达式，东八区）。
- 两类任务粒度：profile（按用户主页全量/增量刷新）、note（按帖子地址更新单帖）。
- 幂等保证：每个任务一把运行锁（重叠触发直接跳过）+ 抓取通道等待
  （手动抓取占用时排队，超时记 skipped，绝不并发抢同一个浏览器配置）。
- 异常隔离：tick / 单次执行 / DB 读写全部 try 包裹，任何异常只记进
  运行记录，不会杀死调度循环，也不影响手动抓取主流程。
- 任务与执行记录持久化在 v2 库（scheduled_task / scheduled_task_run），
  服务重启后自动恢复调度。
"""

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import config
from sqlalchemy import select, update

from database.models_v2 import ScheduledTask, ScheduledTaskRun
from tools.cst import cst_now, to_cst, utc_now
from tools.v2_sync import get_content_index  # noqa: F401  （复用同库会话约定）

# cron 名称映射
_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_DOW_NAMES = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}


class CronError(ValueError):
    """cron 表达式非法。"""


class CronSpec:
    """解析后的 5 段 cron。

    段：minute(0-59) hour(0-23) day-of-month(1-31) month(1-12) day-of-week(0-7, 0/7=周日)
    支持：* 、a 、a-b 、*/n 、a-b/n 、逗号列表、月份/星期英文名。
    day-of-month 与 day-of-week 同时受限时按 crontab 标准取「或」。
    """

    __slots__ = ("minutes", "hours", "days", "months", "dows",
                 "dom_restricted", "dow_restricted", "raw")

    def __init__(self, raw: str):
        self.raw = raw
        parts = raw.split()
        if len(parts) != 5:
            raise CronError(f"cron 需要 5 段（分 时 日 月 周），收到 {len(parts)} 段")
        self.minutes = self._parse_field(parts[0], 0, 59, None)
        self.hours = self._parse_field(parts[1], 0, 23, None)
        self.days = self._parse_field(parts[2], 1, 31, None)
        self.months = self._parse_field(parts[3], 1, 12, _MONTH_NAMES)
        dows = self._parse_field(parts[4], 0, 7, _DOW_NAMES)
        # 0 和 7 都表示周日，统一成 0
        if 7 in dows:
            dows = (dows - {7}) | {0}
        self.dows = dows
        self.dom_restricted = parts[2] != "*"
        self.dow_restricted = parts[4] != "*"

    @staticmethod
    def _parse_field(spec: str, lo: int, hi: int,
                     names: Optional[Dict[str, int]]) -> set:
        out: set = set()
        for part in spec.split(","):
            part = part.strip().lower()
            if not part:
                raise CronError(f"cron 段为空: '{spec}'")
            step = 1
            if "/" in part:
                part, step_s = part.split("/", 1)
                try:
                    step = int(step_s)
                except ValueError:
                    raise CronError(f"cron 步长非法: '{part}/{step_s}'")
                if step < 1:
                    raise CronError(f"cron 步长必须 >= 1: '{step_s}'")
            if part in ("*", ""):
                start, end = lo, hi
            elif "-" in part:
                a, b = part.split("-", 1)
                start, end = CronSpec._to_int(a, lo, hi, names), CronSpec._to_int(b, lo, hi, names)
            else:
                v = CronSpec._to_int(part, lo, hi, names)
                start, end = v, (hi if "/" in spec and step > 1 else v)
                # `5/10` 这类写法表示从 5 开始每 10
                if step > 1:
                    end = hi
            if start > end:
                raise CronError(f"cron 区间起点大于终点: '{part}'")
            out.update(range(start, end + 1, step))
        if not out:
            raise CronError(f"cron 段无有效值: '{spec}'")
        return out

    @staticmethod
    def _to_int(token: str, lo: int, hi: int,
                names: Optional[Dict[str, int]]) -> int:
        token = token.strip().lower()
        if names and token in names:
            v = names[token]
        else:
            try:
                v = int(token)
            except ValueError:
                raise CronError(f"cron 值非法: '{token}'")
        if v < lo or v > hi:
            raise CronError(f"cron 值越界 [{lo},{hi}]: '{token}'")
        return v

    def _day_match(self, dt: datetime) -> bool:
        dom_ok = dt.day in self.days
        # python weekday: Monday=0..Sunday=6 → cron: Sunday=0..Saturday=6
        dow = (dt.weekday() + 1) % 7
        dow_ok = dow in self.dows
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def next_after(self, after: datetime) -> Optional[datetime]:
        """after 之后的下一次触发时间（东八区，分钟精度）。"""
        t = to_cst(after).replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(200000):  # 约 138 天的分钟数，足够覆盖任何合法表达式
            if t.month not in self.months:
                # 跳到下个月 1 号 00:00
                year, month = (t.year + 1, 1) if t.month == 12 else (t.year, t.month + 1)
                t = t.replace(year=year, month=month, day=1, hour=0, minute=0)
                continue
            if not self._day_match(t):
                t = (t + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if t.hour not in self.hours:
                t = (t + timedelta(hours=1)).replace(minute=0)
                continue
            if t.minute not in self.minutes:
                t = t + timedelta(minutes=1)
                continue
            return t
        return None


def parse_cron(expr: str) -> CronSpec:
    return CronSpec((expr or "").strip())


@asynccontextmanager
async def _session():
    """调度器专用的 v2 库会话（与 tools/v2_sync 同一套 engine 缓存）。"""
    from config.db_config import resolve_config_db_type
    from database.db_session import get_async_engine
    from sqlalchemy.ext.asyncio import AsyncSession

    engine = get_async_engine(resolve_config_db_type())
    if engine is None:
        yield None
        return
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


class _TaskState:
    """任务的内存态（运行锁 + 上次调度记录）。"""

    __slots__ = ("lock", "running")

    def __init__(self):
        self.lock = asyncio.Lock()
        self.running = False


class TaskScheduler:
    """定时拉取任务调度器（单例，随 API 服务启停）。"""

    def __init__(self):
        self._states: Dict[int, _TaskState] = {}
        self._sem: Optional[asyncio.Semaphore] = None
        self._loop_task: Optional[asyncio.Task] = None
        self._stopping = False
        self.started = False
        self.last_tick_at: Optional[datetime] = None
        self.last_error = ""

    # ---------- 生命周期 ----------

    async def start(self) -> bool:
        """启动调度器：建表 → 清理残留 → 加载任务 → 起循环。"""
        if self.started:
            return True
        if not await self._ensure_tables():
            self.last_error = "调度器元数据表不可用（数据库未配置）"
            return False
        self._sem = asyncio.Semaphore(max(1, int(config.SCHEDULER_MAX_CONCURRENCY)))
        await self._recover_stale_runs()
        await self.reload()
        self._stopping = False
        self._loop_task = asyncio.create_task(self._loop())
        self.started = True
        return True

    async def stop(self) -> None:
        self._stopping = True
        self.started = False
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):
                pass
            self._loop_task = None

    async def _ensure_tables(self) -> bool:
        try:
            from config.db_config import resolve_config_db_type
            from database.db_session import get_async_engine

            engine = get_async_engine(resolve_config_db_type())
            if engine is None:
                return False
            async with engine.begin() as conn:
                await conn.run_sync(ScheduledTask.__table__.create, checkfirst=True)
                await conn.run_sync(ScheduledTaskRun.__table__.create, checkfirst=True)
            return True
        except Exception as e:
            self.last_error = f"建表失败: {e}"
            return False

    async def _recover_stale_runs(self) -> None:
        """把上次进程残留的 running 记录标记为失败（服务重启过）。"""
        try:
            async with _session() as session:
                if session is None:
                    return
                await session.execute(
                    update(ScheduledTaskRun)
                    .where(ScheduledTaskRun.status == "running")
                    .values(status="failed",
                            error_message="服务重启，执行中断",
                            finished_at=utc_now())
                )
                await session.execute(
                    update(ScheduledTask).where(ScheduledTask.status == "running")
                    .values(status="idle")
                )
                await session.commit()
        except Exception as e:
            self.last_error = f"清理残留运行记录失败: {e}"

    async def reload(self) -> None:
        """从数据库重载任务（新建/改配置后调用）。"""
        try:
            async with _session() as session:
                if session is None:
                    return
                rows = (await session.execute(select(ScheduledTask))).scalars().all()
                for row in rows:
                    if row.id not in self._states:
                        self._states[row.id] = _TaskState()
                    # 到点时间兜底：没有 next_run_at 的立即补算，避免永不触发
                    if row.enabled and row.next_run_at is None:
                        row.next_run_at = self._compute_next(row)
                await session.commit()
        except Exception as e:
            self.last_error = f"加载任务失败: {e}"

    async def _loop(self) -> None:
        tick = max(1, int(config.SCHEDULER_TICK_SEC))
        while not self._stopping:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 调度循环本身绝不允许死
                self.last_error = f"tick 异常: {e}"
            await asyncio.sleep(tick)

    async def _tick(self) -> None:
        self.last_tick_at = cst_now()
        try:
            async with _session() as session:
                if session is None:
                    return
                rows = (await session.execute(
                    select(ScheduledTask).where(ScheduledTask.enabled.is_(True))
                )).scalars().all()
                due = [r for r in rows
                       if r.next_run_at is not None
                       and to_cst(r.next_run_at) <= self.last_tick_at]
        except Exception as e:
            self.last_error = f"tick 查询失败: {e}"
            return

        for row in due:
            state = self._states.get(row.id)
            if state is None or state.running or state.lock.locked():
                continue  # 上次还没跑完 → 跳过本轮（幂等，不重叠）
            asyncio.create_task(self._execute(row.id, trigger="schedule"))

    # ---------- 调度计算 ----------

    @staticmethod
    def _compute_next(task: ScheduledTask) -> Optional[datetime]:
        """算下一次触发时间（UTC aware，写库用）。"""
        now = cst_now()
        if (task.schedule_type or "interval") == "cron":
            try:
                spec = parse_cron(task.cron_expr or "")
            except CronError:
                return None
            nxt = spec.next_after(now)
            return nxt.astimezone(timezone.utc) if nxt else None
        interval = int(task.interval_sec or 0)
        if interval <= 0:
            return None
        base = to_cst(task.last_run_at) if task.last_run_at else now
        if base < now:
            base = now
        return (base + timedelta(seconds=interval)).astimezone(timezone.utc)

    # ---------- 任务 CRUD ----------

    async def create_task(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """创建定时任务。data 已通过 schema 校验。"""
        task_code = f"sched-{uuid.uuid4().hex[:12]}"
        async with _session() as session:
            if session is None:
                raise RuntimeError("数据库不可用，无法创建定时任务")
            row = ScheduledTask(
                task_code=task_code,
                name=data["name"],
                platform=data["platform"],
                task_type=data["task_type"],
                target=data["target"],
                mode=data.get("mode", "incremental"),
                schedule_type=data.get("schedule_type", "interval"),
                interval_sec=data.get("interval_sec"),
                cron_expr=(data.get("cron_expr") or "").strip() or None,
                max_concurrency=int(data.get("max_concurrency", 1)),
                timeout_sec=int(data.get("timeout_sec", 1800)),
                retry_count=int(data.get("retry_count", 1)),
                retry_delay_sec=int(data.get("retry_delay_sec", 60)),
                rate_interval_sec=int(data.get("rate_interval_sec", 3)),
                options_json=data.get("options") or {},
                enabled=bool(data.get("enabled", True)),
            )
            session.add(row)
            await session.flush()
            row.next_run_at = self._compute_next(row)
            await session.commit()
            out = self._task_dict(row)
        self._states.setdefault(out["id"], _TaskState())
        return out

    async def update_task(self, task_id: int,
                          data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        async with _session() as session:
            if session is None:
                raise RuntimeError("数据库不可用")
            row = await session.get(ScheduledTask, task_id)
            if row is None:
                return None
            for key in ("name", "platform", "task_type", "target", "mode",
                        "schedule_type", "interval_sec", "cron_expr",
                        "max_concurrency", "timeout_sec", "retry_count",
                        "retry_delay_sec", "rate_interval_sec", "enabled"):
                if key in data and data[key] is not None:
                    setattr(row, key, data[key])
            if "cron_expr" in data:
                row.cron_expr = (data.get("cron_expr") or "").strip() or None
            if "options" in data:
                row.options_json = data.get("options") or {}
            # 调度配置变了就重算下次触发，避免沿用旧周期
            sched_keys = {"schedule_type", "interval_sec", "cron_expr"}
            if sched_keys & set(data.keys()):
                row.next_run_at = self._compute_next(row)
            await session.commit()
            out = self._task_dict(row)
        self._states.setdefault(task_id, _TaskState())
        return out

    async def delete_task(self, task_id: int) -> bool:
        async with _session() as session:
            if session is None:
                raise RuntimeError("数据库不可用")
            row = await session.get(ScheduledTask, task_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
        self._states.pop(task_id, None)
        return True

    async def set_enabled(self, task_id: int,
                          enabled: bool) -> Optional[Dict[str, Any]]:
        async with _session() as session:
            if session is None:
                raise RuntimeError("数据库不可用")
            row = await session.get(ScheduledTask, task_id)
            if row is None:
                return None
            row.enabled = enabled
            row.next_run_at = self._compute_next(row)
            await session.commit()
            out = self._task_dict(row)
        return out

    async def list_tasks(self) -> List[Dict[str, Any]]:
        async with _session() as session:
            if session is None:
                return []
            rows = (await session.execute(
                select(ScheduledTask).order_by(ScheduledTask.id.desc())
            )).scalars().all()
            return [self._task_dict(r) for r in rows]

    async def get_task(self, task_id: int) -> Optional[Dict[str, Any]]:
        async with _session() as session:
            if session is None:
                return None
            row = await session.get(ScheduledTask, task_id)
            return self._task_dict(row) if row else None

    async def list_runs(self, task_id: Optional[int] = None,
                        limit: int = 50) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit or 50), 500))
        async with _session() as session:
            if session is None:
                return []
            stmt = select(ScheduledTaskRun)
            if task_id:
                stmt = stmt.where(ScheduledTaskRun.task_id == task_id)
            rows = (await session.execute(
                stmt.order_by(ScheduledTaskRun.id.desc()).limit(limit)
            )).scalars().all()
            return [self._run_dict(r) for r in rows]

    # ---------- 执行 ----------

    async def trigger(self, task_id: int) -> Dict[str, Any]:
        """手动触发一次（API 用）。"""
        return await self._execute(task_id, trigger="manual")

    async def _execute(self, task_id: int, trigger: str = "manual") -> Dict[str, Any]:
        from .profile_crawler import execute_task

        state = self._states.get(task_id)
        if state is None:
            state = _TaskState()
            self._states[task_id] = state
        if state.lock.locked():
            return {"status": "skipped", "reason": "already_running",
                    "message": "该任务正在执行中，已跳过本次触发（幂等保护）"}

        async with state.lock:
            state.running = True
            run_id = None
            try:
                async with _session() as session:
                    if session is None:
                        return {"status": "failed", "message": "数据库不可用"}
                    row = await session.get(ScheduledTask, task_id)
                    if row is None:
                        return {"status": "failed", "message": "任务不存在"}
                    run = ScheduledTaskRun(
                        task_id=task_id,
                        trigger_type=trigger,
                        status="running",
                        attempt_count=1,
                    )
                    session.add(run)
                    row.status = "running"
                    await session.commit()
                    run_id = run.id
                    task_dict = self._task_dict(row)

                # 并发闸口：多个任务同时到点时按 SCHEDULER_MAX_CONCURRENCY 放行
                assert self._sem is not None
                async with self._sem:
                    # 登录态兜底：任务没存 cookie 就从账号池取
                    opts = dict(task_dict.get("options") or {})
                    if not (opts.get("cookies") or "").strip():
                        try:
                            from .crawler_manager import crawler_manager
                            pool_cookie = await crawler_manager._get_pool_cookie(
                                task_dict["platform"])
                            if pool_cookie:
                                opts["cookies"] = pool_cookie
                                opts["login_type"] = "cookie"
                        except Exception:
                            pass
                    task_dict["options"] = opts

                    # 存储前置检查（与手动抓取一致）：数据库不可达直接失败，
                    # 不起子进程，避免刷一屏 traceback
                    storage_error = await self._check_storage(task_dict)
                    if storage_error:
                        raise RuntimeError(storage_error)

                    log_lines: List[str] = []

                    def _on_log(msg: str, level: str = "info") -> None:
                        log_lines.append(f"[{level}] {msg}")
                        if len(log_lines) > 200:
                            del log_lines[: len(log_lines) - 200]
                        # 同步到 WebUI 控制台，手动抓取和定时任务一个视图看
                        try:
                            from .crawler_manager import crawler_manager
                            crawler_manager.push_external_log_sync(
                                f"[定时任务#{task_id}] {msg}", level)
                        except Exception:
                            pass

                    result = await execute_task(
                        task_dict,
                        busy_wait_sec=int(config.SCHEDULER_BUSY_WAIT_SEC),
                        is_busy=self._channel_busy,
                        on_log=_on_log,
                    )

                summary = {
                    "status": result.status,
                    "new_count": result.new_count,
                    "updated_count": result.updated_count,
                    "skipped_count": result.skipped_count,
                    "attempts": result.attempts,
                    "error": result.error,
                    "run_id": run_id,
                    "duration_ms": result.duration_ms,
                }
                await self._finish_run(task_id, run_id, summary, result.log_tail or log_lines)
                return summary
            except Exception as e:
                # 执行器之外的异常（DB / 前置检查）也统一落成失败记录
                await self._finish_run(
                    task_id, run_id,
                    {"status": "failed", "new_count": 0, "updated_count": 0,
                     "skipped_count": 0, "attempts": 1, "error": str(e)[:500]},
                    [], mark_task_error=True)
                return {"status": "failed", "error": str(e)[:500], "run_id": run_id}
            finally:
                state.running = False

    async def _finish_run(self, task_id: int, run_id: Optional[int],
                          summary: Dict[str, Any], log_tail: List[str],
                          mark_task_error: bool = False) -> None:
        """收尾：写运行记录 + 任务状态 + 下次触发时间 + 历史裁剪。"""
        try:
            async with _session() as session:
                if session is None:
                    return
                task = await session.get(ScheduledTask, task_id)
                if run_id is not None:
                    run = await session.get(ScheduledTaskRun, run_id)
                    if run is not None:
                        run.status = summary.get("status", "failed")
                        run.finished_at = utc_now()
                        run.duration_ms = summary.get("duration_ms")
                        run.new_count = int(summary.get("new_count") or 0)
                        run.updated_count = int(summary.get("updated_count") or 0)
                        run.skipped_count = int(summary.get("skipped_count") or 0)
                        run.attempt_count = int(summary.get("attempts") or 1)
                        run.error_message = (summary.get("error") or "")[:500] or None
                        run.log_json = (log_tail or [])[-200:]
                if task is not None:
                    task.last_run_at = utc_now()
                    task.last_status = summary.get("status", "failed")
                    task.last_error = (summary.get("error") or "")[:500] or None
                    task.run_count = int(task.run_count or 0) + 1
                    task.status = "error" if (mark_task_error or
                                              summary.get("status") == "failed") else "idle"
                    task.next_run_at = self._compute_next(task)
                await session.commit()
                await self._trim_runs(session, task_id)
        except Exception as e:
            self.last_error = f"写运行记录失败: {e}"

    async def _trim_runs(self, session, task_id: int) -> None:
        """每个任务只保留最近 SCHEDULER_RUN_HISTORY 条运行记录。"""
        try:
            keep = max(10, int(config.SCHEDULER_RUN_HISTORY))
            rows = (await session.execute(
                select(ScheduledTaskRun.id)
                .where(ScheduledTaskRun.task_id == task_id)
                .order_by(ScheduledTaskRun.id.desc())
                .offset(keep)
            )).scalars().all()
            for rid in rows:
                run = await session.get(ScheduledTaskRun, rid)
                if run is not None:
                    await session.delete(run)
            await session.commit()
        except Exception:
            pass

    @staticmethod
    def _channel_busy() -> bool:
        """抓取通道是否被手动抓取占用。"""
        try:
            from .crawler_manager import crawler_manager
            return crawler_manager.is_running()
        except Exception:
            return False

    @staticmethod
    async def _check_storage(task: Dict[str, Any]) -> Optional[str]:
        try:
            from .crawler_manager import crawler_manager
            return await crawler_manager._check_storage_available(
                (task.get("options") or {}).get("save_option", "postgres"))
        except Exception:
            return None

    # ---------- 状态 ----------

    async def get_status(self) -> Dict[str, Any]:
        running = sum(1 for s in self._states.values() if s.running)
        try:
            async with _session() as session:
                if session is None:
                    total = enabled = 0
                else:
                    from sqlalchemy import func
                    total = (await session.execute(
                        select(func.count()).select_from(ScheduledTask))).scalar() or 0
                    enabled = (await session.execute(
                        select(func.count()).select_from(ScheduledTask)
                        .where(ScheduledTask.enabled.is_(True)))).scalar() or 0
        except Exception:
            total = enabled = 0
        return {
            "started": self.started,
            "enabled": bool(config.SCHEDULER_ENABLED),
            "max_concurrency": int(config.SCHEDULER_MAX_CONCURRENCY),
            "tick_sec": int(config.SCHEDULER_TICK_SEC),
            "total_tasks": int(total),
            "enabled_tasks": int(enabled),
            "running_tasks": running,
            "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
            "last_error": self.last_error,
        }

    # ---------- 序列化 ----------

    @staticmethod
    def _task_dict(row: ScheduledTask) -> Dict[str, Any]:
        from tools.cst import cst_iso
        return {
            "id": row.id,
            "task_code": row.task_code,
            "name": row.name,
            "platform": row.platform,
            "task_type": row.task_type,
            "target": row.target,
            "mode": row.mode,
            "schedule_type": row.schedule_type,
            "interval_sec": row.interval_sec,
            "cron_expr": row.cron_expr,
            "max_concurrency": row.max_concurrency,
            "timeout_sec": row.timeout_sec,
            "retry_count": row.retry_count,
            "retry_delay_sec": row.retry_delay_sec,
            "rate_interval_sec": row.rate_interval_sec,
            "options": row.options_json or {},
            "enabled": bool(row.enabled),
            "status": row.status,
            "last_run_at": cst_iso(row.last_run_at),
            "last_status": row.last_status,
            "last_error": row.last_error,
            "next_run_at": cst_iso(row.next_run_at),
            "run_count": int(row.run_count or 0),
            "created_at": cst_iso(row.created_at),
        }

    @staticmethod
    def _run_dict(row: ScheduledTaskRun) -> Dict[str, Any]:
        from tools.cst import cst_iso
        return {
            "id": row.id,
            "task_id": row.task_id,
            "trigger_type": row.trigger_type,
            "status": row.status,
            "started_at": cst_iso(row.started_at),
            "finished_at": cst_iso(row.finished_at),
            "duration_ms": row.duration_ms,
            "new_count": int(row.new_count or 0),
            "updated_count": int(row.updated_count or 0),
            "skipped_count": int(row.skipped_count or 0),
            "attempt_count": int(row.attempt_count or 1),
            "error_message": row.error_message,
            "log_tail": row.log_json or [],
        }


# 全局单例
task_scheduler = TaskScheduler()
