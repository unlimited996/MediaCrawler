# -*- coding: utf-8 -*-
"""WebUI 里的「原版登录页」会话。

不用 Xvfb / noVNC（要装系统包、加端口、镜像变大），改用更轻的方式：
容器内启动一个 headless persistent context 打开小红书原版登录页，
定时截图推给前端，前端把点击坐标 / 文本回传，后端用 Playwright 执行。

**登录态自动落盘**：user_data_dir 与抓取任务 `launch_browser()` 用的是
同一个目录（browser_data/<platform>_user_data_dir，挂在 mc-browser-data 卷上），
Chromium 关闭时自己把 Cookie 写进去。之后定时任务再用 persistent context
启动就是已登录状态，不需要导出 Cookie、不需要改配置、不需要重启容器。

互斥：会话期间写 logs/.login_session_active 标记，防止和抓取任务同时打开
同一个 Chromium profile（会报 profile locked / 损坏登录态）。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

import config
from tools.crawler_util import chromium_launch_kwargs, system_playwright_proxy

router = APIRouter(prefix="/crawler/login-session", tags=["login-session"])

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOCK_FILE = PROJECT_ROOT / "logs" / ".login_session_active"
# 与 CrawlerManager.get_qrcode() 的回退日志是同一个文件；把页面上的二维码
# 原图写进去，右下角小弹窗就能显示清晰可扫的原图（截图里的二维码太小）。
LIVE_LOG = PROJECT_ROOT / "logs" / "scheduler_live.jsonl"
# 必须与 XiaoHongShuCrawler.launch_browser 完全一致，否则同一个
# browser_data profile 两种指纹交替出现，会被小红书判「网络存在风险」。
VIEWPORT = {"width": 1920, "height": 1080}
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0.0.0 Safari/537.36")
STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--exclude-switches=enable-automation",
    "--disable-infobars",
]
# 正常 Chrome 会带这些请求头；headless shell 默认不发，容易触发风控
EXTRA_HEADERS = {
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
}
INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN','zh','en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
window.chrome = window.chrome || { runtime: {} };
"""
IDLE_TIMEOUT_SEC = 15 * 60


def _profile_dir() -> Path:
    """与 media_platform/xhs/core.py::launch_browser 完全一致的路径。"""
    return Path(os.getcwd()) / "browser_data" / (config.USER_DATA_DIR % config.PLATFORM)


