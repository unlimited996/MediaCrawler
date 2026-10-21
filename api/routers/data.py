# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/api/routers/data.py
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

import os
import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

router = APIRouter(prefix="/data", tags=["data"])

# Data directory
DATA_DIR = Path(__file__).parent.parent.parent / "data"


def get_file_info(file_path: Path) -> dict:
    """Get file information"""
    stat = file_path.stat()
    record_count = None

    # Try to get record count
    try:
        if file_path.suffix == ".json":
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    record_count = len(data)
        elif file_path.suffix == ".jsonl":
            # 每行一条记录（默认存储格式），空行不计
            with open(file_path, "r", encoding="utf-8") as f:
                record_count = sum(1 for line in f if line.strip())
        elif file_path.suffix == ".csv":
            with open(file_path, "r", encoding="utf-8") as f:
                record_count = sum(1 for _ in f) - 1  # Subtract header row
    except Exception:
        pass

    return {
        "name": file_path.name,
        "path": str(file_path.relative_to(DATA_DIR)),
        "size": stat.st_size,
        "modified_at": stat.st_mtime,
        "record_count": record_count,
        "type": file_path.suffix[1:] if file_path.suffix else "unknown"
    }


@router.get("/files")
async def list_data_files(platform: Optional[str] = None, file_type: Optional[str] = None):
    """Get data file list"""
    if not DATA_DIR.exists():
        return {"files": []}

    files = []
    supported_extensions = {".json", ".jsonl", ".csv", ".xlsx", ".xls"}

    for root, dirs, filenames in os.walk(DATA_DIR):
        root_path = Path(root)
        for filename in filenames:
            file_path = root_path / filename
            if file_path.suffix.lower() not in supported_extensions:
                continue

            # Platform filter
            if platform:
                rel_path = str(file_path.relative_to(DATA_DIR))
                if platform.lower() not in rel_path.lower():
                    continue

            # Type filter
            if file_type and file_path.suffix[1:].lower() != file_type.lower():
                continue

            try:
                files.append(get_file_info(file_path))
            except Exception:
                continue

    # Sort by modification time (newest first)
    files.sort(key=lambda x: x["modified_at"], reverse=True)

    return {"files": files}


