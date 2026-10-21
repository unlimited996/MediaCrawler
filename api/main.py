# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/api/main.py
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

"""
MediaCrawler WebUI API Server
默认端口 8081（8080 常被本机其它守护进程占用）：
    uvicorn api.main:app --host 127.0.0.1 --port 8081
或：python -m api.main
"""
import asyncio
import os
import sys
import subprocess
from pathlib import Path
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

import config as config_module

from .routers import (crawler_router, data_router, websocket_router, export_router,
                      config_router, story_router, discover_router, accounts_router,
                      scheduler_router, stance_tags_router, s3_backup_router)

# Project root directory (used for running subprocesses like uv run main.py)
PROJECT_ROOT = Path(__file__).parent.parent

app = FastAPI(
    title="MediaCrawler WebUI API",
    description="API for controlling MediaCrawler from WebUI",
    version="1.0.0"
)

# Get webui static files directory
WEBUI_DIR = os.path.join(os.path.dirname(__file__), "webui")


def _ignore_connection_reset(loop, context):
    """忽略客户端断开引发的 ConnectionResetError。

    Windows 的 asyncio Proactor 在客户端直接断开时，会在 _call_connection_lost
    回调里抛 ConnectionResetError(10054)，这不是业务错误，刷屏无意义。
    """
    import asyncio

    exc = context.get("exception")
    if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
        return
    loop.default_exception_handler(context)


@app.on_event("startup")
async def _install_loop_exception_handler():
    import asyncio

    asyncio.get_running_loop().set_exception_handler(_ignore_connection_reset)

    # 预热存储方式探测缓存（含 MySQL 等连不上的库的探测），
    # 避免第一个打开配置页的用户干等探测超时
    from .services.storage_probe import warmup

    asyncio.create_task(warmup())

    # 定时拉取任务调度器：建表 + 恢复调度 + 起循环。
    # 任何失败都不能影响 API 主流程，所以整体 try 包裹。
    try:
        from .services.task_scheduler import task_scheduler

        if config_module.SCHEDULER_ENABLED:
            ok = await task_scheduler.start()
            if not ok:
                print(f"[scheduler] 启动失败：{task_scheduler.last_error}")
        else:
            print("[scheduler] SCHEDULER_ENABLED=0，调度器未启动（手动触发仍可用）")
    except Exception as e:
        print(f"[scheduler] 启动异常（不影响 API）：{e}")

    # 评论情感/立场回填：老数据入库时还没有这些判定，后台补一次，
    # 让评论区的筛选/赞同度统计对所有历史数据立即可用
    try:
        from tools.v2_sync import (backfill_comment_sentiments,
                                   backfill_comment_stances)

        async def _backfill_bg():
            try:
                n1 = await backfill_comment_sentiments()
                n2 = await backfill_comment_stances()
                if n1 or n2:
                    print(f"[stance] 启动回填完成：情感 {n1} 条，立场 {n2} 条")
            except Exception as e:
                print(f"[stance] 启动回填失败（不影响使用，查询时会按需补齐）：{e}")

        asyncio.create_task(_backfill_bg())
    except Exception as e:
        print(f"[stance] 回填任务启动失败（不影响 API）：{e}")

    # 态度标签库（P3）：库空时写入 9 个内置标签，并预热进程内缓存
    try:
        from tools.stance import ensure_seeded, load_library

        async def _stance_lib_boot():
            try:
                n = await ensure_seeded()
                lib = await load_library(force=True)
                print(f"[stance] 标签库就绪：{n} 个标签，version={lib.get('version')}")
            except Exception as e:
                print(f"[stance] 标签库初始化失败（判定时会重试）：{e}")

        asyncio.create_task(_stance_lib_boot())
    except Exception as e:
        print(f"[stance] 标签库启动失败（不影响 API）：{e}")

    # S3 媒体备份（P4）：确保表存在 + 种子配置，然后起定时备份循环。
    # 循环每 60s 扫一批到期任务（pending/重试到期），锁互斥不影响 API 主流程。
    try:
        from tools.media_backup import (ensure_seed_config,
                                         enqueue_missing, run_backup_batch)

        async def _s3_backup_boot():
            try:
                from config.db_config import resolve_config_db_type
                from database.db_session import get_async_engine
                from sqlalchemy.ext.asyncio import AsyncSession

                engine = get_async_engine(resolve_config_db_type())
                if engine is None:
                    print("[s3-backup] 非数据库存储，备份功能未启用")
                    return
                try:
                    async with AsyncSession(engine, expire_on_commit=False) as s:
                        seeded = await ensure_seed_config(s)
                    if seeded:
                        print("[s3-backup] 已写入种子配置 garage")
                finally:
                    await engine.dispose()
            except Exception as e:
                print(f"[s3-backup] 初始化失败（不影响 API）：{e}")

            # 定时循环：先补历史入队，再按批跑；无启用配置时静默跳过
            while True:
                try:
                    await enqueue_missing(limit=200)
                    await run_backup_batch()
                except Exception as e:
                    print(f"[s3-backup] 备份循环异常（继续运行）：{e}")
                await asyncio.sleep(60)

        asyncio.create_task(_s3_backup_boot())
    except Exception as e:
        print(f"[s3-backup] 启动失败（不影响 API）：{e}")

# CORS configuration - allow frontend dev server access
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",  # Vite dev server
        "http://localhost:3000",  # Backup port
        "http://127.0.0.1:5173",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(crawler_router, prefix="/api")
