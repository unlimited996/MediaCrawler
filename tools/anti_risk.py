# -*- coding: utf-8 -*-
"""反检测 / 反风控通用工具：随机间隔、验证码人工介入。

小红书（及定时任务）共用：
- 请求间隔随机化，避免固定节奏被风控识别
- 触发验证码 / 安全限制时，等用户在已连接的真实浏览器里手动完成验证，
  然后自动继续抓取（人工介入，不尝试自动过验证）
"""
from __future__ import annotations

import asyncio
import random
from typing import Optional, Tuple

from tools import utils

#: 页面判定为「验证码/安全验证」的关键字（标题或正文包含任一即命中）
_CAPTCHA_MARKERS = (
    "安全验证", "滑动验证", "拖动滑块", "验证码", "访问频繁",
    "安全限制", "环境异常", "verify", "captcha", "slider",
)


def random_interval(lo: float, hi: float) -> float:
    """[lo, hi] 之间的随机秒数（两位小数）。"""
    lo_f, hi_f = float(lo), float(hi)
    if hi_f < lo_f:
        lo_f, hi_f = hi_f, lo_f
    return round(random.uniform(lo_f, hi_f), 2)


async def random_sleep(lo: float, hi: float, reason: str = "") -> float:
    """随机休眠 [lo, hi] 秒并记录实际值（日志可追溯，便于复盘风控）。"""
    sec = random_interval(lo, hi)
    utils.logger.info(
        f"[anti_risk] 随机休眠 {sec}s{(' · ' + reason) if reason else ''}")
    await asyncio.sleep(sec)
    return sec


async def interval_sleep(spec, reason: str = "") -> float:
    """按间隔规格休眠：float=固定秒；(lo, hi)=随机区间。

    爬虫内部（client 的分页/翻页循环）用这个统一入口，
    配置里给区间就随机、给数字就固定，两种写法兼容。
    """
    if isinstance(spec, (tuple, list)) and len(spec) == 2:
        return await random_sleep(spec[0], spec[1], reason)
    sec = float(spec or 0)
    if sec > 0:
        utils.logger.info(
            f"[anti_risk] 固定休眠 {sec}s{(' · ' + reason) if reason else ''}")
        await asyncio.sleep(sec)
    return sec


def xhs_sleep_bounds() -> Tuple[float, float]:
    """小红书请求间隔：默认 5-15s 随机。

    优先读小红书专属配置 XHS_SLEEP_MIN_SEC / XHS_SLEEP_MAX_SEC，
    没有则退回 5-15 硬编码默认（老配置迁移不清空）。
    """
    import config

    lo = float(getattr(config, "XHS_SLEEP_MIN_SEC", 5) or 5)
    hi = float(getattr(config, "XHS_SLEEP_MAX_SEC", 15) or 15)
    if hi < lo:
        lo, hi = hi, lo
    return lo, hi


async def xhs_sleep(reason: str = "") -> float:
    """小红书抓取请求之间的随机间隔（默认 5-15s）。"""
    lo, hi = xhs_sleep_bounds()
    return await random_sleep(lo, hi, reason or "小红书请求间隔")


async def jitter_sleep(lo: float = 1.0, hi: float = 5.0,
                       reason: str = "") -> float:
    """定时任务接口调用的随机抖动间隔（默认 1-5s）。"""
    return await random_sleep(lo, hi, reason or "接口调用抖动")


def looks_like_captcha_text(text: str) -> bool:
    """一段文本（标题/正文）是否像验证码页。"""
    if not text:
        return False
    low = text.lower()
    return any(m.lower() in low for m in _CAPTCHA_MARKERS)


async def page_has_captcha(page) -> bool:
    """当前页面是否停在验证码/安全验证页。

    page 为 None（无浏览器模式）时返回 False —— 那种模式没有可人工操作的页面。
    """
    if page is None:
        return False
    try:
        title = await page.title()
    except Exception:
        return False
    if looks_like_captcha_text(title or ""):
        return True
    try:
        url = str(page.url or "")
    except Exception:
        url = ""
    if any(k in url for k in ("/verify", "/captcha", "/security")):
        return True
    try:
        body = (await page.inner_text("body"))[:2000]
    except Exception:
        return False
    return looks_like_captcha_text(body)


async def wait_for_human_verify(page, timeout_sec: int = 300,
                                poll_sec: float = 5.0) -> bool:
    """验证码人工介入：等用户在真实浏览器里手动完成验证后继续。

    流程：打印醒目指引 → 轮询页面验证码是否消失 → 消失即放行；
    超时返回 False（调用方决定重试或中止）。

    page 为 None 时无法人工操作，直接返回 False（不再空等 30s：
    没人能在这台机器上点验证码，等下去只是拖慢整批任务）。
    """
    timeout_sec = int(timeout_sec or 300)
    if page is None:
        utils.logger.warning(
            "[anti_risk] 触发验证码但当前是无浏览器模式，无法人工介入，"
            "直接跳过该目标")
        return False

    utils.logger.warning(
        "\n" + "=" * 64 +
        "\n[anti_risk] 触发小红书安全验证！请在已连接的浏览器窗口中"
        "\n           手动完成验证（滑块/二维码），完成后程序会自动继续。"
        f"\n           最长等待 {timeout_sec}s ..." +
        "\n" + "=" * 64)

    waited = 0.0
    while waited < timeout_sec:
        if not await page_has_captcha(page):
            utils.logger.info("[anti_risk] 人工验证已完成，继续抓取")
            return True
        await asyncio.sleep(poll_sec)
        waited += poll_sec
        if int(waited) % 60 == 0:
            utils.logger.info(
                f"[anti_risk] 仍在等待人工验证 ...（已等 {int(waited)}s）")

    utils.logger.error(f"[anti_risk] 等待人工验证超时（{timeout_sec}s），放弃本次操作")
    return False


def is_captcha_error(exc: Exception) -> bool:
    """异常是否属于验证码/安全限制类（需要人工介入而非简单重试）。"""
    text = str(exc)
    if "CAPTCHA" in text or "Verifytype" in text or "Verifyuuid" in text:
        return True
    if type(exc).__name__ in ("PlatformAccessError", "IPBlockError"):
        return True
    return False
