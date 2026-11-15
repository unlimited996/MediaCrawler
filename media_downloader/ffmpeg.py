# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_downloader/ffmpeg.py
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

"""ffmpeg 检测与音视频合流。

DASH 资源（如 B 站）的视频轨与音频轨是分开的，需要 ffmpeg 无损封装成 mp4。
本机没有 ffmpeg 时调用方应降级为单流直链，不做任何隐式安装。
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
import shutil
import subprocess
from pathlib import Path

from .types import MediaDownloadError

logger = logging.getLogger("MediaCrawler.media_downloader.ffmpeg")

DEFAULT_MERGE_TIMEOUT = 300.0

# 重编码线程上限：留出 CPU 给 API / 抓取，避免一条视频把整机压满。
# 0 表示交给 ffmpeg 自己按核数决定。
_SOFT_THREADS = int(os.getenv("VIDEO_FFMPEG_THREADS", "4") or 4)
_SOFT_PRESET = os.getenv("VIDEO_FFMPEG_PRESET", "superfast")
_SOFT_CRF = os.getenv("VIDEO_FFMPEG_CRF", "24")
_ENCODER_CACHE: dict[str, bool] = {}


@functools.lru_cache(maxsize=1)
def ffmpeg_path() -> str | None:
    """返回本机 ffmpeg 可执行文件路径，不存在则返回 None（进程内缓存一次）"""
    return shutil.which("ffmpeg")


@functools.lru_cache(maxsize=1)
def ffprobe_path() -> str | None:
    """ffprobe 可执行文件；与 ffmpeg 一起安装，缺失时探测能力降级。"""
    return shutil.which("ffprobe")


async def _run(args: list[str], timeout: float) -> tuple[int, str]:
    """跑一次 ffmpeg/ffprobe 并保证子进程被回收（事件循环不能被同步调用阻塞）。"""
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise MediaDownloadError(f"ffmpeg 执行超时（{timeout}s）") from exc
    except BaseException:
        if process.returncode is None:
            process.kill()
            with contextlib.suppress(BaseException):
                await process.wait()
        raise
    return process.returncode, (stdout or stderr or b"").decode("utf-8", "replace")


async def probe_video(path: Path) -> dict:
    """读取视频编码信息：{codec_v, codec_a, duration_sec}。

    ffprobe 不存在或解析失败时返回空字典，由调用方决定降级——不做隐式安装。
    """
    executable = ffprobe_path()
    if not executable:
        return {}
    code, out = await _run([
        executable, "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,codec_name",
        "-of", "json", str(path),
    ], timeout=60)
    if code != 0:
        return {}
    try:
        import json

        data = json.loads(out or "{}")
    except Exception:
        return {}
    codec_v, codec_a = "", ""
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "video" and not codec_v:
            codec_v = str(stream.get("codec_name") or "")
        elif stream.get("codec_type") == "audio" and not codec_a:
            codec_a = str(stream.get("codec_name") or "")
    try:
        duration = float((data.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return {"codec_v": codec_v, "codec_a": codec_a, "duration_sec": duration}


def is_browser_friendly(probe: dict) -> bool:
    """浏览器原生可播：H.264 视频 + AAC（或无音轨）音频。"""
    return (probe.get("codec_v") == "h264"
            and probe.get("codec_a") in ("aac", "", None))


def _ffmpeg_has_encoder(name: str) -> bool:
    """ffmpeg 是否编进了某个编码器（缓存，避免每条视频都跑一次 -encoders）。"""
    cached = _ENCODER_CACHE.get(name)
    if cached is not None:
        return cached
    exe = ffmpeg_path()
    ok = False
    if exe:
        try:
            out = subprocess.run([exe, "-hide_banner", "-encoders"],
                                 capture_output=True, text=True, timeout=15).stdout
            ok = name in (out or "")
        except Exception:
            ok = False
    _ENCODER_CACHE[name] = ok
    return ok


def _hw_encode_spec() -> tuple[list[str], list[str]]:
    """硬件编码参数 ``(全局参数, 输出参数)``；不可用返回 ``([], [])``。

    只做 VAAPI（Intel / AMD 核显通用）。真正生效还需要：
      * 容器挂载 ``/dev/dri``（compose: ``devices: ["/dev/dri:/dev/dri"]``）
      * 镜像内装 Intel/AMD 的 VAAPI 驱动（见 docs/DEPLOY_SPLIT.md）
    探测不到就静默回退 libx264，不影响任何现有部署。
    用 ``VIDEO_HWACCEL=none`` 可强制关闭，``VIDEO_VAAPI_DEVICE`` 可改设备路径。
    """
    mode = os.getenv("VIDEO_HWACCEL", "auto").strip().lower()
    if mode in ("none", "off", "0", "false"):
        return [], []
    device = os.getenv("VIDEO_VAAPI_DEVICE", "/dev/dri/renderD128")
    if (mode in ("auto", "vaapi") and Path(device).exists()
            and _ffmpeg_has_encoder("h264_vaapi")):
        return (["-vaapi_device", device],
                ["-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", "-qp", "24"])
    return [], []


def _soft_encode_args() -> list[str]:
    """软件编码参数：画质可接受的前提下尽量少占 CPU / 内存。

    实测 1440x2560 的 HEVC 源：默认 veryfast 吃 8.5 核 / 1.68GB；
    限 4 线程 + 关 lookahead 后降到 2.7 核 / 0.9GB。
    可用环境变量覆盖：VIDEO_FFMPEG_PRESET / VIDEO_FFMPEG_CRF / VIDEO_FFMPEG_THREADS。
    """
    args = ["-c:v", "libx264", "-preset", _SOFT_PRESET, "-crf", _SOFT_CRF,
            "-pix_fmt", "yuv420p", "-rc-lookahead", "0", "-refs", "1", "-bf", "2"]
    if _SOFT_THREADS > 0:
        args += ["-threads", str(_SOFT_THREADS)]
    return args + ["-c:a", "aac", "-b:a", "128k"]


async def normalize_video(
    src: Path, dst: Path, probe: dict | None = None,
    timeout: float = DEFAULT_MERGE_TIMEOUT,
) -> dict:
    """统一成 H.264/AAC 的 progressive MP4，并把 moov 前置（边下边播）。

    - 已是 H.264/AAC：只做 `-c copy -movflags +faststart` 重封装，不重编码；
    - 其它编码 / HLS 合并后的流：重编码成 libx264 + aac；
    - ffmpeg 缺失：原样返回 src，并在结果里标 `available=False`，调用方需显式
      降级并打日志，绝不静默当作已转码。

    返回 {path, transcoded, remuxed, available, reason?}
    """
    executable = ffmpeg_path()
    if not executable:
        return {"path": src, "transcoded": False, "remuxed": False,
                "available": False, "reason": "ffmpeg 不可用"}
    probe = probe or await probe_video(src)
    friendly = is_browser_friendly(probe)
    tail = ["-movflags", "+faststart", "-f", "mp4", str(dst)]
    head = [executable, "-hide_banner", "-loglevel", "error", "-y"]

    hw_global: list[str] = []
    if friendly:
        args = head + ["-i", str(src), "-c", "copy"] + tail
    else:
        hw_global, hw_out = _hw_encode_spec()
        encode = hw_out or _soft_encode_args()
        args = head + hw_global + ["-i", str(src)] + encode + tail

    code, detail = await _run(args, timeout=timeout)
    ok = code == 0 and dst.exists() and dst.stat().st_size > 0
    if not ok and hw_global:
        # 硬件编码失败（驱动/设备不支持）时自动回退软件编码，不让整条任务失败
        logger.warning("[normalize_video] 硬件编码失败，回退 libx264：%s",
                       detail[-200:])
        if dst.exists():
            dst.unlink(missing_ok=True)
        args = head + ["-i", str(src)] + _soft_encode_args() + tail
        code, detail = await _run(args, timeout=timeout)
        ok = code == 0 and dst.exists() and dst.stat().st_size > 0
    if not ok:
        raise MediaDownloadError(
            f"ffmpeg 归一化失败（退出码 {code}）: {detail[-500:]}")
    logger.info("[normalize_video] %s -> %s (%d bytes, transcoded=%s)",
                src.name, dst.name, dst.stat().st_size, not friendly)
    return {"path": dst, "transcoded": not friendly, "remuxed": friendly,
            "available": True}


def is_available() -> bool:
    """本机是否具备 ffmpeg"""
    return ffmpeg_path() is not None


async def merge_audio_video(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    timeout: float = DEFAULT_MERGE_TIMEOUT,
) -> None:
    """把独立的视频轨与音频轨无损封装成 mp4。

    必须使用 ``asyncio.create_subprocess_exec``：爬虫的采集与评论任务跑在同一个
    事件循环上，同步的 ``subprocess.run`` 会阻塞整个循环数十秒。

    :raises MediaDownloadError: ffmpeg 不存在、超时或返回非 0
    """
    executable = ffmpeg_path()
    if not executable:
        raise MediaDownloadError("未检测到 ffmpeg，无法合流 DASH 音视频")

    process = await asyncio.create_subprocess_exec(
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(audio_path),
        "-c",
        "copy",
        str(output_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise MediaDownloadError(f"ffmpeg 合流超时（{timeout}s）") from exc
    except BaseException:
        # 外部取消（Ctrl-C / 任务被 abort）不会走上面的超时分支，
        # 这里必须显式回收子进程，否则 ffmpeg 会变成孤儿继续跑满整个编解码任务
        if process.returncode is None:
            process.kill()
            # 等待子进程真正退出：否则 transport 会留到事件循环关闭后才被 GC 并报错。
            # 取消状态下这个 await 可能再次被取消，吞掉即可，原始异常照常抛出
            with contextlib.suppress(BaseException):
                await process.wait()
        raise

    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace")[-500:] if stderr else ""
        raise MediaDownloadError(f"ffmpeg 合流失败（退出码 {process.returncode}）: {detail}")

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise MediaDownloadError("ffmpeg 合流产物为空")

    logger.info(
        "[merge_audio_video] 合流完成: %s (%d bytes)",
        output_path.name,
        output_path.stat().st_size,
    )
