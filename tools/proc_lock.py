# -*- coding: utf-8 -*-
"""跨进程互斥工具（OS 文件锁 + PID 存活探测）。

用途：
  1. 单实例保护：同一个「执行器/后端」只允许一个进程在跑
     （`logs/.worker.lock`、`logs/.api_instance.lock`）。
  2. 抓取通道互斥：同一时刻只允许一个进程真正在抓取
     （`logs/.crawl_channel.json`），避免 API 手动抓取与定时任务执行器
     各起一个浏览器同时打同一个账号。

锁由操作系统持有：进程崩溃/被杀时自动释放，不会留下"僵尸锁"，
所以不需要超时清理逻辑；只有"记录型"状态（crawl channel）才需要探活。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"

#: 抓取通道状态文件（记录型，含 pid，便于探活）
CHANNEL_FILE = LOG_DIR / ".crawl_channel.json"


# ---------------------------------------------------------------------------
# 独占文件锁
# ---------------------------------------------------------------------------

def acquire_lock(path: Path, wait_sec: float = 0.0) -> Optional[object]:
    """尝试独占 `path` 的文件锁。

    成功返回文件句柄（**必须一直持有**，进程退出才释放），失败返回 None。
    `wait_sec > 0` 时在内部轮询等待。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + max(0.0, float(wait_sec))
    while True:
        try:
            fh = open(path, "a+")
        except OSError:
            return None
        try:
            # 确保至少有 1 字节可锁（空文件在部分平台锁不上）
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write("0")
                fh.flush()
            fh.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            try:
                fh.close()
            except Exception:
                pass
            if time.time() >= deadline:
                return None
            time.sleep(0.2)


def is_locked(path: Path) -> bool:
    """`path` 上的锁是否已被别的进程占用（探测后立即释放，不长期持有）。"""
    fh = acquire_lock(path)
    if fh is None:
        return True
    release_lock(fh)
    return False


def release_lock(fh) -> None:
    """主动释放锁（异常安全，失败忽略）。"""
    if fh is None:
        return
    try:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        fh.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# PID 探活
# ---------------------------------------------------------------------------

def pid_alive(pid: int) -> bool:
    """进程是否还活着（跨平台）。

    注意：Windows 上 **不能** 用 `os.kill(pid, 0)` —— CPython 会把它当作
    终止信号真的去 TerminateProcess，直接把别人的进程杀掉。必须走
    OpenProcess 查询句柄。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = wintypes.DWORD()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    # STILL_ACTIVE == 259；已退出进程会拿到真实退出码
                    return code.value == 259
                return True
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 抓取通道（谁在真正抓取）
# ---------------------------------------------------------------------------

def read_channel() -> Optional[dict]:
    """读当前抓取通道占用信息；无占用/已过期返回 None。"""
    try:
        raw = CHANNEL_FILE.read_text(encoding="utf-8")
        info = json.loads(raw)
    except Exception:
        return None
    if not isinstance(info, dict):
        return None
    if not pid_alive(info.get("pid")):
        # 持有者已死（崩溃/被杀）→ 视为空闲，顺手清掉残片
        try:
            CHANNEL_FILE.unlink()
        except Exception:
            pass
        return None
    return info


def channel_busy() -> bool:
    return read_channel() is not None


def acquire_channel(owner: str, note: str = "", force: bool = False) -> bool:
    """占用抓取通道；已被别的进程占用则返回 False。

    `force=True` 表示用户主动发起的抓取（手动抓取），即使通道被定时任务
    占着也照常标记（用户指令优先），只在调用方那边提示风险。
    """
    info = read_channel()
    if info is not None and not force:
        return False
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "owner": owner,
        "pid": os.getpid(),
        "since": time.time(),
        "note": note,
    }
    try:
        CHANNEL_FILE.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        return False
    # 写入后二次确认（并发下后写者覆盖先写者的情况）
    cur = read_channel() or {}
    return int(cur.get("pid") or 0) == os.getpid()


def release_channel() -> None:
    """释放抓取通道（只有自己持有才删）。"""
    info = read_channel()
    if info and int(info.get("pid") or 0) == os.getpid():
        try:
            CHANNEL_FILE.unlink()
        except Exception:
            pass
