# -*- coding: utf-8 -*-
"""数据导出接口：目前支持把数据文件渲染成 PDF。"""
import re
import time
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/export", tags=["export"])

# 数据目录与导出目录（data/exports/）
DATA_DIR = Path(__file__).parent.parent.parent / "data"
EXPORT_DIR = DATA_DIR / "exports"

# 允许导出的后缀（与 data.py 的预览能力保持一致）
SUPPORTED_SUFFIX = {".json", ".jsonl", ".csv"}

# 导出文件名只允许这些字符，避免路径穿越
SAFE_NAME_RE = re.compile(r"^[\w.\-]+\.pdf$")


def _require_pdf_engine() -> None:
    """PDF 导出依赖服务端浏览器内核（Playwright 渲染 HTML）。

    为什么单独检查：library 镜像为了瘦身只装 requirements-core.txt，
    不含 Playwright。直接让 `import exporters.pdf_exporter` 抛 ImportError
    会被上层 `except Exception` 吞成 500「PDF export failed: …」，
    用户完全不知道该怎么办。这里提前给出可执行的处置建议。
    """
    try:
        import playwright  # noqa: F401
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail=(
                "当前部署是精简版 library 服务（不装浏览器内核），无法导出 PDF。"
                "可改用 CSV / JSON 格式导出；若必须导出 PDF，请部署 crawler "
                "或 all 镜像（含 Playwright）后再执行该操作。"
            ))


class ExportPdfRequest(BaseModel):
    file_path: str = Field(..., description="相对于 data 目录的数据文件路径")
    limit: int = Field(200, ge=1, le=200, description="最多导出多少条记录")
    embed_images: bool = Field(True, description="是否下载图片并内嵌到 PDF（离线可读）")
    platform: Optional[str] = Field("", description="平台标识，留空则按路径推断")


def _resolve_data_file(file_path: str) -> Path:
    full_path = (DATA_DIR / file_path).resolve()
    try:
        full_path.relative_to(DATA_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")
    if not full_path.exists() or not full_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    if full_path.suffix.lower() not in SUPPORTED_SUFFIX:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type for PDF export: {full_path.suffix}",
        )
    return full_path


@router.post("/pdf")
async def export_pdf(req: ExportPdfRequest):
    """把数据文件导出为 PDF"""
    _require_pdf_engine()

    from exporters.pdf_exporter import export_pdf as do_export

    source = _resolve_data_file(req.file_path)

    # 导出文件名：原文件名 + 时间戳，避免覆盖
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe_stem = re.sub(r"[^\w.\-]", "_", source.stem)[:60]
    out_name = f"{safe_stem}-{stamp}.pdf"
    out_path = EXPORT_DIR / out_name

    try:
        result = await do_export(
            file_path=source,
            out_path=out_path,
            limit=req.limit,
            platform=req.platform or "",
            embed_images=req.embed_images,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF export failed: {type(e).__name__}: {e}")

    return {
        "success": True,
        "name": result["name"],
        "size": result["size"],
        "record_count": result["record_count"],
        "image_count": result["image_count"],
        "download_url": f"/api/export/download/{result['name']}",
    }


class ExportStoriesRequest(BaseModel):
    ids: List[int] = Field(..., description="勾选的故事 ID 列表")
    embed_images: bool = Field(True, description="是否内嵌图片（离线可读）")


@router.post("/stories")
async def export_stories(req: ExportStoriesRequest):
    """按勾选的条目导出 PDF（数据管理页用）。"""
    _require_pdf_engine()

    from exporters.pdf_exporter import export_stories_pdf as do_export

    if not req.ids:
        raise HTTPException(status_code=400, detail="请先勾选要导出的数据")
    if len(req.ids) > 200:
        raise HTTPException(status_code=400, detail="一次最多导出 200 条")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_name = f"stories-{len(req.ids)}items-{stamp}.pdf"
    out_path = EXPORT_DIR / out_name

    try:
        result = await do_export(req.ids, out_path, embed_images=req.embed_images)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF export failed: {type(e).__name__}: {e}")

    return {
        "success": True,
        "name": result["name"],
        "size": result["size"],
        "record_count": result["record_count"],
        "image_count": result["image_count"],
        "download_url": f"/api/export/download/{result['name']}",
    }


@router.get("/download/{file_name}")
async def download_export(file_name: str):
    """下载已生成的 PDF"""
    if not SAFE_NAME_RE.match(file_name):
        raise HTTPException(status_code=400, detail="Invalid file name")

    full_path = (EXPORT_DIR / file_name).resolve()
    try:
        full_path.relative_to(EXPORT_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")
    if not full_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        path=full_path,
        filename=file_name,
        media_type="application/pdf",
    )