@router.get("/files/{file_path:path}")
async def get_file_content(file_path: str, preview: bool = True, limit: int = 100):
    """Get file content or preview"""
    full_path = DATA_DIR / file_path

    if not full_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    if not full_path.is_file():
        raise HTTPException(status_code=400, detail="Not a file")

    # Security check: ensure within DATA_DIR
    try:
        full_path.resolve().relative_to(DATA_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")

    if preview:
        # Return preview data
        try:
            if full_path.suffix == ".json":
                with open(full_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return {"data": data[:limit], "total": len(data)}
                    return {"data": data, "total": 1}
            elif full_path.suffix == ".jsonl":
                # 每行一条 JSON（项目默认存储格式）
                rows = []
                total = 0
                with open(full_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        total += 1
                        if len(rows) < limit:
                            try:
                                rows.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue
                return {"data": rows, "total": total}
            elif full_path.suffix == ".csv":
                import csv
                with open(full_path, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    rows = []
                    for i, row in enumerate(reader):
                        if i >= limit:
                            break
                        rows.append(row)
                    # Re-read to get total count
                    f.seek(0)
                    total = sum(1 for _ in f) - 1
                    return {"data": rows, "total": total}
            elif full_path.suffix.lower() in (".xlsx", ".xls"):
                import pandas as pd
                # Read first limit rows
                df = pd.read_excel(full_path, nrows=limit)
                # Get total row count (only read first column to save memory)
                df_count = pd.read_excel(full_path, usecols=[0])
                total = len(df_count)
                # Convert to list of dictionaries, handle NaN values
                rows = df.where(pd.notnull(df), None).to_dict(orient='records')
                return {
                    "data": rows,
                    "total": total,
                    "columns": list(df.columns)
                }
            else:
                raise HTTPException(status_code=400, detail="Unsupported file type for preview")
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON file")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    else:
        # Return file download
        return FileResponse(
            path=full_path,
            filename=full_path.name,
            media_type="application/octet-stream"
        )


@router.get("/download/{file_path:path}")
async def download_file(file_path: str):
    """Download file"""
    full_path = DATA_DIR / file_path

    if not full_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    if not full_path.is_file():
        raise HTTPException(status_code=400, detail="Not a file")

    # Security check
    try:
        full_path.resolve().relative_to(DATA_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")

    return FileResponse(
        path=full_path,
        filename=full_path.name,
        media_type="application/octet-stream"
    )


@router.delete("/files/{file_path:path}")
async def delete_data_file(file_path: str):
    """删除数据文件（仅限 data 目录内的单个文件，不可删目录）。"""
    full_path = DATA_DIR / file_path

    if not full_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if not full_path.is_file():
        raise HTTPException(status_code=400, detail="Not a file")

    # 安全校验：必须落在 DATA_DIR 内，防止 ../ 穿越
    try:
        full_path.resolve().relative_to(DATA_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        full_path.unlink()
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {e}")

    return {"success": True, "deleted": str(file_path)}


@router.get("/stats")
async def get_data_stats():
    """Get data statistics"""
    if not DATA_DIR.exists():
        return {"total_files": 0, "total_size": 0, "by_platform": {}, "by_type": {}}

    stats = {
        "total_files": 0,
        "total_size": 0,
        "by_platform": {},
        "by_type": {}
    }

    supported_extensions = {".json", ".jsonl", ".csv", ".xlsx", ".xls"}

    for root, dirs, filenames in os.walk(DATA_DIR):
        root_path = Path(root)
        for filename in filenames:
            file_path = root_path / filename
            if file_path.suffix.lower() not in supported_extensions:
                continue

            try:
                stat = file_path.stat()
                stats["total_files"] += 1
                stats["total_size"] += stat.st_size

                # Statistics by type
                file_type = file_path.suffix[1:].lower()
                stats["by_type"][file_type] = stats["by_type"].get(file_type, 0) + 1

                # Statistics by platform (inferred from path)
                rel_path = str(file_path.relative_to(DATA_DIR))
                for platform in ["xhs", "dy", "ks", "bili", "wb", "tieba", "zhihu"]:
                    if platform in rel_path.lower():
                        stats["by_platform"][platform] = stats["by_platform"].get(platform, 0) + 1
                        break
            except Exception:
                continue

    return stats


@router.get("/media/{platform}/{content_id}/{filename}")
async def serve_local_media(platform: str, content_id: str, filename: str):
    """服务本地已下载的媒体文件（图片/视频封面等）。

    存在的意义：小红书/B站等 CDN 有防盗链，浏览器 <img> 直连原始 URL
    会吃 403。爬虫下载到本地的文件通过这个路由吐出，详情页就能正常显示。
    """
    # 防目录穿越：只允许纯文件名
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="非法文件名")
    if "/" in content_id or "\\" in content_id or content_id.startswith("."):
        raise HTTPException(status_code=400, detail="非法内容ID")

    file_path = DATA_DIR / platform / "media" / content_id / filename
    try:
        file_path = file_path.resolve()
        base = (DATA_DIR / platform / "media").resolve()
        file_path.relative_to(base)   # 不在许可目录内则抛 ValueError
    except (ValueError, OSError):
        raise HTTPException(status_code=400, detail="非法路径")

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    # 媒体文件不可变（按内容ID+序号命名），允许长缓存
    return FileResponse(
        file_path,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/media/placeholder")
async def media_placeholder(text: str = "无图", w: int = 300, h: int = 400):
    """占位图：外链失效（原帖删除 / CDN 签名过期）且无本地副本时兜底。

    返回一张内联 SVG，避免列表/详情页出现浏览器默认的破图图标。
    """
    w = max(16, min(int(w or 300), 1024))
    h = max(16, min(int(h or 400), 1024))
    label = (text or "无图")[:12].replace("<", "").replace("&", "")
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}">'
        f'<rect width="100%" height="100%" fill="#1a1d29"/>'
        f'<rect x="1" y="1" width="{w-2}" height="{h-2}" fill="none" '
        f'stroke="#2a2f45" stroke-width="2" rx="8"/>'
        f'<text x="50%" y="50%" fill="#5b6480" font-family="monospace" '
        f'font-size="{max(12, min(w, h) // 10)}" text-anchor="middle" '
        f'dominant-baseline="middle">{label}</text>'
        f"</svg>"
    )
    return Response(content=svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=3600"})


@router.get("/avatar-proxy")
async def avatar_proxy(url: str):
    """代理平台头像图片。

    头像 CDN（如 xhscdn）与图片 CDN 一样有防盗链，浏览器 <img> 直连吃 403。
    本路由在服务端带头部 cookie 请求后转发（实测 200），前端用它显示头像。

    安全：仅允许 http/https 目标；单次请求超时 15s；大小上限 5MB。
    """
    from urllib.parse import urlparse

    import httpx

    from account.crypto import decrypt_cookie

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=400, detail="非法 URL")

    # 按域名猜平台，取对应账号池 cookie（没有就匿名请求）
    cookie = ""
    host = parsed.netloc.lower()
    platform_guess = None
    if "xhscdn" in host or "xiaohongshu" in host:
        platform_guess = "xhs"
    elif "douyin" in host or "douyinvod" in host:
        platform_guess = "dy"
    elif "zhimg" in host or "zhihu" in host:
        platform_guess = "zhihu"
    elif "weibo" in host or "sinaimg" in host:
        platform_guess = "wb"

    if platform_guess:
        try:
            from sqlalchemy import select
            from sqlalchemy.ext.asyncio import AsyncSession

            from config.db_config import resolve_config_db_type
            from database.db_session import get_async_engine
            from database.models_v2 import Account

            engine = get_async_engine(resolve_config_db_type())
            if engine is not None:
                try:
                    async with AsyncSession(engine, expire_on_commit=False) as session:
                        row = (await session.execute(
                            select(Account).where(Account.platform == platform_guess,
                                                  Account.status == "active")
                        )).scalars().first()
                        if row is not None and row.cookie_cipher:
                            cookie = decrypt_cookie(row.cookie_cipher)
                finally:
                    # 必须 dispose：否则连接不还池，连续请求会耗尽连接池导致超时
                    await engine.dispose()
        except Exception as e:
            print(f"[avatar_proxy] 取 cookie 失败: {e}")

    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36"),
        "Referer": "https://www.xiaohongshu.com/",
    }
    if cookie:
        headers["Cookie"] = cookie

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"上游请求失败: {e}")

    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="上游拒绝")

    if len(resp.content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="图片过大")

    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "public, max-age=86400"},
    )