class _LoginSession:
    def __init__(self) -> None:
        self.playwright = None
        self.context = None
        self.page = None
        self.last_active = 0.0
        self._last_qr: Optional[str] = None
        self.lock = asyncio.Lock()
        self.started_at: Optional[str] = None
        # 登录成功 / 关闭会话时只发一次「二维码已清除」哨兵，避免 FAB 一直显示过期二维码
        self._clear_emitted = False

    @property
    def active(self) -> bool:
        return self.context is not None

    def touch(self) -> None:
        self.last_active = time.monotonic()

    async def logged_in(self) -> bool:
        if not self.context:
            return False
        try:
            cookies = await self.context.cookies()
        except Exception:
            return False
        return any(
            c.get("name") == "web_session"
            and ("xiaohongshu" in (c.get("domain") or "")
                 or "rednote" in (c.get("domain") or ""))
            for c in cookies
        )

    async def _expire_if_idle(self) -> None:
        if self.active and time.monotonic() - self.last_active > IDLE_TIMEOUT_SEC:
            await self.stop()

    async def start(self) -> None:
        async with self.lock:
            if self.active:
                self.touch()
                return
            # 新会话开始：重置清除标记，并先发一次清除哨兵，
            # 避免上一次异常退出残留的二维码还挂在 FAB 上
            self._clear_emitted = False
            self._emit_qrcode_clear()
            # 已有抓取在跑时不能抢同一个 profile
            running = await _running_task_count()
            if running:
                raise RuntimeError(
                    f"当前有 {running} 个抓取任务在执行，请先等它结束再打开登录页"
                    "（两个浏览器不能共用同一个 browser_data profile）")
            if LOCK_FILE.exists():
                age = time.time() - LOCK_FILE.stat().st_mtime
                if age < IDLE_TIMEOUT_SEC:
                    raise RuntimeError("登录页会话标记仍有效，可能另一个会话未正常关闭")
                LOCK_FILE.unlink(missing_ok=True)

            from playwright.async_api import async_playwright

            pw = await async_playwright().start()
            try:
                kwargs = chromium_launch_kwargs(pw.chromium)
                kwargs.setdefault("args", [])
                kwargs["args"] = list(kwargs["args"]) + [
                    a for a in STEALTH_ARGS if a not in kwargs["args"]]
                profile = _profile_dir()
                profile.mkdir(parents=True, exist_ok=True)
                ctx = await pw.chromium.launch_persistent_context(
                    user_data_dir=str(profile),
                    accept_downloads=True,
                    headless=True,
                    proxy=system_playwright_proxy(),
                    viewport=VIEWPORT,
                    user_agent=USER_AGENT,
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    extra_http_headers=EXTRA_HEADERS,
                    **kwargs,
                )
                await ctx.add_init_script(INIT_SCRIPT)
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                self.playwright, self.context, self.page = pw, ctx, page
                self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
                LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
                LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
                self.touch()
                # 导航放后台，别让 start 卡到前端 axios 的 30s 超时；
                # 页面没加载出来时用户可点「刷新页面」重试。
                asyncio.create_task(self._navigate(page))
            except Exception:
                await pw.stop()
                self.playwright = self.context = self.page = None
                LOCK_FILE.unlink(missing_ok=True)
                raise

    async def stop(self) -> None:
        async with self.lock:
            ctx, pw = self.context, self.playwright
            self.context = self.page = self.playwright = None
            self.started_at = None
            self._last_qr = None
            LOCK_FILE.unlink(missing_ok=True)
            # 关闭会话：清除二维码（登录页关闭后不应再弹）
            self._emit_qrcode_clear()
            if ctx:
                try:
                    await ctx.close()   # Chromium 在此把 Cookie 刷进 user_data_dir
                except Exception:
                    pass
            if pw:
                try:
                    await pw.stop()
                except Exception:
                    pass

    @staticmethod
    async def _navigate(page) -> None:
        url = ("https://www.rednote.com" if config.XHS_INTERNATIONAL
               else "https://www.xiaohongshu.com")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        except Exception as e:
            print(f"[login-session] 打开 {url} 失败（可点刷新页面重试）: "
                  f"{type(e).__name__}: {e}", flush=True)

    async def _publish_qrcode(self) -> None:
        """原版登录页里的二维码是 img.qrcode-img 的 base64。

        截图缩放后不好扫，把它原样写进共享实时日志，
        CrawlerManager.get_qrcode() 会读出来给右下角小弹窗显示原图。
        内容不变就只写一次，避免刷屏。
        """
        if not self.page:
            return
        try:
            loc = self.page.locator("img.qrcode-img").first
            if await loc.count() == 0:
                return
            src = await loc.get_attribute("src")
            if not src or not src.startswith("data:image") or src == self._last_qr:
                return
            self._last_qr = src
            import json

            line = json.dumps({
                "ts": time.strftime("%H:%M:%S"),
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "level": "info",
                "msg": "[QRCODE]" + src,
            }, ensure_ascii=False)
            LIVE_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(LIVE_LOG, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass

    async def screenshot(self) -> bytes:
        if not self.page:
            raise RuntimeError("会话未启动")
        self.touch()
        await self._publish_qrcode()
        # 已登录：页面不再有二维码，发一次清除哨兵让 FAB 收起
        if await self.logged_in() and not self._clear_emitted:
            self._emit_qrcode_clear()
            self._clear_emitted = True
        return await self.page.screenshot(type="jpeg", quality=60)

    def _emit_qrcode_clear(self) -> None:
        """写一条 [QRCODE_CLEAR] 哨兵到共享实时日志。

        crawler_manager.get_qrcode() 看到比二维码更新的清除哨兵就返回 None，
        让右下角扫码浮窗收起，避免「明明已登录、刷新后二维码还一直弹」。
        """
        try:
            line = json.dumps({
                "ts": time.strftime("%H:%M:%S"),
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "level": "info",
                "msg": "[QRCODE_CLEAR]",
            }, ensure_ascii=False)
            LIVE_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(LIVE_LOG, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass

    async def input(self, action: str, x: float = 0, y: float = 0,
                    text: str = "", key: str = "", dy: float = 0) -> None:
        if not self.page:
            raise RuntimeError("会话未启动")
        self.touch()
        if action == "click":
            await self.page.mouse.click(float(x), float(y))
        elif action == "type":
            await self.page.keyboard.type(str(text))
        elif action == "key":
            await self.page.keyboard.press(str(key or "Enter"))
        elif action == "scroll":
            await self.page.mouse.wheel(0, float(dy))
        elif action == "reload":
            try:
                await self.page.reload(wait_until="domcontentloaded", timeout=15000)
            except Exception:
                await self._navigate(self.page)
        else:
            raise ValueError(f"不支持的操作: {action}")


_session = _LoginSession()


async def _running_task_count() -> int:
    """有抓取在跑就别启动登录会话（共用 profile 会互相踢掉）。"""
    try:
        from sqlalchemy import func, select
        from sqlalchemy.ext.asyncio import AsyncSession

        from config.db_config import resolve_config_db_type
        from database.db_session import get_async_engine
        from database.models_v2 import ScheduledTaskRun

        engine = get_async_engine(resolve_config_db_type())
        if engine is None:
            return 0
        async with AsyncSession(engine, expire_on_commit=False) as s:
            return int(await s.scalar(
                select(func.count()).select_from(ScheduledTaskRun)
                .where(ScheduledTaskRun.status == "running")) or 0)
    except Exception:
        return 0


class InputPayload(BaseModel):
    action: str = Field(..., description="click | type | key | scroll | reload")
    x: float = 0
    y: float = 0
    text: str = ""
    key: str = ""
    dy: float = 0


@router.post("/start")
async def start_session() -> Dict[str, Any]:
    if _session.active:
        _session.touch()
    else:
        try:
            await _session.start()
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except Exception as e:
            raise HTTPException(status_code=500,
                                detail=f"启动登录页失败: {type(e).__name__}: {e}") from e
    return {"ok": True, "viewport": VIEWPORT,
            "message": "原版登录页已打开，在图片上点击、用下方输入框打字"}


@router.get("/screenshot")
async def screenshot() -> Response:
    await _session._expire_if_idle()
    if not _session.active:
        raise HTTPException(status_code=404, detail="会话未启动或已超时关闭")
    try:
        data = await _session.screenshot()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return Response(content=data, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store, max-age=0"})


@router.post("/input")
async def send_input(payload: InputPayload) -> Dict[str, Any]:
    await _session._expire_if_idle()
    if not _session.active:
        raise HTTPException(status_code=409, detail="会话已关闭，请重新打开登录页")
    try:
        await _session.input(payload.action, payload.x, payload.y,
                             payload.text, payload.key, payload.dy)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"ok": True}


@router.get("/status")
async def status() -> Dict[str, Any]:
    await _session._expire_if_idle()
    active = _session.active
    return {
        "active": active,
        "logged_in": await _session.logged_in() if active else False,
        "started_at": _session.started_at,
        "idle_left_sec": max(0, int(IDLE_TIMEOUT_SEC - (
            time.monotonic() - _session.last_active))) if active else 0,
        "message": ("已检测到登录态，点「完成」关闭会话即可，Cookie 会自动保存"
                    if active and await _session.logged_in()
                    else "在页面上完成登录（扫码或手机号+短信验证码）"),
    }


@router.post("/stop")
async def stop_session() -> Dict[str, Any]:
    logged = await _session.logged_in()
    await _session.stop()
    return {"ok": True, "logged_in": logged,
            "message": ("登录态已写入 browser_data 卷，下次抓取自动生效"
                        if logged else "会话已关闭（未检测到登录态）")}
