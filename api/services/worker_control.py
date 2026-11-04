# -*- coding: utf-8 -*-
"""抓取执行器（worker）的进程控制与实时日志桥。

架构约定（2026-11-04 拆分）：

    API 进程（uvicorn api.main:app, :8081）
        └─ 只提供查询/配置/前端接口，**不跑任何抓取**
    Worker 进程（python -m worker）
        └─ 独占 logs/.worker.lock，跑 task_scheduler + 抓取子进程

两者只通过「数据库 + 日志文件」通信，因此任一方重启都不会影响另一方：
重启 API 不影响正在跑的抓取；重启 worker 也不会让 API 掉线。

本模块提供三件事：
  1. `worker_alive()`        —— 探测执行器是否在跑（文件锁 + pid 记录）
  2. `ensure_worker()`       —— 没跑就按需拉起（detached，父进程退出不带走）
  3. `append_live()/tail_live()` —— 执行器实时日志落盘，API 侧读取给前端
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

from tools.proc_lock import (LOG_DIR, PROJECT_ROOT, acquire_lock, is_locked,
                             pid_alive, release_lock)

#: 执行器独占锁（拿不到 = 已有执行器在跑）
WORKER_LOCK = LOG_DIR / ".worker.lock"
#: 执行器进程自己的 stdout/stderr
WORKER_STDOUT_LOG = LOG_DIR / "worker.out.log"
#: 执行器实时日志（JSONL），API 的 /api/crawler/logs 从这里读
LIVE_LOG = LOG_DIR / "scheduler_live.jsonl"
#: 实时日志保留上限（行）。超过就裁掉前一半，避免无限增长
LIVE_LOG_MAX_LINES = 4000
#: worker 元信息（pid / 启动时间），便于 UI 展示
WORKER_META = LOG_DIR / ".worker.json"


# ---------------------------------------------------------------------------
# 存活探测
# ---------------------------------------------------------------------------

def worker_alive() -> bool:
    """执行器是否在跑。

    以**文件锁**为准（拿得到锁 = 没人持锁 = 没在跑），PID 记录只用于展示。
    这样即便进程被强杀、pid 文件残留，也不会误判为"在跑"。
    """
    if is_locked(WORKER_LOCK):
        return True
    # 锁没被占：清掉残留的 meta，避免 UI 显示幽灵 pid
    try:
        WORKER_META.unlink()
    except Exception:
        pass
    return False


def worker_info() -> dict:
    """执行器状态（给 /api/scheduler/status 用）。"""
    alive = worker_alive()
    info: dict = {"alive": alive, "pid": None, "started_at": None,
                  "stop_reason": None}
    if alive:
        try:
            info.update(json.loads(WORKER_META.read_text(encoding="utf-8")))
            info["alive"] = True
        except Exception:
            pass
    return info


def write_worker_meta(extra: Optional[dict] = None) -> None:
    """执行器启动时落一份元信息（pid/启动时间），仅用于展示。"""
    payload = {
        "pid": os.getpid(),
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "argv": sys.argv[1:],
    }
    if extra:
        payload.update(extra)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        WORKER_META.write_text(json.dumps(payload, ensure_ascii=False),
                               encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 拉起执行器
# ---------------------------------------------------------------------------

#: 两段式拉起用的引导脚本：父进程 spawn 它，它再 spawn 真正的 worker 后立刻退出。
#: 这样 worker 的父进程是一个**已退出的**引导进程，Windows 的 `taskkill /T`
#: （按父进程链杀整棵树）就够不到它 —— 否则重启 API 时会把正在跑的抓取一起带走。
#:
#: 注意：这里把子进程的 stdout 直接丢给 DEVNULL，**不做文件重定向**。
#: 实测「detached + close_fds=True + stdout=文件句柄」组合下，子进程拿不到
#: 可写的 std 句柄，print 静默丢失；而 close_fds=False 又会让子进程继承
#: 父进程的管道句柄，导致调用方读管道永远等不到 EOF（会挂住）。
#: 所以改成：子进程自己打开日志文件并 dup2 到 1/2（见 worker.py）。
_BOOTSTRAP = (
    "import os, subprocess, sys\n"
    "_flags = 0x00000008 | 0x00000200 | 0x01000000  # DETACHED|NEW_GROUP|BREAKAWAY\n"
    "subprocess.Popen([sys.argv[1], '-m', 'worker'], cwd=sys.argv[2],\n"
    "                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
    "                 stdin=subprocess.DEVNULL, close_fds=True,\n"
    "                 creationflags=_flags,\n"
    "                 env=dict(os.environ, PYTHONUNBUFFERED='1'))\n"
)


def spawn_worker() -> bool:
    """以「脱离父进程树」的方式拉起执行器。

    必须两段式（bootstrap → worker）：
      * 单段 detached 的子进程，ParentProcessId 仍指向 API。Windows 上
        `taskkill /T`（杀进程树）会顺着父链把它一起杀掉 —— 实测重启 API 时
        正在跑的抓取被带走，等于没解耦。
      * 两段式后 worker 的父进程是「已退出的引导进程」，树杀够不到。
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        try:
            subprocess.Popen(
                [sys.executable, "-c", _BOOTSTRAP, sys.executable,
                 str(PROJECT_ROOT)],
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
            return True
        except Exception as e:
            print(f"[worker] 拉起执行器失败：{e}")
            return False

    out = open(WORKER_STDOUT_LOG, "a", encoding="utf-8", errors="replace")
    try:
        subprocess.Popen(
            [sys.executable, "-m", "worker"],
            cwd=str(PROJECT_ROOT),
            stdout=out, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, close_fds=True,
            start_new_session=True,
            env=dict(os.environ, PYTHONUNBUFFERED="1"),
        )
        return True
    except Exception as e:
        print(f"[worker] 拉起执行器失败：{e}")
        return False
    finally:
        try:
            out.close()
        except Exception:
            pass


def ensure_worker(auto_spawn: bool = True, wait_sec: float = 3.0) -> str:
    """确保执行器在跑。

    返回 "running"（本来就在）/ "spawned"（刚拉起）/ "absent"（没在跑且未拉起）
    """
    if worker_alive():
        return "running"
    if not auto_spawn:
        return "absent"
    if not spawn_worker():
        return "absent"
    # 给子进程一点时间拿锁，避免紧接着的探测仍判"没在跑"
    deadline = time.time() + max(0.0, wait_sec)
    while time.time() < deadline:
        if worker_alive():
            return "spawned"
        time.sleep(0.2)
    return "absent"


def stop_worker() -> bool:
    """请求执行器退出（SIGTERM / taskkill），成功返回 True。

    只在需要「整体停机」时使用；日常重启 API 不要调它。
    """
    info = worker_info()
    pid = int(info.get("pid") or 0)
    if not pid or not pid_alive(pid):
        return False
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        else:
            os.kill(pid, 15)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 实时日志桥（worker 写 → API 读）
# ---------------------------------------------------------------------------

def append_live(message: str, level: str = "info") -> None:
    """执行器侧：追加一行实时日志（JSONL，带时间戳）。"""
    if level not in ("info", "warning", "error", "success", "debug"):
        level = "info"
    line = json.dumps({
        "ts": time.strftime("%H:%M:%S"),
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "level": level,
        "msg": str(message),
    }, ensure_ascii=False)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LIVE_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        _maybe_trim()
    except Exception:
        pass


def _maybe_trim() -> None:
    """日志超过上限时裁掉前一半（低频调用，成本可接受）。"""
    try:
        if LIVE_LOG.stat().st_size < 2 * 1024 * 1024:  # 2MB 以内不折腾
            return
        lines = LIVE_LOG.read_text(encoding="utf-8",
                                   errors="replace").splitlines()
        if len(lines) <= LIVE_LOG_MAX_LINES:
            return
        keep = lines[-(LIVE_LOG_MAX_LINES // 2):]
        LIVE_LOG.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except Exception:
        pass


def tail_live(limit: int = 100) -> List[dict]:
    """API 侧：读执行器最近 `limit` 条实时日志。"""
    if limit <= 0 or not LIVE_LOG.exists():
        return []
    try:
        # 日志文件不大（有 2MB trim 阈值），整读再取尾最稳
        raw = LIVE_LOG.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    out: List[dict] = []
    for line in raw.splitlines()[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict) and item.get("msg"):
            out.append(item)
    return out


def clear_live() -> None:
    """清空实时日志（新一轮整体启动时可调）。"""
    try:
        LIVE_LOG.write_text("", encoding="utf-8")
    except Exception:
        pass
