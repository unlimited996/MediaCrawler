# -*- coding: utf-8 -*-
"""「安全验证」弹窗的 API 侧文件中转。

抓取子进程（worker 拉起的 ``python main.py``）与 crawler API 是不同进程，
但共用 ``logs/captcha_session/``（群晖 compose 里同一份 mc-logs 卷）。
这里不启动任何浏览器，只负责：读截图/状态给前端，把用户的点击、滑块拖拽、
输入写成指令文件，让抓取进程在**它自己那个命中验证的页面**上执行。
"""
from __future__ import annotations

import json
import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from tools import captcha_bridge as bridge

router = APIRouter(prefix="/crawler/captcha-session", tags=["captcha-session"])


def _active_info() -> Optional[dict]:
    f = bridge.ACTIVE_FILE
    # 用户已确认完成 / 会话已结束：即便 active.json 还没被后台任务删掉，
    # 也立刻视为结束，避免弹窗多闪一帧（后台循环 0.8s 才走一轮）。
    if bridge.DONE_FILE.exists():
        f.unlink(missing_ok=True)
        return None
    if not f.exists():
        return None
    try:
        info = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None
    age = time.time() - float(info.get("ts") or 0)
    if age > bridge.SESSION_TTL_SEC:
        # 抓取进程异常退出留下的僵尸会话，前端据此关闭弹窗
        f.unlink(missing_ok=True)
        return None
    return {"url": info.get("url") or "",
            "started_at": info.get("started_at") or "",
            "age_sec": int(age)}


@router.get("/status")
async def status():
    """前端每 2s 轮询；active 变 true 时自动弹出安全验证窗口。"""
    info = _active_info()
    if not info:
        return {"active": False, "message": "当前无需人工验证"}
    return {
        "active": True,
        "url": info["url"],
        "started_at": info["started_at"],
        "age_sec": info["age_sec"],
        "message": "抓取任务命中安全验证，请在下方完成滑块 / 扫码 / 点按",
    }


@router.get("/screenshot")
async def screenshot():
    """返回验证页最新一帧；抓取进程按 0.8s 原子替换。"""
    f = bridge.FRAME_FILE
    if not f.exists():
        raise HTTPException(status_code=404, detail="验证页截图尚未生成")
    return Response(
        content=f.read_bytes(),
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


class InputPayload(BaseModel):
    action: str = Field(description="click | drag | type | key | scroll | reload | done")
    x: float = 0
    y: float = 0
    x2: float = 0
    y2: float = 0
    text: str = ""
    key: str = ""
    dy: float = 0
    steps: int = 18


@router.post("/input")
async def input_cmd(payload: InputPayload):
    if payload.action not in bridge.ALLOWED_ACTIONS:
        raise HTTPException(status_code=400, detail=f"不支持的操作: {payload.action}")
    bridge.SESSION_DIR.mkdir(parents=True, exist_ok=True)
    with open(bridge.CMD_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload.model_dump(), ensure_ascii=False) + "\n")
    return {"ok": True}


@router.post("/done")
async def done():
    """用户点「我已完成验证」：立刻让抓取进程的等待循环放行。

    同时写 done.json（``wait_for_human_verify`` 的 done_check 读它）和一条
    done 指令（让后台截图任务收尾、关闭会话），双保险。
    """
    bridge.SESSION_DIR.mkdir(parents=True, exist_ok=True)
    with open(bridge.CMD_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"action": "done"}) + "\n")
    bridge.DONE_FILE.write_text(json.dumps({
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "reason": "user_confirmed_via_api",
    }, ensure_ascii=False), encoding="utf-8")
    bridge.ACTIVE_FILE.unlink(missing_ok=True)
    return {"ok": True, "message": "已通知抓取任务继续，登录态会自动回灌"}
