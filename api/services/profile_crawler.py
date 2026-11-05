# -*- coding: utf-8 -*-
"""用户主页全量/增量拉取、单帖定期更新的执行器。

定位：不重写抓取内核，而是把现有 `main.py --type creator/detail` 子进程
包装成可调度、可统计、可重试的任务执行单元，供 api/services/task_scheduler.py
和手动触发接口复用。

能力：
- 主页地址解析与规范化（小红书完整支持，其它平台尽力解析）
- 增量去重：执行前预读已入库内容索引，xhs 翻到整页旧笔记即停；
  入库走 v2 upsert（按平台+内容ID），天然保证「跳过已存在 / 更新已有」
- 失败重试、限流间隔、单次超时（杀进程树）
- 执行前后对比内容索引，统计 新增 / 已刷新 / 未变化
- 每篇帖子的完整字段与抓取时间由 store 层落库（ext.crawled_at）
"""

import asyncio
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tools.v2_sync import get_content_index

# 项目根目录（api/services/ → 上两级）
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 子进程日志只保留尾部若干行写进运行记录（避免 log_json 无限膨胀）
LOG_TAIL_LINES = 200

# 主页全量拉取的帖子上限（翻到 has_more=false 为止，给个硬顶防失控）
PROFILE_MAX_NOTES = 10000

# 等待抓取通道空闲的轮询间隔
CHANNEL_POLL_SEC = 5

# 爬虫侧熔断标记（media_platform/<platform>/core.py 里同名常量）
# 子进程退出码为 0 但日志含它 = 评论阶段被风控拦下，任务要按失败处理
RISK_ABORT_MARKER = "[MEDIACRAWLER_RISK_ABORT]"

_XHS_USER_RE = re.compile(r"/user/profile/([^/?#]+)")
_XHS_NOTE_RE = re.compile(r"/(?:explore|discovery/item)/([^/?#]+)")
_HEX24_RE = re.compile(r"^[0-9a-f]{24}$")


class CrawlExecutionResult:
    """一次任务执行的结果（含重试后的汇总）。"""

    def __init__(self):
        self.status = "failed"          # success | failed | timeout | skipped
        self.exit_code: Optional[int] = None
        self.new_count = 0
        self.updated_count = 0
        self.skipped_count = 0
        self.attempts = 0
        self.error = ""
        self.log_tail: List[str] = []
        # 细粒度统计：新增/更新了哪些帖子 + 评论完整率前后对比（见 v2_sync.get_run_crawl_detail）
        self.stats: Optional[Dict[str, Any]] = None
        self.started_at: Optional[datetime] = None
        self.finished_at: Optional[datetime] = None

    @property
    def duration_ms(self) -> Optional[int]:
        if self.started_at and self.finished_at:
            return int((self.finished_at - self.started_at).total_seconds() * 1000)
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "new_count": self.new_count,
            "updated_count": self.updated_count,
            "skipped_count": self.skipped_count,
            "attempts": self.attempts,
            "error": self.error,
            "log_tail": self.log_tail,
            "stats": self.stats,
            "duration_ms": self.duration_ms,
        }


# ---------------------------------------------------------------------------
# URL 解析
# ---------------------------------------------------------------------------

def _url_params(url: str) -> Dict[str, str]:
    """取 query string 里的参数（xsec_token / xsec_source 等）。"""
    out: Dict[str, str] = {}
    query = url.split("?", 1)[1] if "?" in url else ""
    for pair in query.split("&"):
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def parse_profile_target(platform: str, url: str) -> Dict[str, str]:
    """解析用户主页地址。

    返回 {"user_id", "normalized_url", "xsec_token", "xsec_source", "ok"}。
    ok=False 表示无法解析出用户 ID（仍会按原串交给爬虫尝试）。
    """
    raw = (url or "").strip()
    info = {"user_id": "", "normalized_url": raw,
            "xsec_token": "", "xsec_source": "", "ok": False}
    if not raw:
        return info

    if platform == "xhs":
        params = _url_params(raw)
        if _HEX24_RE.match(raw):
            info.update(user_id=raw, ok=True)
            return info
        m = _XHS_USER_RE.search(raw)
        if m:
            info.update(
                user_id=m.group(1),
                xsec_token=params.get("xsec_token", ""),
                xsec_source=params.get("xsec_source", ""),
                ok=True,
            )
            return info
        # 兜底：任意含 24 位 hex 的串
        m2 = re.search(r"\b([0-9a-f]{24})\b", raw)
        if m2:
            info.update(user_id=m2.group(1), ok=True)
        return info

    # 其它平台：爬虫自己解析 URL，这里只做尽力提取（用于统计口径）
    m = re.search(r"/(?:user/profile|profile)/([^/?#]+)", raw)
    if m:
        info.update(user_id=m.group(1), ok=True)
    return info


