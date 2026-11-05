# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/api/routers/crawler.py
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

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException

from ..schemas import (CrawlerStartRequest, CrawlerStatusResponse, CrawlerTypeEnum,
                       QueueAddRequest, QueueListResponse)
from ..services import crawler_manager, crawl_queue

router = APIRouter(prefix="/crawler", tags=["crawler"])


@router.post("/start")
async def start_crawler(request: CrawlerStartRequest):
    """Start crawler task"""
    # 搜索模式必须给关键词：项目不提供默认关键词，避免"没填却抓了别的"
    if request.crawler_type == CrawlerTypeEnum.SEARCH and not (request.keywords or "").strip():
        raise HTTPException(
            status_code=400,
            detail="搜索模式必须提供关键词（keywords 不能为空）",
        )

    # 详情模式必须给 ID / 链接
    if request.crawler_type == CrawlerTypeEnum.DETAIL and not (request.specified_ids or "").strip():
        raise HTTPException(
            status_code=400,
            detail="详情模式必须提供笔记/视频 ID 或链接（specified_ids 不能为空）",
        )

    # 创作者模式必须给 ID
    if request.crawler_type == CrawlerTypeEnum.CREATOR and not (request.creator_ids or "").strip():
        raise HTTPException(
            status_code=400,
            detail="创作者模式必须提供创作者 ID（creator_ids 不能为空）",
        )

    result = await crawler_manager.start(request)
    mode = result.get("mode")

    # 正忙：排进队列，等前一个跑完自动续跑（不再直接 400 拒绝）
    if mode == "queued":
        queued = result.get("queued", 0)
        if queued == 0:
            raise HTTPException(
                status_code=400,
                detail="这些目标都已在队列中（或队列里没有可加入的目标）",
            )
        return {
            "status": "queued",
            "message": f"爬虫正在运行，已加入队列 {queued} 条，将按顺序执行",
            "queued": queued,
            "added": result.get("added", []),
            "duplicates": result.get("duplicates", []),
        }

    if mode != "started":
        # 前置检查失败（如数据库不可达）时返回具体原因，便于用户直接定位
        if crawler_manager.last_error:
            raise HTTPException(status_code=400, detail=crawler_manager.last_error)
        raise HTTPException(status_code=500, detail="Failed to start crawler")

    return {"status": "ok", "message": "Crawler started successfully"}


@router.post("/stop")
async def stop_crawler():
    """停止当前抓取（手动抓取 + 执行器里的定时任务都覆盖）。

    拆分成两个服务后，定时任务跑在独立 worker 进程里，crawler 进程够不着，
    所以这里除了停本地子进程，还会往共享目录投一个停止信号（见
    tools/crawl_stop.py），由 worker 自己把子进程杀掉。

    没有在跑时也返回 200（`stopped: false`），避免前端把"本来就没在跑"
    当成失败弹窗。
    """
    from ..services.crawl_control import stop_current_crawl

    return await stop_current_crawl()


@router.get("/status", response_model=CrawlerStatusResponse)
async def get_crawler_status():
    """Get crawler status"""
    return crawler_manager.get_status()


@router.get("/qrcode")
async def get_login_qrcode():
    """扫码登录的二维码（base64 data url）。

    后台服务里用户看不到浏览器窗口/PIL 弹窗，用这个接口把二维码显示在 WebUI 上。
    没有待扫的码（未到扫码步骤 / 已过期 / 已扫完）返回 {"qr": null}。
    """
    qr = crawler_manager.get_qrcode()
    if not qr:
        return {"qr": None}
    return {"qr": qr.get("image"), "at": qr.get("at")}


@router.get("/logs")
async def get_logs(limit: int = 100):
    """Get recent logs

    合并两个来源，保证「手动抓取」和「定时任务」在控制台是同一个视图：
      * 本进程内存缓冲（手动抓取，跑在 API 进程里）；
      * 抓取执行器（worker 进程）的实时日志文件 —— 拆分后定时任务日志
        必须跨进程读取，否则控制台会显得"定时任务没有输出"。
    """
    from datetime import datetime

    items: List[Dict[str, Any]] = [log.model_dump() for log in crawler_manager.logs]
    today = datetime.now().strftime("%Y-%m-%d")
    for it in items:
        it["_sort"] = f"{today} {it.get('timestamp') or '00:00:00'}"

    try:
        from ..services.worker_control import tail_live

        for e in tail_live(max(0, limit) or 100):
            ts = e.get("ts") or ""
            items.append({
                "id": 0,
                "timestamp": ts,
                "level": e.get("level") or "info",
                "message": e.get("msg") or "",
                "_sort": e.get("at") or f"{today} {ts}",
            })
    except Exception:
        pass

    items.sort(key=lambda d: d.get("_sort") or "")
    if limit > 0:
        items = items[-limit:]
    for idx, it in enumerate(items, 1):
        it["id"] = idx
        it.pop("_sort", None)
    return {"logs": items}


