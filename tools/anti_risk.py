# -*- coding: utf-8 -*-
"""反检测 / 反风控通用工具：随机间隔、验证码人工介入。

小红书（及定时任务）共用：
- 请求间隔随机化，避免固定节奏被风控识别
- 触发验证码 / 安全限制时，等用户在已连接的真实浏览器里手动完成验证，
  然后自动继续抓取（人工介入，不尝试自动过验证）
"""
from __future__ import annotations

import asyncio
import os
import random
from typing import Callable, Optional, Tuple

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


def focus_browser_window() -> bool:
    """把浏览器窗口拉到最前（Windows）。

    验证码必须在**看得见的窗口**里点，否则用户根本不知道要去操作：
    Playwright 的 `bring_to_front()` 只激活标签页，窗口最小化/被其它窗口
    盖住时仍然看不见，所以这里再补一次 OS 级 restore + 置前。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        handles = []

        def _cb(hwnd, _lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                n = user32.GetWindowTextLengthW(hwnd)
                if n <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                title = buf.value or ""
            except Exception:
                return True
            if any(k in title for k in
                   ("Chromium", "Chrome", "Microsoft Edge", "小红书", "Xiaohongshu")):
                handles.append(hwnd)
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(WNDENUMPROC(_cb), 0)
        if not handles:
            return False
        hwnd = handles[0]
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


async def _show_banner(page, text: str, color: str = "#e11d48") -> None:
    """在页面上盖一条醒目的固定横幅，告诉用户「现在需要你做什么」。

    为什么必须这么做：461/471 是**接口层**返回的，浏览器页面本身可能完全
    正常（没有验证码、没有报错），用户盯着窗口根本不知道发生了什么，
    只会觉得「窗口弹出来又关掉了」。有了这条横幅，至少能看懂要干嘛。
    """
    js = """
    (args) => {
      const id = '__mc_risk_banner__';
      let el = document.getElementById(id);
      if (!el) {
        el = document.createElement('div');
        el.id = id;
        el.style.cssText = [
          'position:fixed', 'z-index:2147483647', 'left:0', 'right:0', 'top:0',
          'padding:12px 18px', 'font:600 15px/1.6 -apple-system,BlinkMacSystemFont,',
          '"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif',
          'color:#fff', 'text-align:center', 'box-shadow:0 2px 12px rgba(0,0,0,.35)',
          'letter-spacing:.3px'
        ].join(';');
        document.documentElement.appendChild(el);
      }
      el.style.background = args.color;
      el.textContent = args.text;
    }
    """
    try:
        await page.evaluate(js, {"text": text, "color": color})
    except Exception:
        pass  # 页面可能正在导航，打不上横幅不影响等待本身


async def human_interlude(page, base_url: Optional[str] = None,
                          scrolls: int = 3) -> None:
    """模拟真人操作：返回首页 + 滚动几下（打断「死磕同一接口」的机器画像）。

    小红书的风控很大程度看「请求节奏与形态」——连续几十次只打 comment/page 这一
    个接口，是最典型的爬虫画像。真人会在评论之间回首页、滚信息流、再看详情。
    没有浏览器页面时退化为短暂随机休眠（至少不零间隔连打）。

    仅做浏览器侧动作（不产生额外 API 请求，除 page.goto 本身）。
    """
    if page is None:
        await asyncio.sleep(random.uniform(2.5, 5.0))
        return
    try:
        if base_url:
            await page.goto(base_url, wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(random.uniform(1.2, 2.5))
        for _ in range(max(0, int(scrolls))):
            try:
                await page.mouse.wheel(0, random.randint(600, 1400))
            except Exception:
                pass
            await asyncio.sleep(random.uniform(0.8, 1.8))
        await asyncio.sleep(random.uniform(1.0, 2.5))
    except Exception as e:
        utils.logger.warning(f"[anti_risk] 人类行为穿插失败（忽略）：{e}")


async def wait_for_human_verify(page, timeout_sec: int = 300,
                                poll_sec: float = 5.0,
                                url: Optional[str] = None,
                                grace_sec: float = 8.0,
                                manual_window_sec: Optional[float] = None,
                                done_check: Optional[Callable[[], bool]] = None) -> bool:
    """验证码人工介入：把浏览器窗口弹到最前，等用户手动完成验证后继续。

    流程：窗口置前 →（可选）导航到触发风控的 URL → 等验证页真正渲染出来 →
    轮询验证码是否消失 → 消失即放行。

    返回 True 的两种情况：
    1. 页面上出现过验证码，且已被用户处理掉；
    2. 页面全程没有验证码（浏览器能正常访问）——给 `manual_window_sec` 的
       可操作窗口后按「Cookie 不同步」处理，让调用方同步 Cookie 再重试一次。

    与旧版的区别（2026-10-29）：
    - **主动前置窗口**并导航到出事的页面，用户一眼能看到要操作什么；
    - 先等 `grace_sec` 让页面渲染完再判断，避免"页面还没出来 → 误判已完成"
      导致立刻返回 True、下一发请求又撞 461；
    - 全程没见过验证页时不再直接放弃，而是留出人工窗口（用户可能要重新登录）。

    page 为 None 时无法人工操作，直接返回 False（调用方应先尝试拉起浏览器）。
    """
    # 容器远程过验证：用户点 WebUI「我已完成验证」后不必等满 300s。
    # done_check 是同步回调（读共享标记文件），默认 None 完全不影响原有调用点。
    def _remote_done() -> bool:
        try:
            return bool(done_check and done_check())
        except Exception:
            return False

    timeout_sec = int(timeout_sec or 300)
    # 轮询间隔必须 > 0：传 0 会让 waited 永远不增，循环卡死（测试里踩过）
    poll = max(0.05, float(poll_sec or 0))
    if page is None:
        utils.logger.warning(
            "[anti_risk] 触发验证码但没有可用浏览器页面，无法人工介入")
        return False

    # 1) 窗口前置 + 导航到出事的页面（让验证页/登录墙真的显现出来）
    try:
        await page.bring_to_front()
    except Exception:
        pass
    focus_browser_window()

    if url:
        try:
            await page.goto(url, wait_until="domcontentloaded",
                            timeout=min(30, max(10, timeout_sec)) * 1000)
        except Exception as e:
            utils.logger.warning(f"[anti_risk] 打开验证页失败（不影响人工操作）：{e}")

    if done_check:
        # 容器 / 无图形界面：根本没有可见窗口，验证页已推到 WebUI
        utils.logger.warning(
            "\n" + "=" * 64 +
            "\n[anti_risk] 触发小红书安全验证！验证页已推送到 WebUI，"
            "\n           请在「安全验证」弹窗中完成（滑块 / 扫码 / 点按），"
            "\n           点「我已完成验证」后程序立即继续。"
            f"\n           最长等待 {timeout_sec}s（页面：{(url or '当前页')[:80]}）" +
            "\n" + "=" * 64)
    else:
        utils.logger.warning(
            "\n" + "=" * 64 +
            "\n[anti_risk] 触发小红书安全验证！浏览器窗口已置前，请在该窗口中"
            "\n           手动完成验证（滑块 / 扫码 / 点按），完成后程序会自动继续。"
            f"\n           最长等待 {timeout_sec}s（页面：{(url or '当前页')[:80]}）" +
            "\n" + "=" * 64)

    # 2) 先给页面渲染的宽限期，避免"验证页还没出来"被误判成"已完成"
    waited = 0.0
    while waited < min(grace_sec, timeout_sec):
        await asyncio.sleep(1.0)
        waited += 1.0
        if _remote_done():
            utils.logger.info("[anti_risk] 用户已在 WebUI 确认完成验证，立即继续")
            return True

    # 3) 轮询：必须有「见过验证页 → 又消失」的过程才算通过
    saw_captcha = await page_has_captcha(page)
    if not saw_captcha:
        # 浏览器能正常打开 → 账号本身没被限，问题多半是 httpx 侧的 Cookie/签名
        # 没同步（或需要重新登录）。给用户一个可操作窗口，然后同步 Cookie 重试一次。
        # 注意：这里**不能**只等几十秒就走 —— 用户可能在扫码登录/手动过滑块，
        # 窗口一闪就关会让他完全来不及（2026-10-29 用户反馈「弹出来直接关闭了」）。
        window = float(timeout_sec if manual_window_sec is None
                       else manual_window_sec)
        window = max(0.0, min(window, max(0.0, timeout_sec - waited)))
        await _show_banner(
            page,
            f"⚠️ 检测到接口风控（页面可能没有验证码）：如未登录请在此窗口重新登录，"
            f"程序会等你 {int(window)} 秒后自动同步 Cookie 并重试")
        utils.logger.warning(
            "[anti_risk] 页面上没有出现验证码 —— 浏览器侧访问正常，"
            "问题多半在 httpx 的 Cookie/签名。"
            f"\n            如需重新登录 / 手动处理，请现在就在这个窗口里操作；"
            f"\n            {int(window)}s 后程序会同步最新 Cookie 并重试一次 ...")
        waited = 0.0
        while waited < window:
            await asyncio.sleep(poll)
            waited += poll
            if _remote_done():
                utils.logger.info("[anti_risk] 用户已在 WebUI 确认完成验证，立即继续")
                return True
            if await page_has_captcha(page):
                utils.logger.warning(
                    "[anti_risk] 检测到验证页，转为等待你完成验证 ...")
                saw_captcha = True
                break
            if int(waited) % 60 == 0:
                # 每分钟提醒一次：让用户知道程序还在等，不是卡死了
                utils.logger.warning(
                    f"[anti_risk] 仍在等待你操作（已等 {int(waited)}s / "
                    f"共 {int(window)}s），完成后会自动继续")
                await _show_banner(
                    page,
                    f"⚠️ 仍在等你操作：{int(window - waited)} 秒后自动同步 Cookie 并重试"
                    f"（若未登录，请现在扫码登录）")
        if not saw_captcha:
            utils.logger.info(
                "[anti_risk] 手动窗口结束，按「浏览器侧正常」处理："
                "同步 Cookie 后重试一次")
            return True
    utils.logger.warning("[anti_risk] 已检测到验证页，等待你完成验证 ...")
    await _show_banner(page, "⚠️ 请在此窗口完成安全验证，完成后程序会自动继续")

    while waited < timeout_sec:
        await asyncio.sleep(poll)
        waited += poll
        if _remote_done():
            utils.logger.info("[anti_risk] 用户已在 WebUI 确认完成验证，立即继续")
            return True
        if not await page_has_captcha(page):
            utils.logger.info(
                f"[anti_risk] 人工验证已完成（等了 {int(waited)}s），继续抓取")
            return True
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
