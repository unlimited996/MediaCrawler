# -*- coding: utf-8 -*-
"""环境自检（跨平台，方案 #4：完整 Linux 环境支持）。

检查项：Python 版本 / 依赖 / Node 运行时 / Chromium / ffmpeg / 数据库连接。
用法：
    python scripts/check_env.py
"""
from __future__ import annotations

import platform
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

OK, WARN, FAIL = "[ OK ]", "[WARN]", "[FAIL]"


def _print(status: str, name: str, detail: str = "") -> None:
    print(f"{status} {name:<22} {detail}")


def check_python() -> None:
    v = sys.version_info
    ok = v >= (3, 11)
    _print(OK if ok else FAIL, "Python", f"{v.major}.{v.minor}.{v.micro} "
                                         f"(需要 >=3.11) platform={platform.system()}")


def check_deps() -> None:
    required = ["httpx", "playwright", "sqlalchemy", "asyncpg", "websockets",
                "xhshow", "pydantic", "fastapi", "uvicorn"]
    optional = ["execjs", "pandas", "openpyxl", "wordcloud", "matplotlib"]
    for mod in required:
        try:
            __import__(mod)
            _print(OK, f"dep:{mod}")
        except ImportError:
            _print(FAIL, f"dep:{mod}", "缺失，pip install -r requirements.txt")
    for mod in optional:
        try:
            __import__(mod)
            _print(OK, f"opt:{mod}")
        except ImportError:
            _print(WARN, f"opt:{mod}", "可选依赖未安装")


def check_node() -> None:
    node = shutil.which("node")
    if not node:
        _print(WARN, "Node.js", "未找到，抖音/知乎的 JS 签名将不可用")
        return
    try:
        import execjs

        runtime = execjs.get()
        _print(OK, "Node.js", f"{node} runtime={runtime.name}")
    except Exception as e:
        _print(WARN, "Node.js", f"{node} 但 execjs 不可用：{type(e).__name__}")


def check_chromium() -> None:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            exe = p.chromium.executable_path
            exists = Path(exe).exists()
            _print(OK if exists else FAIL, "Chromium",
                   f"{exe}" if exists else f"{exe} 不存在，请执行 playwright install chromium")
    except ImportError:
        _print(WARN, "Chromium", "playwright 未安装")
    except Exception as e:
        _print(WARN, "Chromium", f"检查失败：{type(e).__name__}: {e}")


def check_ffmpeg() -> None:
    exe = shutil.which("ffmpeg")
    if exe:
        _print(OK, "ffmpeg", exe)
    else:
        _print(WARN, "ffmpeg", "未找到，视频下载/合成功能不可用")


def check_libs() -> None:
    for name in ("douyin.js", "zhihu.js", "stealth.min.js"):
        path = PROJECT_ROOT / "libs" / name
        _print(OK if path.exists() else FAIL, f"libs/{name}",
               str(path) if path.exists() else "缺失")


def check_db() -> None:
    try:
        import asyncio

        from config.db_config import resolve_config_db_type
        from database.db_session import get_async_engine
        from sqlalchemy import text

        db_type = resolve_config_db_type()

        async def go():
            engine = get_async_engine(db_type)
            if engine is None:
                _print(WARN, "Database", f"当前存储方式 {db_type} 不是数据库")
                return
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            await engine.dispose()

        asyncio.run(go())
        _print(OK, "Database", f"连接正常 ({db_type})")
    except Exception as e:
        _print(FAIL, "Database", f"{type(e).__name__}: {str(e)[:120]}")


def main() -> None:
    print(f"=== MediaCrawler 环境自检 ({platform.system()} {platform.release()}) ===")
    check_python()
    check_deps()
    check_node()
    check_chromium()
    check_ffmpeg()
    check_libs()
    check_db()
    print("=== 完成 ===")


if __name__ == "__main__":
    main()