def parse_note_target(platform: str, url: str) -> Dict[str, str]:
    """解析单帖地址，返回 {"content_id", "normalized_url", "xsec_token", "ok"}。"""
    raw = (url or "").strip()
    info = {"content_id": "", "normalized_url": raw, "xsec_token": "", "ok": False}
    if not raw:
        return info
    params = _url_params(raw)
    info["xsec_token"] = params.get("xsec_token", "")

    if platform == "xhs":
        m = _XHS_NOTE_RE.search(raw)
        if m:
            info.update(content_id=m.group(1), ok=True)
            return info
        m2 = re.search(r"\b([0-9a-f]{24})\b", raw)
        if m2:
            info.update(content_id=m2.group(1), ok=True)
        return info

    # 其它平台按各自常见格式提取内容 ID
    patterns = [
        r"/video/(\d+)", r"[?&]modal_id=(\d+)", r"/video/(\w+)",
        r"(BV[0-9A-Za-z]+)", r"[?&]aid=(\d+)",
        r"/detail/(\d+)", r"/status/(\w+)", r"/p/(\w+)",
        r"/question/(\d+)", r"/answer/(\d+)",
    ]
    for pat in patterns:
        m = re.search(pat, raw)
        if m:
            info.update(content_id=m.group(1), ok=True)
            return info
    return info


# ---------------------------------------------------------------------------
# 增量统计
# ---------------------------------------------------------------------------

def diff_content_index(before: Dict[str, int],
                       after: Dict[str, int]) -> Tuple[int, int, int]:
    """对比执行前后的内容索引，返回 (新增, 已刷新, 未变化)。

    crawled_at 每次 upsert 都会刷新，值变了即代表本轮被抓过。
    """
    new = updated = unchanged = 0
    for cid, ts in after.items():
        if cid not in before:
            new += 1
        elif ts != before[cid]:
            updated += 1
        else:
            unchanged += 1
    return new, updated, unchanged


# ---------------------------------------------------------------------------
# 子进程执行
# ---------------------------------------------------------------------------

def _kill_tree(proc: subprocess.Popen) -> None:
    """杀掉子进程整棵树（爬虫会拉起浏览器/node 等孙进程）。

    Windows 上 proc.kill() 只杀直接子进程，浏览器常常活下来占着用户数据目录，
    导致下一次启动报 profile 锁冲突，所以必须按进程树杀。
    """
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def build_crawler_command(task: Dict[str, Any]) -> List[str]:
    """按任务配置构造 main.py 命令行。

    与 api/services/crawler_manager.py::_build_command 保持一致的参数风格，
    但这里允许任务级覆盖（并发 / 限流 / 帖子数上限）。

    任务维度 → 抓取类型：
      profile（关注的用户）  → --type creator  --creator_id {主页}
      note（关注的帖子）    → --type detail   --specified_id {帖子}
      account（登录用户）   → --type account  --account_id {账号池 ID}
    """
    platform = task["platform"]
    task_type = task["task_type"]
    opts = task.get("options") or {}
    target = task["target"]

    type_map = {"profile": "creator", "note": "detail", "account": "account"}
    cmd = [sys.executable, "main.py",
           "--platform", platform,
           "--lt", opts.get("login_type", "cookie"),
           "--type", type_map.get(task_type, "detail"),
           "--save_data_option", opts.get("save_option", "postgres")]

    if task_type == "profile":
        cmd.extend(["--creator_id", target])
    elif task_type == "account":
        cmd.extend(["--account_id", target])
    else:
        cmd.extend(["--specified_id", target])

    cmd.extend(["--get_comment", "true" if opts.get("enable_comments", True) else "false"])
    cmd.extend(["--get_sub_comment", "true" if opts.get("enable_sub_comments", False) else "false"])
    cmd.extend(["--get_media", "true" if opts.get("enable_media", False) else "false"])

    if opts.get("max_comments_count"):
        cmd.extend(["--max_comments_count_singlenotes", str(opts["max_comments_count"])])
    if opts.get("cookies"):
        cmd.extend(["--cookies", opts["cookies"]])
    cmd.extend(["--headless", "true" if opts.get("headless", True) else "false"])
    return cmd


