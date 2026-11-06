# -*- coding: utf-8 -*-
"""环境自检（前端启动时的「环境检测」弹窗用）。

为什么重写
----------
拆分部署前这里跑的是 `python main.py --help` —— 单体时代能用，但拆开后：

* `library` 镜像里只有 `requirements-core.txt`，根本没有爬虫的
  playwright / opencv / pyexecjs，跑 `main.py` 必然 ImportError，
  前端于是永远显示「无法连接到 API 服务器」（实际是本地 500，不是连不上）；
* crawler 与 library 是两台容器，判断「环境是否就绪」的含义也不同：
  library 关心「库能不能连、crawler 能不能发现」，crawler 关心「浏览器和
  平台依赖在不在」。

所以改成按服务职责做真实探测，返回结构保持不变（success/message/error/output）。
"""
from __future__ import annotations

import importlib.util
from typing import Any, Dict

from fastapi import APIRouter

import config

router = APIRouter(prefix="/env", tags=["env"])

#: 各服务「必须能 import」的包。缺了就是镜像/依赖装错了。
_COMMON_MODULES = ("fastapi", "uvicorn", "sqlalchemy", "httpx", "pydantic")
_LIBRARY_MODULES = ("asyncpg", "aiofiles", "boto3", "openpyxl", "jieba")
_CRAWLER_MODULES = ("playwright", "cv2", "execjs", "pandas")


def _missing(modules) -> list:
    return [m for m in modules if importlib.util.find_spec(m) is None]


async def _check_db() -> Dict[str, Any]:
    """能不能连上配置库。"""
    try:
        from config.db_config import resolve_config_db_type
        from database.db_session import get_async_engine
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import AsyncSession

        engine = get_async_engine(resolve_config_db_type())
        if engine is None:
            return {"ok": False, "detail": "未配置数据库（engine=None）"}
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await session.execute(text("SELECT 1"))
        return {"ok": True, "detail": f"已连接（{resolve_config_db_type()}）"}
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}


def _check_crawler_service() -> Dict[str, Any]:
    """能否发现 crawler 服务（Nacos 或 CRAWLER_BASE_URL 兜底）。"""
    try:
        from api.services import discovery

        name = getattr(config, "CRAWLER_SERVICE_NAME", "mediacrawler-crawler")
        base = discovery.pick(name)
        if base:
            return {"ok": True, "detail": base}
        return {"ok": False, "detail": f"未发现 {name}（Nacos 不可用且未配置 CRAWLER_BASE_URL）"}
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}


@router.get("/check")
async def check_environment() -> Dict[str, Any]:
    """环境自检，供前端启动检测调用。"""
    role = str(getattr(config, "APP_ROLE", "") or "").lower()
    is_crawler = role in ("crawler", "all")

    required = list(_COMMON_MODULES) + list(
        _CRAWLER_MODULES if is_crawler else _LIBRARY_MODULES)
    missing = _missing(required)
    db = await _check_db()

    checks: Dict[str, Any] = {
        "role": role or "library",
        "dependencies": {"ok": not missing, "missing": missing},
        "database": db,
    }
    if not is_crawler:
        # library 额外关心 crawler 是否可达（网关转发、抓取控制都依赖它）
        crawler = _check_crawler_service()
        checks["crawler_service"] = crawler
    else:
        checks["crawler_service"] = {"ok": True, "detail": "本进程即 crawler"}

    ok = (
        checks["dependencies"]["ok"]
        and db["ok"]
        and checks["crawler_service"]["ok"]
    )

    if ok:
        return {
            "success": True,
            "message": "环境正常：依赖齐全、数据库已连接、crawler 服务可达",
            "output": "；".join(f"{k}: {v.get('detail', '')}" for k, v in checks.items()
                                if isinstance(v, dict) and v.get("detail")),
        }

    problems = []
    if missing:
        problems.append("缺少依赖：" + ", ".join(missing))
    if not db["ok"]:
        problems.append("数据库：" + str(db["detail"]))
    if not checks["crawler_service"]["ok"]:
        problems.append("crawler 服务：" + str(checks["crawler_service"]["detail"]))

    return {
        "success": False,
        "message": "环境检查未通过",
        "error": "；".join(problems) or "未知原因",
        "output": str(checks),
    }