app.include_router(data_router, prefix="/api")
app.include_router(websocket_router, prefix="/api")
app.include_router(export_router, prefix="/api")
app.include_router(config_router, prefix="/api")
app.include_router(story_router, prefix="/api")
app.include_router(discover_router, prefix="/api")
app.include_router(accounts_router, prefix="/api")
app.include_router(scheduler_router, prefix="/api")
app.include_router(stance_tags_router, prefix="/api")
app.include_router(s3_backup_router, prefix="/api")


@app.api_route("/", methods=["GET", "HEAD"])
async def serve_frontend():
    """Return frontend page（同时支持 HEAD，便于健康检查/预览探测）"""
    index_path = os.path.join(WEBUI_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {
        "message": "MediaCrawler WebUI API",
        "version": "1.0.0",
        "docs": "/docs",
        "note": "WebUI not found, please build it first: cd webui && npm run build"
    }


@app.get("/api/health")
async def health_check():
    return {"status": "ok"}


@app.get("/api/env/check")
async def check_environment():
    """Check if MediaCrawler environment is configured correctly"""
    try:
        # Run main.py --help with the current interpreter to check environment.
        # 用当前 Python 解释器（通常是项目 venv），不依赖 uv：
        # `uv run` 会重新解析/同步整个依赖，在无 uv 缓存的环境里会卡满超时。
        # Use PROJECT_ROOT so it works regardless of where uvicorn was started
        if sys.platform == "win32":
            loop = asyncio.get_running_loop()
            process = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    [sys.executable, "main.py", "--help"],
                    capture_output=True,
                    timeout=30.0,
                    cwd=str(PROJECT_ROOT)
                )
            )
            stdout, stderr = process.stdout, process.stderr  # bytes
        else:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "main.py", "--help",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(PROJECT_ROOT)  # Project root directory
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=30.0  # 30 seconds timeout
            )
        if process.returncode == 0:
            return {
                "success": True,
                "message": "MediaCrawler environment configured correctly",
                "output": stdout.decode("utf-8", errors="ignore")[:500]  # Truncate to first 500 characters
            }
        else:
            error_msg = stderr.decode("utf-8", errors="ignore") or stdout.decode("utf-8", errors="ignore")
            return {
                "success": False,
                "message": "Environment check failed",
                "error": error_msg[:500]
            }
    except asyncio.TimeoutError:
        return {
            "success": False,
            "message": "Environment check timeout",
            "error": "Command execution exceeded 30 seconds"
        }
    except FileNotFoundError:
        return {
            "success": False,
            "message": "python interpreter not found",
            "error": "Please ensure the Python interpreter used by the API server is available"
        }
    except Exception as e:
        return {
            "success": False,
            "message": "Environment check error",
            "error": f"{type(e).__name__}: {str(e) or 'Unknown'}"
        }


@app.get("/api/config/platforms")
async def get_platforms():
    """Get list of supported platforms"""
    return {
        "platforms": [
            {"value": "xhs", "label": "Xiaohongshu", "icon": "book-open"},
            {"value": "dy", "label": "Douyin", "icon": "music"},
            {"value": "ks", "label": "Kuaishou", "icon": "video"},
            {"value": "bili", "label": "Bilibili", "icon": "tv"},
            {"value": "wb", "label": "Weibo", "icon": "message-circle"},
            {"value": "tieba", "label": "Baidu Tieba", "icon": "messages-square"},
            {"value": "zhihu", "label": "Zhihu", "icon": "help-circle"},
        ]
    }


@app.get("/api/config/options")
async def get_config_options():
    """Get all configuration options

    save_options 按"此刻是否真的能用"动态探测（见 services/storage_probe）：
    没配或连不上的数据库不出现，避免用户选了就报错。
    """
    from .services.storage_probe import available_save_options

    try:
        storage = await available_save_options()
    except Exception as e:
        # 探测本身失败时退化为"仅文件类"，保证配置页始终可用
        storage = {"items": [
            {"value": v, "label": l} for v, l in [
                ("jsonl", "JSONL File"), ("json", "JSON File"),
                ("csv", "CSV File"), ("excel", "Excel File"),
            ]
        ], "hidden": []}

    return {
        "login_types": [
            {"value": "qrcode", "label": "QR Code Login"},
            {"value": "cookie", "label": "Cookie Login"},
        ],
        "crawler_types": [
            {"value": "search", "label": "Search Mode"},
            {"value": "detail", "label": "Detail Mode"},
            {"value": "creator", "label": "Creator Mode"},
        ],
        "save_options": storage["items"],
        "save_options_hidden": storage["hidden"],
    }


# Mount static resources - must be placed after all routes
if os.path.exists(WEBUI_DIR):
    assets_dir = os.path.join(WEBUI_DIR, "assets")
    if os.path.exists(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")
    # Mount logos directory
    logos_dir = os.path.join(WEBUI_DIR, "logos")
    if os.path.exists(logos_dir):
        app.mount("/logos", StaticFiles(directory=logos_dir), name="logos")
    # 通用挂载：webui 下其余一级目录（emoji/ 等前端静态资源）自动挂到同名路径。
    # 避免每加一个 public 子目录都要回来改代码。
    _reserved = {"assets", "logos", "index.html"}
    for _entry in sorted(os.listdir(WEBUI_DIR)):
        _full = os.path.join(WEBUI_DIR, _entry)
        if _entry in _reserved or not os.path.isdir(_full):
            continue
        if any(r.path == f"/{_entry}" for r in app.routes):
            continue
        app.mount(f"/{_entry}", StaticFiles(directory=_full), name=f"webui-{_entry}")
    # Mount other static files (e.g., vite.svg)
    app.mount("/static", StaticFiles(directory=WEBUI_DIR), name="webui-static")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
