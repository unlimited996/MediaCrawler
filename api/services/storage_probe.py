# -*- coding: utf-8 -*-
"""存储方式可用性探测。

`/api/config/options` 原来把 save_options 写死成 7 个值，带来两个问题：
1. 漏了 postgres —— 用户在 .env 配好了远程 PostgreSQL，下拉里根本选不到；
2. 无条件暴露 sqlite/mysql/mongodb —— 没配的服务选了就报错，
   而 mongodb 在本项目里压根没有 store 实现。

现在按"此刻是否真的能用"动态探测：文件类与 sqlite 恒可用，
MySQL/PostgreSQL 做真实连通性探测（带超时与短时缓存）。
"""
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional

# 探测结果缓存：数据库可用性不会频繁变动，5 分钟足够。
# 太短会导致"每 N 秒就有一个请求白等探测超时"（MySQL 被防火墙丢弃时要等满超时）
_CACHE_TTL = timedelta(seconds=300)
_cache: Dict[str, object] = {"ts": None, "items": None}

# 全部候选存储方式（value → 标签）。顺序即下拉展示顺序。
ALL_SAVE_OPTIONS: List[Dict[str, str]] = [
    {"value": "jsonl", "label": "JSONL File"},
    {"value": "json", "label": "JSON File"},
    {"value": "csv", "label": "CSV File"},
    {"value": "excel", "label": "Excel File"},
    {"value": "sqlite", "label": "SQLite Database"},
    {"value": "postgres", "label": "PostgreSQL Database"},
    {"value": "db", "label": "MySQL Database"},
]

# 恒可用的：文件类 + sqlite（本地文件库，无需任何配置）
ALWAYS_AVAILABLE = {"jsonl", "json", "csv", "excel", "sqlite"}

# 需要探测连通性的数据库类
PROBE_DB_TYPES = {"db": "mysql", "postgres": "postgres"}

# 单项探测超时（秒）：连不上的库通常是 connection refused，毫秒级返回；
# 2 秒足够覆盖"网络可达但无响应"的极端情况，也不至于让配置页等太久
PROBE_TIMEOUT = 2.0


async def warmup() -> None:
    """应用启动时预热探测缓存，避免首个打开配置页的用户等待。"""
    try:
        await available_save_options(force=True)
    except Exception:
        pass


async def _probe_db(db_type: str) -> bool:
    """真实连一次数据库（复用爬虫启动前的同一套探测逻辑）。"""
    from database.db_session import get_async_engine
    from sqlalchemy import text

    engine = get_async_engine(db_type)
    if engine is None:
        return False
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        try:
            await engine.dispose()
        except Exception:
            pass


async def _probe_one(value: str) -> bool:
    if value in ALWAYS_AVAILABLE:
        return True
    db_type = PROBE_DB_TYPES.get(value)
    if not db_type:
        return False
    try:
        return await asyncio.wait_for(_probe_db(db_type), timeout=PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        return False
    except Exception:
        return False


async def available_save_options(force: bool = False) -> Dict[str, object]:
    """返回当前可用的存储方式（带 30 秒缓存）。

    Returns:
        {"items": [...可用项...], "hidden": [...被隐藏项及原因...]}
    """
    now = datetime.now()
    if (not force and _cache["items"] is not None
            and _cache["ts"] is not None
            and now - _cache["ts"] < _CACHE_TTL):   # type: ignore[operator]
        return _cache["items"]   # type: ignore[return-value]

    values = [o["value"] for o in ALL_SAVE_OPTIONS]
    results = await asyncio.gather(*[_probe_one(v) for v in values])

    items, hidden = [], []
    for opt, ok in zip(ALL_SAVE_OPTIONS, results):
        if ok:
            items.append(dict(opt))
        else:
            hidden.append({**opt, "reason": "未配置或当前不可达"})

    payload = {"items": items, "hidden": hidden}
    _cache["ts"] = now
    _cache["items"] = payload
    return payload


def invalidate_cache() -> None:
    """配置变更后手动失效缓存（如 .env 改动）。"""
    _cache["ts"] = None
    _cache["items"] = None
