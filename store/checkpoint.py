# -*- coding: utf-8 -*-
"""断点续爬：把每个翻页单元的进度落到 crawl_checkpoint 表。

设计要点：
1. task_code 由抓取参数哈希生成 —— **相同参数重跑就是续跑**，不需要用户记 ID
2. scope_type + scope_key + page_no 唯一确定一个翻页单元
3. 抓完即标记 done，中断后重跑只补 pending/error 的单元
4. 默认关闭（配置 crawl.enable_checkpoint），开启后行为才改变
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models_v2 import CrawlCheckpoint, CrawlTask


def make_task_code(platform: str, crawler_type: str, params: Any) -> str:
    """同一套抓取参数 → 稳定的 task_code（重跑即续跑）。"""
    if not isinstance(params, str):
        params = json.dumps(params, ensure_ascii=False, sort_keys=True)
    raw = f"{platform}:{crawler_type}:{params}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def _engine_session():
    from config.db_config import resolve_config_db_type
    from database.db_session import get_async_engine

    engine = get_async_engine(resolve_config_db_type())
    if engine is None:
        return None, None
    return engine, AsyncSession(engine, expire_on_commit=False)


async def ensure_task(task_code: str, platform: str, crawl_type: str,
                      params: Dict[str, Any], config_snapshot: Optional[Dict] = None) -> Optional[int]:
    """创建或复用一个抓取任务，返回 task_id。"""
    engine, session = _engine_session()
    if session is None:
        return None
    try:
        from database.models_v2 import SourceSite

        row = (await session.execute(
            select(CrawlTask).where(CrawlTask.task_code == task_code)
        )).scalar_one_or_none()
        if row:
            return row.id

        site_id = None
        site = (await session.execute(
            select(SourceSite).where(SourceSite.code == platform)
        )).scalar_one_or_none()
        if site:
            site_id = site.id

        row = CrawlTask(
            task_code=task_code,
            source_site_id=site_id,
            crawl_type=crawl_type,
            params_json=params,
            config_snapshot=config_snapshot or {},
            status="running",
            started_at=datetime.now(),
        )
        session.add(row)
        await session.commit()
        return row.id
    finally:
        await session.close()
        if engine:
            await engine.dispose()


async def is_done(task_id: int, scope_type: str, scope_key: str, page_no: int) -> bool:
    """该翻页单元是否已抓过（用于跳过）。"""
    engine, session = _engine_session()
    if session is None:
        return False
    try:
        row = (await session.execute(
            select(CrawlCheckpoint).where(
                CrawlCheckpoint.task_id == task_id,
                CrawlCheckpoint.scope_type == scope_type,
                CrawlCheckpoint.scope_key == scope_key,
                CrawlCheckpoint.page_no == page_no,
            )
        )).scalar_one_or_none()
        return bool(row and row.state == "done")
    finally:
        await session.close()
        if engine:
            await engine.dispose()


async def get_pending(task_id: int, scope_type: Optional[str] = None) -> List[Dict[str, Any]]:
    """列出未完成（pending/error）的翻页单元。"""
    engine, session = _engine_session()
    if session is None:
        return []
    try:
        q = select(CrawlCheckpoint).where(
            CrawlCheckpoint.task_id == task_id,
            CrawlCheckpoint.state.in_(["pending", "error"]),
        )
        if scope_type:
            q = q.where(CrawlCheckpoint.scope_type == scope_type)
        rows = (await session.execute(q.order_by(CrawlCheckpoint.page_no))).scalars().all()
        return [{"scope_type": r.scope_type, "scope_key": r.scope_key,
                 "page_no": r.page_no, "state": r.state,
                 "attempt_count": r.attempt_count} for r in rows]
    finally:
        await session.close()
        if engine:
            await engine.dispose()


async def mark(task_id: int, scope_type: str, scope_key: str, page_no: int,
               state: str, fetched_count: int = 0, declared_count: Optional[int] = None,
               cursor: Optional[str] = None, payload: Optional[Dict] = None,
               error: Optional[str] = None) -> None:
    """写入/更新一个翻页单元的状态（done / error / running）。"""
    engine, session = _engine_session()
    if session is None:
        return
    try:
        row = (await session.execute(
            select(CrawlCheckpoint).where(
                CrawlCheckpoint.task_id == task_id,
                CrawlCheckpoint.scope_type == scope_type,
                CrawlCheckpoint.scope_key == scope_key,
                CrawlCheckpoint.page_no == page_no,
            )
        )).scalar_one_or_none()

        if row is None:
            row = CrawlCheckpoint(task_id=task_id, scope_type=scope_type,
                                  scope_key=scope_key, page_no=page_no)
            session.add(row)

        row.state = state
        row.cursor = cursor
        row.fetched_count = fetched_count
        if declared_count is not None:
            row.declared_count = declared_count
        if payload is not None:
            row.payload_json = payload
        if state == "error":
            row.last_error = (error or "")[:500]
            row.attempt_count = int(row.attempt_count or 0) + 1
        await session.commit()
    finally:
        await session.close()
        if engine:
            await engine.dispose()


async def mark_done(task_id: int, scope_type: str, scope_key: str, page_no: int,
                    fetched_count: int = 0, declared_count: Optional[int] = None,
                    cursor: Optional[str] = None, payload: Optional[Dict] = None) -> None:
    await mark(task_id, scope_type, scope_key, page_no, "done",
               fetched_count=fetched_count, declared_count=declared_count,
               cursor=cursor, payload=payload)


async def mark_error(task_id: int, scope_type: str, scope_key: str, page_no: int,
                     error: str) -> None:
    await mark(task_id, scope_type, scope_key, page_no, "error", error=error)


async def summary(task_id: int) -> Dict[str, int]:
    """任务进度汇总，便于前端/日志展示。"""
    engine, session = _engine_session()
    if session is None:
        return {}
    try:
        rows = (await session.execute(
            select(CrawlCheckpoint.state, func.count(CrawlCheckpoint.id))
            .where(CrawlCheckpoint.task_id == task_id)
            .group_by(CrawlCheckpoint.state)
        )).all()
        return {state: cnt for state, cnt in rows}
    finally:
        await session.close()
        if engine:
            await engine.dispose()


async def finish_task(task_id: int, status: str = "success",
                      error_message: Optional[str] = None) -> None:
    engine, session = _engine_session()
    if session is None:
        return
    try:
        row = (await session.execute(
            select(CrawlTask).where(CrawlTask.id == task_id)
        )).scalar_one_or_none()
        if row:
            row.status = status
            row.finished_at = datetime.now()
            row.error_message = error_message
            await session.commit()
    finally:
        await session.close()
        if engine:
            await engine.dispose()