async def build_crawler_env(task: Dict[str, Any]) -> Dict[str, str]:
    """任务级执行参数通过环境变量注入爬虫（base_config 已支持 _env_int）。

    关键修复：每次任务启动都重新从库表加载 runtime_config。否则配置页里
    apply_timing='next_task' 的项（如两个单轮预算）编辑后不会触发热刷新
    （config_admin 仅对 immediate 项调用 refresh），build_crawler_env 读到的
    仍是进程启动时的旧缓存 —— 表现为「页面改成 0 仍然按默认 20 执行」。
    在任务启动入口统一刷新，既保证 next_task 改动本轮生效，也顺带让所有
    配置项都读到最新库值。
    """
    # 重新拉取库表最新值，确保 next_task 类配置的页面改动在本轮生效
    try:
        from config.runtime_config import runtime_config
        await runtime_config.refresh()
    except Exception as e:
        print(f"[build_crawler_env] 刷新运行时配置失败，沿用缓存: {e}")
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["MAX_CONCURRENCY_NUM"] = str(int(task.get("max_concurrency") or 1))
    env["CRAWLER_MAX_SLEEP_SEC"] = str(float(task.get("rate_interval_sec") or 3))
    # 小红书抓取节奏 / 验证码等待：从配置页读最新值注入子进程 env，
    # 子进程的 config.xhs_config 用 _env_float/_env_int 覆盖默认值。
    if task.get("platform") == "xhs":
        try:
            from config import xhs_config as _xc
            from config.runtime_config import runtime_config as _rc
            env["XHS_SLEEP_MIN_SEC"] = str(float(
                _rc.get_float("xhs_sleep_min_sec", _xc.XHS_SLEEP_MIN_SEC)))
            env["XHS_SLEEP_MAX_SEC"] = str(float(
                _rc.get_float("xhs_sleep_max_sec", _xc.XHS_SLEEP_MAX_SEC)))
            env["XHS_CAPTCHA_WAIT_SEC"] = str(int(
                _rc.get_int("xhs_captcha_wait_sec", _xc.XHS_CAPTCHA_WAIT_SEC)))
            env["XHS_INTERLEAVE_EVERY_PAGES"] = str(int(
                _rc.get_int("xhs_interleave_every_pages", _xc.XHS_INTERLEAVE_EVERY_PAGES)))
            env["XHS_461_INTERLEAVE_RETRIES"] = str(int(
                _rc.get_int("xhs_461_interleave_retries", _xc.XHS_461_INTERLEAVE_RETRIES)))
        except Exception as e:
            print(f"[build_crawler_env] 读取小红书节奏配置失败，回退常量: {e}")
    # 注：2026-11-03 起不再注入 XHS_DETAIL_BUDGET_PER_ROUND /
    # XHS_COMMENT_BUDGET_PER_ROUND —— 单轮请求数量上限已移除（会与「慢速抓全」
    # 诉求冲突，且人为截断单篇评论）。风控改由慢速随机间隔 + 撞 461 退避续抓承担。
    if task.get("task_type") == "account":
        # 账号维度：行为来源（点赞/收藏）由任务选项决定，经环境变量传给爬虫
        opts = task.get("options") or {}
        behaviors = opts.get("behaviors") or ["likes", "collects"]
        env["XHS_ACCOUNT_BEHAVIORS"] = ",".join(
            str(b) for b in behaviors if str(b) in ("likes", "collects")) or "likes,collects"
        # 列表抓取额度：account 模式必须有自己的额度，不能沿用
        # CRAWLER_MAX_NOTES_COUNT（默认 15）——否则点赞只抓 2 页 20 条、
        # 收藏直接一条不抓（2026-10-28 修）。任务选项 max_notes（即 UI
        # 「本次抓取上限」）同时作为「新帖额度」与「补抓篇数上限」的统一值，
        # 二者不再分设（2026-10-29 修：删除独立的 XHS_COMMENT_BACKFILL_MAX_NOTES）。
        if opts.get("max_notes"):
            env["XHS_BEHAVIOR_MAX_NOTES"] = str(int(opts["max_notes"]))
        # 增量：默认只补新点赞；任务选项 mode=full 时强制全量重抓
        mode = (opts.get("mode") or task.get("mode") or "incremental")
        env["XHS_BEHAVIOR_INCREMENTAL"] = "0" if str(mode) == "full" else "1"
        env.pop("XHS_CREATOR_INCREMENTAL", None)
        return env
    if task.get("task_type") == "profile":
        # 主页拉取默认不限条数（翻到尾），增量模式靠整页早停收敛
        env["CRAWLER_MAX_NOTES_COUNT"] = str(PROFILE_MAX_NOTES)
        if (task.get("platform") == "xhs"
                and (task.get("mode") or "incremental") == "incremental"):
            env["XHS_CREATOR_INCREMENTAL"] = "1"
        else:
            env.pop("XHS_CREATOR_INCREMENTAL", None)
    return env


