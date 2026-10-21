# -*- coding: utf-8 -*-
"""定时拉取任务 API。"""

from typing import Any, Dict, List

from fastapi import APIRouter, Body, HTTPException

from ..schemas.scheduler import (ScheduledTaskCreate, ScheduledTaskOut,
                                 ScheduledTaskRunOut, ScheduledTaskUpdate,
                                 SchedulerStatusOut)
from ..services.task_scheduler import task_scheduler

router = APIRouter(prefix="/scheduler", tags=["scheduler"])


@router.get("/status", response_model=SchedulerStatusOut)
async def get_status():
    """调度器运行状态（是否启动、任务数、并发上限等）。"""
    return await task_scheduler.get_status()


@router.get("/tasks", response_model=List[ScheduledTaskOut])
async def list_tasks():
    """定时任务列表。"""
    return await task_scheduler.list_tasks()


@router.post("/tasks", response_model=ScheduledTaskOut)
async def create_task(payload: ScheduledTaskCreate):
    """创建定时任务。"""
    data = payload.model_dump()
    # options 里的枚举转成字符串，落库前统一形态
    opts = data.get("options") or {}
    data["options"] = {
        "login_type": getattr(opts.get("login_type"), "value", opts.get("login_type")),
        "enable_comments": bool(opts.get("enable_comments", True)),
        "enable_sub_comments": bool(opts.get("enable_sub_comments", False)),
        "enable_media": bool(opts.get("enable_media", False)),
        "save_option": getattr(opts.get("save_option"), "value", opts.get("save_option")),
        "headless": bool(opts.get("headless", True)),
        "cookies": opts.get("cookies") or "",
        "max_comments_count": opts.get("max_comments_count"),
    }
    data["platform"] = payload.platform.value
    try:
        return await task_scheduler.create_task(data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/tasks/{task_id}", response_model=ScheduledTaskOut)
async def get_task(task_id: int):
    task = await task_scheduler.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@router.put("/tasks/{task_id}", response_model=ScheduledTaskOut)
async def update_task(task_id: int, payload: ScheduledTaskUpdate):
    data = {k: v for k, v in payload.model_dump(exclude_unset=True).items()}
    if "options" in data and isinstance(data["options"], dict):
        opts = data["options"]
        data["options"] = {
            "login_type": getattr(opts.get("login_type"), "value", opts.get("login_type")),
            "enable_comments": bool(opts.get("enable_comments", True)),
            "enable_sub_comments": bool(opts.get("enable_sub_comments", False)),
            "enable_media": bool(opts.get("enable_media", False)),
            "save_option": getattr(opts.get("save_option"), "value", opts.get("save_option")),
            "headless": bool(opts.get("headless", True)),
            "cookies": opts.get("cookies") or "",
            "max_comments_count": opts.get("max_comments_count"),
        }
    try:
        task = await task_scheduler.update_task(task_id, data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: int):
    ok = await task_scheduler.delete_task(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"status": "ok", "deleted": task_id}


@router.post("/tasks/{task_id}/toggle", response_model=ScheduledTaskOut)
async def toggle_task(task_id: int, payload: Dict[str, Any] = Body(default={})):
    """启用 / 停用任务。body: {"enabled": true|false}"""
    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="enabled 必须是布尔值")
    task = await task_scheduler.set_enabled(task_id, enabled)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@router.post("/tasks/{task_id}/run")
async def run_task(task_id: int):
    """立即手动执行一次（不影响调度周期）。"""
    task = await task_scheduler.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    result = await task_scheduler.trigger(task_id)
    return result


@router.get("/tasks/{task_id}/runs", response_model=List[ScheduledTaskRunOut])
async def list_task_runs(task_id: int, limit: int = 50):
    """某个任务的执行记录。"""
    return await task_scheduler.list_runs(task_id=task_id, limit=limit)


@router.get("/runs", response_model=List[ScheduledTaskRunOut])
async def list_all_runs(limit: int = 100):
    """最近的执行记录（跨任务）。"""
    return await task_scheduler.list_runs(limit=limit)