# ---------------------------------------------------------------------------
# 抓取队列：爬虫忙时提交的任务排队等待，前一个跑完自动按顺序续跑
# ---------------------------------------------------------------------------

def _split_status_param(status: Optional[str]) -> Optional[List[str]]:
    """把 ?status=done,failed 解析成列表"""
    if not status:
        return None
    vals = [s.strip() for s in status.split(",") if s.strip()]
    return vals or None


async def _kick_if_idle():
    """空闲且队列开着续跑时，立刻把队首任务拉起来（不必等下一次触发）。"""
    if not crawler_manager.is_running() and crawl_queue.auto_run:
        await crawler_manager.start_queue()


@router.get("/queue", response_model=QueueListResponse)
async def get_queue(status: Optional[str] = None, limit: int = 200):
    """队列列表（默认返回全部，含已结束的历史）。"""
    items = await crawl_queue.snapshot(statuses=_split_status_param(status), limit=limit)
    stats = await crawl_queue.stats()
    return QueueListResponse(items=items, auto_run=crawl_queue.auto_run,
                             batch_size=crawl_queue.batch_size, **stats)


@router.post("/queue/add")
async def add_to_queue(payload: QueueAddRequest):
    """往队列追加目标。空闲时立即开跑，正忙时排队等前一个跑完。"""
    if not payload.urls:
        raise HTTPException(status_code=400, detail="urls 不能为空")

    options = payload.model_dump(exclude={"urls", "platform", "crawler_type"})
    # 枚举转回字符串，方便队列内部做配置签名比对
    for key in ("login_type", "save_option"):
        if key in options and hasattr(options[key], "value"):
            options[key] = options[key].value

    result = await crawl_queue.add(payload.urls, payload.platform.value,
                                   payload.crawler_type.value, options)
    if result["added"]:
        await _kick_if_idle()

    return {
        "status": "ok",
        "added": result["added"],
        "duplicates": result["duplicates"],
        "message": f"已加入队列 {len(result['added'])} 条"
        + (f"，跳过重复 {len(result['duplicates'])} 条" if result["duplicates"] else ""),
    }


@router.delete("/queue/{item_id}")
async def remove_queue_item(item_id: int):
    """删除队列里的一条任务（正在执行的不允许删）。"""
    removed = await crawl_queue.remove([item_id])
    if not removed:
        item = next((i for i in await crawl_queue.snapshot() if i.id == item_id), None)
        if item is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        raise HTTPException(status_code=400, detail=f"任务正在执行中（{item.status}），无法删除")
    return {"status": "ok", "removed": item_id}


@router.post("/queue/clear")
async def clear_queue(payload: Dict[str, Any] = Body(default={})):
    """清空队列。body: {"statuses": ["done","failed"]}，不传则清所有非执行中的任务。"""
    statuses = payload.get("statuses") if isinstance(payload, dict) else None
    if statuses is not None and not isinstance(statuses, list):
        raise HTTPException(status_code=400, detail="statuses 必须是数组")
    removed = await crawl_queue.clear(statuses)
    return {"status": "ok", "removed": len(removed), "ids": removed}


@router.post("/queue/retry")
async def retry_queue(payload: Dict[str, Any] = Body(default={})):
    """把失败/取消的任务重新排队。body: {"ids": [1,2]} 或 {"all": true}。"""
    ids: List[int] = []
    if isinstance(payload, dict):
        if payload.get("all"):
            failed = await crawl_queue.snapshot(statuses=["failed", "cancelled"])
            ids = [i.id for i in failed]
        else:
            raw = payload.get("ids") or []
            if not isinstance(raw, list):
                raise HTTPException(status_code=400, detail="ids 必须是数组")
            ids = [int(v) for v in raw if str(v).lstrip("-").isdigit()]
    requeued = await crawl_queue.requeue(ids)
    if requeued:
        await _kick_if_idle()
    return {"status": "ok", "requeued": len(requeued), "ids": requeued}


@router.post("/queue/resume")
async def resume_queue():
    """手动恢复队列续跑（点过「终止」后想继续跑剩下的任务时用）。"""
    result = await crawler_manager.start_queue()
    if result.get("mode") == "running":
        return {"status": "ok", "message": "爬虫正在运行，无需手动触发"}
    if result.get("mode") == "empty":
        return {"status": "ok", "message": result.get("message", "队列为空"),
                "pending": result.get("pending", 0)}
    return {"status": "ok", "message": "已开始执行队列任务"}