async def _wait_for_channel(busy_wait_sec: int,
                            is_busy, on_log=None) -> Tuple[bool, str]:
    """等待抓取通道空闲（手动抓取 / 其它定时任务占用时排队等待）。

    返回 (ok, reason)。超时未轮到 → reason='crawler_busy'。

    等待期间每 30s 打一条日志：此前是纯静默等待 300s 再跳过，用户点了
    「立即执行」后界面上什么都看不到，会误以为任务没生效。
    """
    started = time.monotonic()
    deadline = started + max(0, busy_wait_sec)
    last_note = 0.0
    while True:
        if not is_busy():
            waited = time.monotonic() - started
            if on_log and waited >= 10:
                on_log(f"抓取通道已空闲（等待 {int(waited)}s），开始抓取", "info")
            return True, ""
        now = time.monotonic()
        if now >= deadline:
            return False, "crawler_busy"
        # 首次 + 之后每 30s 提示一次，让执行记录/控制台看得到「在排队」
        if on_log and now - last_note >= 30:
            on_log(
                f"抓取通道被占用（手动抓取或其它任务正在跑），排队等待中："
                f"已等 {int(now - started)}s / 上限 {int(busy_wait_sec)}s",
                "info",
            )
            last_note = now
        await asyncio.sleep(CHANNEL_POLL_SEC)


