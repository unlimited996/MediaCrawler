# -*- coding: utf-8 -*-
"""定时拉取任务 API。"""

from typing import Any, Dict, List

from fastapi import APIRouter, Body, HTTPException

from ..schemas.scheduler import (ScheduledTaskCreate, ScheduledTaskOut,
                                 ScheduledTaskRunOut, ScheduledTaskUpdate,
                                 SchedulerStatusOut)
from ..services.task_scheduler import task_scheduler

router = APIRouter(prefix="/scheduler", tags=["scheduler"])


def _normalize_options(opts: Dict[str, Any]) -> Dict[str, Any]:
    """options 归一化：枚举转字符串，账号维度字段原样透传。"""
    behaviors = opts.get("behaviors") or ["likes", "collects"]
    behaviors = [str(b) for b in behaviors if str(b) in ("likes", "collects")]
    if not behaviors:
        behaviors = ["likes", "collects"]
    return {
        "login_type": getattr(opts.get("login_type"), "value", opts.get("login_type")),
        "enable_comments": bool(opts.get("enable_comments", True)),
        "enable_sub_comments": bool(opts.get("enable_sub_comments", False)),
        "enable_media": bool(opts.get("enable_media", False)),
        "save_option": getattr(opts.get("save_option"), "value", opts.get("save_option")),
        "headless": bool(opts.get("headless", True)),
        "cookies": opts.get("cookies") or "",
        "max_comments_count": opts.get("max_comments_count"),
        "behaviors": behaviors,
        "account_id": opts.get("account_id"),
        "account_name": opts.get("account_name"),
        # 抓取上限必须显式保留。原先这里是固定白名单，max_notes 不在其中 ——
        # 用户在「本次抓取上限」里填了 100 也会被静默丢弃，任务跑起来永远
        # 按配置默认走，于是「设了 100 却只更新 20 篇」（2026-10-29 修）。
        # build_crawler_env 用它同时驱动新帖额度与评论补抓预算，别删。
        "max_notes": opts.get("max_notes"),
        # 抓取模式：顶层 task.mode 列才是主来源，这里仅兼容存进 options 的情况
        "mode": opts.get("mode"),
        # 风控冷却豁免：撞验证码/限流熔断后，同账号在 risk_cooldown_hours 内
        # 默认不再发起；勾了这个选项才允许本次强制跑（2026-10-29）。
        # 同样必须显式保留 —— 白名单会静默丢弃未知键，用户勾了也存不住。
        "ignore_risk_cooldown": bool(opts.get("ignore_risk_cooldown", False)),
    }


@router.get("/status", response_model=SchedulerStatusOut)
async def get_status():
    """调度器运行状态（是否启动、任务数、并发上限等）。"""
    return await task_scheduler.get_status()


@router.get("/dimensions")
async def list_dimensions(platform: str = "xhs"):
    """任务维度元信息 + 账号维度的可选扫码登录账号。

    前端「新建任务」面板用它一次性拿到：三个维度的定义、账号池里
    可用于「登录用户」维度的账号列表（可能同时扫码登录多个）。
    """
    from account import AccountPool

    accounts: List[Dict[str, Any]] = []
    try:
        pool = AccountPool()
        rows = await pool.list_accounts(platform)
        accounts = [
            {
                "id": r["id"],
                "account_name": r.get("account_name") or f"账号{r['id']}",
                "status": r.get("status"),
                "health_score": r.get("health_score"),
                "has_cookie": r.get("has_cookie"),
            }
            for r in rows if r.get("has_cookie")
        ]
    except Exception:
        accounts = []

    return {
        "dimensions": [
            {"key": "author", "task_type": "profile",
             "target_kind": "profile_url"},
            {"key": "note", "task_type": "note",
             "target_kind": "note_url"},
            {"key": "account", "task_type": "account",
             "target_kind": "account_id",
             "behaviors": ["likes", "collects"]},
        ],
        "accounts": accounts,
    }


@router.get("/tasks", response_model=List[ScheduledTaskOut])
async def list_tasks():
    """定时任务列表。"""
    return await task_scheduler.list_tasks()


@router.post("/tasks", response_model=ScheduledTaskOut)
async def create_task(payload: ScheduledTaskCreate):
    """创建定时任务。"""
    data = payload.model_dump()
    # options 里的枚举转成字符串，落库前统一形态
    data["options"] = _normalize_options(data.get("options") or {})
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
        data["options"] = _normalize_options(data["options"])
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
async def run_task(task_id: int, force: bool = False):
    """立即手动执行一次（不影响调度周期）。

    force=True 表示本次忽略风控冷却强制执行：冷却期内（撞过验证码/限流）
    默认会直接跳过，手动点运行时可用 force 绕过；若仍撞验证码，
    爬虫会弹出浏览器等你人工过验证。
    """
    task = await task_scheduler.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    result = await task_scheduler.trigger(task_id, force=force)
    return result


@router.get("/tasks/{task_id}/runs", response_model=List[ScheduledTaskRunOut])
async def list_task_runs(task_id: int, limit: int = 50):
    """某个任务的执行记录。"""
    return await task_scheduler.list_runs(task_id=task_id, limit=limit)


@router.get("/runs", response_model=List[ScheduledTaskRunOut])
async def list_all_runs(limit: int = 100):
    """最近的执行记录（跨任务）。"""
    return await task_scheduler.list_runs(limit=limit)
