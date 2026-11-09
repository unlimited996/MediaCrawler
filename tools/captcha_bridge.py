# -*- coding: utf-8 -*-
"""抓取进程命中安全验证时，把验证页推到 WebUI 的跨进程桥。

背景
----
容器里没有 X server，`_human_verify` 原来走
``playwright.chromium.launch(headless=False)`` 弹「专用可见窗口」，而镜像里
装的是 ``chrome-headless-shell``（连有头模式都不支持），根本弹不出任何窗口。
日志里「已临时打开浏览器窗口 / 浏览器窗口已置前」只是 ``launch()`` 返回成功，
用户什么也看不到，于是 ``wait_for_human_verify`` 干等 300s 后放弃。

做法
----
不再另起浏览器，直接把**主流程那个命中验证的 headless 页面**
``self.context_page`` 通过共享文件推给 crawler API 进程：

    logs/captcha_session/
        active.json   会话元信息（存在即「需要人工验证」，WebUI 据此自动弹窗）
        frame.jpg     最新页面截图（原子替换，避免 API 读到半张图）
        cmd.jsonl     用户指令，API 追加写，抓取侧按字节偏移消费
        done.json     用户点「完成验证」的标记，让等待循环立刻继续

群晖 compose 里 API 与 worker 同容器、共用 ``mc-logs:/app/logs`` 卷，
所以文件通道可用；与 ``scheduler_live.jsonl`` / ``.stop_crawl`` 是同一套机制。

默认只在「Linux 且无 DISPLAY」时启用（容器），本机 Windows / 有图形界面的
Linux 仍走原来的弹窗逻辑；可用 ``XHS_CAPTCHA_REMOTE=1/0`` 强制开关。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

SHOT_INTERVAL_SEC = 0.8
SESSION_TTL_SEC = 20 * 60

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SESSION_DIR = PROJECT_ROOT / "logs" / "captcha_session"
ACTIVE_FILE = SESSION_DIR / "active.json"
FRAME_FILE = SESSION_DIR / "frame.jpg"
FRAME_TMP = SESSION_DIR / ".frame.tmp"
CMD_FILE = SESSION_DIR / "cmd.jsonl"
DONE_FILE = SESSION_DIR / "done.json"

_cmd_offset = 0


def _log(msg: str) -> None:
    # 抓取子进程 stdout 会被 worker 收进 scheduler_live.jsonl，用户能在 WebUI 看到
    print(f"[captcha-bridge] {msg}", flush=True)


def is_remote_mode() -> bool:
    """是否需要走 WebUI 远程过验证。

    容器 = Linux + 没有 DISPLAY/X server；本机开发（Windows 或有桌面的 Linux）
    保持原来的弹窗行为。允许用环境变量强制覆盖，便于调试。
    """
    forced = os.environ.get("XHS_CAPTCHA_REMOTE", "").strip().lower()
    if forced in ("1", "true", "yes", "on"):
        return True
    if forced in ("0", "false", "no", "off"):
        return False
    if sys.platform.startswith("linux"):
        return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return False


def is_done() -> bool:
    """给 ``wait_for_human_verify`` 的同步回调：用户已在 WebUI 点了「完成验证」。"""
    return DONE_FILE.exists()


def start_session(url: str = "") -> None:
    """开启一次远程验证会话，清掉上一轮的残留。"""
    global _cmd_offset
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    for p in (ACTIVE_FILE, FRAME_FILE, FRAME_TMP, CMD_FILE, DONE_FILE):
        p.unlink(missing_ok=True)
    _cmd_offset = 0
    ACTIVE_FILE.write_text(json.dumps({
        "pid": os.getpid(),
        "url": url,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ts": time.time(),
    }, ensure_ascii=False), encoding="utf-8")
    _log("已推送验证会话到 WebUI，请在「安全验证」弹窗中完成（滑块/扫码/点按）")


async def close() -> None:
    """结束会话：写 done 让等待循环放行，删 active 让弹窗自动关闭。"""
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    if not DONE_FILE.exists():
        DONE_FILE.write_text(json.dumps({
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "reason": "closed",
        }, ensure_ascii=False), encoding="utf-8")
    ACTIVE_FILE.unlink(missing_ok=True)
    FRAME_FILE.unlink(missing_ok=True)
    FRAME_TMP.unlink(missing_ok=True)


async def _screenshot(page) -> None:
    data = await page.screenshot(type="jpeg", quality=60)
    # 原子替换：API 侧随时可能在读，直接覆盖会读到半张 JPEG
    FRAME_TMP.write_bytes(data)
    os.replace(FRAME_TMP, FRAME_FILE)


def _read_new_commands() -> list[Dict[str, Any]]:
    """按字节偏移读增量指令；末尾半行留到下一轮，避免解析失败。"""
    global _cmd_offset
    if not CMD_FILE.exists():
        return []
    size = CMD_FILE.stat().st_size
    if size <= _cmd_offset:
        return []
    with open(CMD_FILE, "rb") as fh:
        fh.seek(_cmd_offset)
        raw = fh.read()
    text = raw.decode("utf-8", errors="replace")
    if not text.endswith("\n"):
        tail = text.rsplit("\n", 1)[-1]
        _cmd_offset = size - len(tail.encode("utf-8"))
        text = text[:len(text) - len(tail)]
    else:
        _cmd_offset = size
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                out.append(item)
        except Exception:
            continue
    return out


async def _apply(page, cmd: Dict[str, Any]) -> None:
    action = str(cmd.get("action") or "")
    x, y = float(cmd.get("x") or 0), float(cmd.get("y") or 0)
    if action == "click":
        await page.mouse.click(x, y)
    elif action == "drag":
        # 滑块验证：按下 → 分步移动 → 松开，太快会被判机器人
        x2, y2 = float(cmd.get("x2") or x), float(cmd.get("y2") or y)
        await page.mouse.move(x, y)
        await page.mouse.down()
        await page.mouse.move(x2, y2, steps=max(8, int(cmd.get("steps") or 18)))
        await asyncio.sleep(0.15)
        await page.mouse.up()
    elif action == "type":
        await page.keyboard.type(str(cmd.get("text") or ""))
    elif action == "key":
        await page.keyboard.press(str(cmd.get("key") or "Enter"))
    elif action == "scroll":
        await page.mouse.wheel(0, float(cmd.get("dy") or 0))
    elif action == "reload":
        try:
            await page.reload(wait_until="domcontentloaded", timeout=15000)
        except Exception:
            pass
    elif action == "done":
        DONE_FILE.write_text(json.dumps({
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "reason": "user_confirmed",
        }, ensure_ascii=False), encoding="utf-8")
    else:
        _log(f"忽略未知指令: {action}")


async def drive(page, url: str = "") -> None:
    """后台任务：持续截图 + 消费指令，直到用户确认完成或会话超时。

    注意 **不要** 在这里改 page 的导航 —— ``wait_for_human_verify`` 正盯着
    同一个 page 轮询验证码是否消失，用户在 WebUI 上的真实操作会让它自然退出。
    """
    start_session(url)
    started = time.time()
    try:
        while not is_done() and time.time() - started < SESSION_TTL_SEC:
            try:
                await _screenshot(page)
            except Exception as e:
                _log(f"截图失败（页面可能正在跳转）: {type(e).__name__}")
            try:
                for cmd in _read_new_commands():
                    await _apply(page, cmd)
                    if cmd.get("action") == "done":
                        break
            except Exception as e:
                _log(f"执行指令失败: {type(e).__name__}: {e}")
            await asyncio.sleep(SHOT_INTERVAL_SEC)
    finally:
        await close()
        _log("远程验证会话已结束")


# 供 API 侧复用（避免两边各写一份路径/白名单）
ALLOWED_ACTIONS = ("click", "drag", "type", "key", "scroll", "reload", "done")
