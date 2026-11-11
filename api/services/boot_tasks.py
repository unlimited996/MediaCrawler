# -*- coding: utf-8 -*-
"""应用启动期的后台引导任务（三个应用共用）。

拆分成 crawler / data 两个独立服务后，各自需要启动的后台任务不一样：

    crawler 服务：抓取执行器（worker）/ 调度器
    data    服务：立场回填、标签库、S3 备份循环、菜谱建表
    单体 main.py ：两者全部（本地开发用）

把这些逻辑集中在这里，避免三份实现漂移。
**所有函数都必须「失败不影响主流程」**：服务起不来的代价远大于某个后台任务缺席。
"""
from __future__ import annotations

import asyncio

import config as config_module


def _log(tag: str, msg: str) -> None:
    print(f"[{tag}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------

def install_loop_exception_handler() -> None:
    """忽略客户端断开引发的 ConnectionResetError（Windows asyncio 会刷屏）。"""

    def _ignore_connection_reset(loop, context):
        exc = context.get("exception")
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        loop.default_exception_handler(context)

    try:
        asyncio.get_running_loop().set_exception_handler(_ignore_connection_reset)
    except Exception as e:
        _log("boot", f"安装事件循环异常处理器失败：{e}")


def start_storage_warmup() -> None:
    """预热存储方式探测缓存，避免第一个打开配置页的用户干等超时。"""
    try:
        from .storage_probe import warmup

        asyncio.create_task(warmup())
    except Exception as e:
        _log("boot", f"存储探测预热失败（不影响 API）：{e}")


# ---------------------------------------------------------------------------
# crawler 服务
# ---------------------------------------------------------------------------

def start_crawler_runtime() -> None:
    """启动抓取运行时：默认由独立 worker 承担，`SCHEDULER_IN_API=1` 时在进程内跑。"""
    try:
        if not config_module.SCHEDULER_ENABLED:
            _log("scheduler", "SCHEDULER_ENABLED=0，执行器未启动"
                              "（手动触发会排队，但没人执行）")
            return
        asyncio.create_task(_start_crawler_runtime_async())
    except Exception as e:
        _log("scheduler", f"启动异常（不影响 API）：{e}")


async def _start_crawler_runtime_async() -> None:
    try:
        if getattr(config_module, "SCHEDULER_IN_API", False):
            from .task_scheduler import task_scheduler

            ok = await task_scheduler.start()
            if ok:
                _log("scheduler", "调度器运行在本进程内（SCHEDULER_IN_API=1）")
            else:
                _log("scheduler", f"启动失败：{task_scheduler.last_error}")
            return

        from .worker_control import ensure_worker, worker_info

        action = ensure_worker(auto_spawn=bool(
            getattr(config_module, "WORKER_AUTO_SPAWN", True)))
        info = worker_info()
        if action == "absent":
            _log("worker", "抓取执行器未运行 → 手动触发会排队等它上线。"
                           "启动方式：python -m worker")
        else:
            _log("worker", f"抓取执行器{'已在运行' if action == 'running' else '已拉起'}"
                           f"（pid={info.get('pid')}），抓取与接口已解耦")
    except Exception as e:
        _log("scheduler", f"启动异常（不影响 API）：{e}")


# ---------------------------------------------------------------------------
# data 服务
# ---------------------------------------------------------------------------

def start_stance_backfill() -> None:
    """评论情感/立场回填：老数据入库时没这些判定，启动补一次。"""
    try:
        from tools.v2_sync import (backfill_comment_sentiments,
                                   backfill_comment_stances)

        async def _bg():
            try:
                n1 = await backfill_comment_sentiments()
                n2 = await backfill_comment_stances()
                if n1 or n2:
                    _log("stance", f"启动回填完成：情感 {n1} 条，立场 {n2} 条")
            except Exception as e:
                _log("stance", f"启动回填失败（不影响使用，查询时按需补齐）：{e}")

        asyncio.create_task(_bg())
    except Exception as e:
        _log("stance", f"回填任务启动失败（不影响 API）：{e}")


def start_stance_library() -> None:
    """态度标签库：库空时写入内置标签并预热进程内缓存。"""
    try:
        from tools.stance import ensure_seeded, load_library

        async def _bg():
            try:
                n = await ensure_seeded()
                lib = await load_library(force=True)
                _log("stance", f"标签库就绪：{n} 个标签，version={lib.get('version')}")
            except Exception as e:
                _log("stance", f"标签库初始化失败（判定时会重试）：{e}")

        asyncio.create_task(_bg())
    except Exception as e:
        _log("stance", f"标签库启动失败（不影响 API）：{e}")


def start_emoji_library() -> None:
    """表情包库：幂等建表（历史库无需手工跑 init_v2）。"""
    async def _bg():
        try:
            from database.models_v2 import BaseV2, EmojiAsset
            from config.db_config import resolve_config_db_type
            from database.db_session import get_async_engine

            engine = get_async_engine(resolve_config_db_type())
            if engine is None:
                return
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(
                        lambda sync_conn: EmojiAsset.__table__.create(
                            bind=sync_conn, checkfirst=True))
                _log("emoji", "表情包库表已就绪")
            finally:
                await engine.dispose()
        except Exception as e:
            _log("emoji", f"表情包库建表失败（不影响 API）：{e}")

    asyncio.create_task(_bg())


def start_s3_backup() -> None:
    """S3 媒体备份：建表 + 种子配置，然后起 60s 循环。"""
    try:
        from tools.media_backup import (ensure_seed_config, enqueue_missing,
                                        retry_orphaned_purges, run_backup_batch)

        async def _bg():
            try:
                from config.db_config import resolve_config_db_type
                from database.db_session import get_async_engine
                from sqlalchemy.ext.asyncio import AsyncSession

                engine = get_async_engine(resolve_config_db_type())
                if engine is None:
                    _log("s3-backup", "非数据库存储，备份功能未启用")
                    return
                try:
                    async with AsyncSession(engine, expire_on_commit=False) as s:
                        seeded = await ensure_seed_config(s)
                    if seeded:
                        _log("s3-backup", "已写入种子配置 garage")
                finally:
                    await engine.dispose()
            except Exception as e:
                _log("s3-backup", f"初始化失败（不影响 API）：{e}")

            while True:
                try:
                    # 入队量必须小于每批上传量，否则 pending 只涨不消
                    # （原来 200 入队 vs 20 上传，队列 10:1 爆炸）
                    await enqueue_missing(limit=60)
                    await run_backup_batch()
                    await retry_orphaned_purges(limit=50)
                except Exception as e:
                    _log("s3-backup", f"备份循环异常（继续运行）：{e}")
                await asyncio.sleep(60)

        asyncio.create_task(_bg())
    except Exception as e:
        _log("s3-backup", f"启动失败（不影响 API）：{e}")


def start_recipe_boot() -> None:
    """菜谱域：建表幂等 + 打印库内统计。"""
    try:
        from sqlalchemy.ext.asyncio import AsyncSession

        from config.db_config import resolve_config_db_type
        from database.db_session import get_async_engine
        from tools.recipe_import import ensure_schema, verify

        async def _bg():
            engine = get_async_engine(resolve_config_db_type())
            if engine is None:
                _log("recipe", "非数据库存储，菜谱接口不可用")
                return
            try:
                sch = await ensure_schema(engine)
                async with AsyncSession(engine, expire_on_commit=False) as s:
                    rep = await verify(s)
                c = rep["counts"]
                _log("recipe", f"表就绪 {sch['tables']} 张：{c['recipes']} 菜谱 / "
                               f"{c['ingredients']} 配料 / {c['steps']} 步骤 / "
                               f"{c['tagLinks']} 标签关联")
            except Exception as e:
                _log("recipe", f"初始化失败（不影响其它 API）：{e}")
            finally:
                await engine.dispose()

        asyncio.create_task(_bg())
    except Exception as e:
        _log("recipe", f"启动任务创建失败（不影响 API）：{e}")