async def _run_once(task: Dict[str, Any], busy_wait_sec: int,
                    is_busy, on_log=None) -> CrawlExecutionResult:
    """执行一次（不含重试）。超时/退出码非 0 都算失败。"""
    res = CrawlExecutionResult()
    res.started_at = datetime.now()

    # 1) 等抓取通道（占用时排队；期间持续打日志，避免用户以为任务没动静）
    ok, reason = await _wait_for_channel(busy_wait_sec, is_busy, on_log=on_log)
    if not ok:
        res.status = "skipped"
        res.error = (
            f"抓取通道被占用超过 {busy_wait_sec}s，本次跳过"
            f"（有手动抓取或其它定时任务在跑，通道空闲后重新触发即可）"
        )
        res.finished_at = datetime.now()
        if on_log:
            on_log(res.error, "warning")
        return res

    # 2) 预读内容索引（增量统计 + xhs 增量翻页的地基）
    platform = task["platform"]
    before: Dict[str, int] = {}
    # 账号维度：抓来的帖子作者各异，无法按作者/单帖预读索引，
    # 改为记录执行时间窗，结束后按 crawled_at 归因统计新增/刷新
    account_window_start = int(time.time() * 1000) if task.get("task_type") == "account" else 0
    # 评论完整率「之前」的快照。抓完会跑 refresh_comment_completeness() 把
    # 完整率覆盖成本轮结果，这里不先拍下来就比不出本轮实际补齐了多少
    # （执行记录下拉的前后对比要靠它）。
    completeness_before: Dict[str, Dict] = {}
    if task.get("task_type") == "account":
        try:
            from tools.v2_sync import get_completeness_snapshot

            completeness_before = await get_completeness_snapshot(platform)
        except Exception as e:
            if on_log:
                on_log(f"完整率快照失败（不影响执行）：{e}", "warning")
    try:
        if task.get("task_type") == "profile":
            info = parse_profile_target(platform, task["target"])
            if info.get("user_id"):
                before = await get_content_index(platform, user_id=info["user_id"])
        else:
            info = parse_note_target(platform, task["target"])
            if info.get("content_id"):
                before = await get_content_index(platform, content_id=info["content_id"])
    except Exception as e:
        if on_log:
            on_log(f"预读内容索引失败（不影响执行）：{e}", "warning")

    # 3) 构造命令并起子进程
    cmd = build_crawler_command(task)
    env = await build_crawler_env(task)
    timeout = int(task.get("timeout_sec") or 1800)
    if on_log:
        on_log(f"启动抓取：{' '.join(cmd[:6])} ...", "info")

    # 新一轮开始：清掉可能残留的「停止当前抓取」标记，
    # 否则上一轮点过停止、人走开了，本轮刚起来就会被误杀
    try:
        from tools.crawl_stop import clear_stop

        clear_stop()
    except Exception:
        pass

    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(PROJECT_ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            bufsize=1, creationflags=creation,
        )
    except Exception as e:
        res.status = "failed"
        res.error = f"启动子进程失败：{e}"
        res.finished_at = datetime.now()
        return res

    # 4) 流式读输出 + 超时控制
    lines: List[str] = []
    timed_out = False
    #: 用户点「停止抓取」后置位（跨进程信号文件，见 tools/crawl_stop.py）
    stop_state: Dict[str, Any] = {"hit": False, "reason": ""}

    async def _pump() -> None:
        loop = asyncio.get_running_loop()
        while True:
            line = await loop.run_in_executor(None, proc.stdout.readline)
            if not line:
                break
            line = line.rstrip("\n")
            if not line.strip():
                continue
            lines.append(line)
            if len(lines) > LOG_TAIL_LINES * 2:
                del lines[: len(lines) - LOG_TAIL_LINES]
            if on_log:
                on_log(line, "info")

    async def _stop_watchdog() -> None:
        """每秒看一次「停止抓取」信号；命中就杀掉整棵子进程树。

        单独开协程而不是塞进 _pump：爬虫可能几十秒不输出一行，
        靠 readline 的回调兜不住，停止按钮会显得"点了没反应"。
        """
        from tools.crawl_stop import stop_requested

        while proc.poll() is None:
            info = stop_requested()
            if info:
                stop_state["hit"] = True
                stop_state["reason"] = str(info.get("reason") or "用户手动停止")
                if on_log:
                    on_log(f"收到停止请求（{stop_state['reason']}），正在终止抓取…", "warning")
                _kill_tree(proc)
                return
            await asyncio.sleep(1)

    watchdog = asyncio.create_task(_stop_watchdog())
    try:
        await asyncio.wait_for(_pump(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        _kill_tree(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=20)
        except Exception:
            pass
    finally:
        watchdog.cancel()
        try:
            await watchdog
        except (asyncio.CancelledError, Exception):
            pass

    # stdout EOF 只代表管道关闭，进程可能还没被 reap，poll() 会返回 None。
    # 必须显式 wait() 拿真实退出码，否则会把成功当成失败（还会触发无谓的重试）。
    try:
        await asyncio.wait_for(proc.wait(), timeout=30)
    except Exception:
        pass
    exit_code = proc.poll()
    try:
        if proc.stdout:
            proc.stdout.close()
    except Exception:
        pass

    res.log_tail = lines[-LOG_TAIL_LINES:]
    res.exit_code = exit_code

    # 5) 后读内容索引，算增量
    try:
        if task.get("task_type") == "account":
            from tools.v2_sync import get_run_crawl_stats

            res.new_count, res.updated_count = await get_run_crawl_stats(
                platform, account_window_start, int(time.time() * 1000))
            res.skipped_count = 0
            # 明细跟上：动了哪些帖子、完整率前后各多少。与上面同源同时窗，
            # 保证明细条数和 headline 计数一致。失败不影响主流程。
            try:
                from tools.v2_sync import get_run_crawl_detail

                res.stats = await get_run_crawl_detail(
                    platform, account_window_start, int(time.time() * 1000),
                    completeness_before)
            except Exception as stat_exc:
                if on_log:
                    on_log(f"执行明细统计失败（不影响结果）：{stat_exc}",
                           "warning")
        elif task.get("task_type") == "profile":
            info = parse_profile_target(platform, task["target"])
            if info.get("user_id"):
                after = await get_content_index(platform, user_id=info["user_id"])
                res.new_count, res.updated_count, res.skipped_count = \
                    diff_content_index(before, after)
        else:
            info = parse_note_target(platform, task["target"])
            if info.get("content_id"):
                after = await get_content_index(platform, content_id=info["content_id"])
                res.new_count, res.updated_count, res.skipped_count = \
                    diff_content_index(before, after)
    except Exception as e:
        if on_log:
            on_log(f"统计增量失败（不影响执行结果）：{e}", "warning")

    res.finished_at = datetime.now()
    # 风控熔断：子进程退出码仍是 0，但评论阶段被平台验证码/限流整批拦下。
    # 这种情况必须报失败——否则任务显示 success，用户只看得到「帖子进来了、
    # 评论是空的」，还无从判断是没评论还是被风控了。
    risk_line = next((l for l in lines if RISK_ABORT_MARKER in l), "")
    if stop_state["hit"]:
        try:
            from tools.crawl_stop import clear_stop

            clear_stop()          # 消费掉信号，别影响下一轮
        except Exception:
            pass
        res.status = "stopped"
        res.error = f"{stop_state['reason']}（已终止本轮抓取，进度已保留）"
        if on_log:
            on_log(res.error, "warning")
    elif timed_out:
        res.status = "timeout"
        res.error = f"执行超过 {timeout}s 已终止"
    elif risk_line:
        res.status = "failed"
        res.error = f"触发平台风控，评论未抓全：{risk_line[-200:]}"
        if on_log:
            on_log(res.error, "error")
    elif exit_code == 0:
        res.status = "success"
        res.error = ""
    else:
        res.status = "failed"
        tail = " | ".join(lines[-3:])[:300]
        res.error = f"子进程退出码 {exit_code}" + (f"：{tail}" if tail else "")
    return res


async def execute_task(task: Dict[str, Any], busy_wait_sec: int = 300,
                       is_busy=None, on_log=None) -> CrawlExecutionResult:
    """执行一个定时任务（含重试）。

    task: 行记录 dict，至少含 platform/task_type/target/mode/options/
          max_concurrency/timeout_sec/retry_count/retry_delay_sec/rate_interval_sec。
    is_busy: 判断抓取通道是否被占用的可调用对象（传 crawler_manager.is_running）。
    on_log: 可选回调 (message, level)。
    """
    if is_busy is None:
        from .crawler_manager import crawler_manager
        is_busy = crawler_manager.is_running

    retries = max(0, int(task.get("retry_count") or 0))
    delay = max(0, int(task.get("retry_delay_sec") or 0))
    total_attempts = retries + 1

    result = CrawlExecutionResult()
    for attempt in range(1, total_attempts + 1):
        # 跨进程抓取通道：登记「本进程正在抓取」，让另一侧的 API 手动抓取
        # 能看到（反之亦然）。拿不到（别人在抓）就不登记，_run_once 里的
        # 通道等待会先把这一轮排到空闲再开跑。
        _ch_ok = False
        try:
            from tools.proc_lock import acquire_channel, release_channel

            _ch_ok = acquire_channel(
                "scheduler", note=str(task.get("name") or task.get("task_id") or ""))
        except Exception:
            _ch_ok = False
        try:
            result = await _run_once(task, busy_wait_sec, is_busy, on_log)
        finally:
            if _ch_ok:
                try:
                    release_channel()
                except Exception:
                    pass
        result.attempts = attempt
        if result.status == "success":
            break
        if result.status == "skipped":
            break  # 通道被占不是任务的错，重试没有意义
        if result.status == "stopped":
            # 用户主动停止：绝不能自动重试，否则等于跟用户对着干
            if on_log:
                on_log("本轮已被用户停止，不自动重试", "warning")
            break
        # 风控熔断不重试：被平台验证码/限流整批拦下后 60s 就再打一轮，
        # 只会把账号越推越死（熔断的意义本来就是踩刹车）。等下一轮排期，
        # 留出冷却/人工过验证码的时间。
        if result.status == "failed" and "触发平台风控" in (result.error or ""):
            if on_log:
                on_log(
                    "本轮触发平台风控，已熔断：不自动重试，等下次排期"
                    "（建议先冷却或人工过验证码）", "warning")
            break
        if attempt < total_attempts:
            if on_log:
                on_log(f"第 {attempt}/{total_attempts} 次执行失败，{delay}s 后重试", "warning")
            await asyncio.sleep(delay)

    # 汇总日志取最后一次尝试的尾部
    return result
