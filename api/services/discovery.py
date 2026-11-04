# -*- coding: utf-8 -*-
"""服务发现与跨服务调用（Nacos）。

对外只暴露 5 个东西：
    register_self()          进程启动时把自己注册到 Nacos
    shutdown()               进程退出时摘除
    discover(service)        拿实例列表（Nacos 优先，静态地址兜底）
    call(service, ...)       跨服务 HTTP 调用（自动挑实例 + 失败重试下一个）
    status()                 给健康检查/网关卡用的快照

**核心约定：Nacos 只是旁路**。Nacos 挂了、没部署、鉴权错，都不能让主服务
起不来或接口 500 —— 所有失败都降级为「用静态地址」或「返回空」。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

import config
from tools import nacos_client

_registered: List[tuple] = []          # [(service, ip, port)]
_register_lock = threading.Lock()
_rr: Dict[str, int] = {}               # 轮询游标
_rr_lock = threading.Lock()
_last_error: str = ""


def _log(msg: str) -> None:
    print(f"[discovery] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------

def register_self(service: Optional[str] = None,
                  port: Optional[int] = None) -> bool:
    """把自己注册进 Nacos。重复调用幂等。"""
    global _last_error
    if not getattr(config, "NACOS_ENABLED", True):
        _log("NACOS_ENABLED=0，跳过注册（用静态地址互联）")
        return False
    name = service or getattr(config, "SERVICE_NAME", "mediacrawler")
    p = int(port or getattr(config, "SERVICE_PORT", 8081))
    ip = getattr(config, "SERVICE_IP", "") or nacos_client.local_ip()

    with _register_lock:
        if any(s == name and i == ip and pt == p for s, i, pt in _registered):
            return True
        client = nacos_client.get_client()
        ok = client.register(name, ip, p, metadata={"app": "mediacrawler"})
        if ok:
            _registered.append((name, ip, p))
        else:
            _last_error = f"注册 {name} 失败（Nacos={client.server}）"
        return ok


def shutdown() -> None:
    """进程退出时摘除实例（优雅下线，避免网关把流量打到已死实例）。"""
    if not _registered:
        return
    client = nacos_client.get_client()
    for name, ip, p in list(_registered):
        try:
            client.deregister(name, ip, p)
            _log(f"已摘除 {name} -> {ip}:{p}")
        except Exception:
            pass
    _registered.clear()
    try:
        client.stop()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 发现
# ---------------------------------------------------------------------------

def _static_base(service: str) -> str:
    """Nacos 不可用时的静态兜底地址。"""
    if service == getattr(config, "CRAWLER_SERVICE_NAME", "mediacrawler-crawler"):
        return getattr(config, "CRAWLER_BASE_URL", "http://127.0.0.1:8001")
    if service == getattr(config, "DATA_SERVICE_NAME", "mediacrawler-data"):
        return getattr(config, "DATA_BASE_URL", "http://127.0.0.1:8081")
    return ""


def discover(service: str) -> List[str]:
    """返回可用实例的 base url 列表（形如 http://10.0.0.2:8001）。"""
    global _last_error
    if getattr(config, "NACOS_ENABLED", True):
        try:
            hosts = nacos_client.get_client().discover(service)
            urls = [f"http://{h['ip']}:{h['port']}" for h in hosts if h.get("healthy", True)]
            if urls:
                return urls
        except Exception as e:
            _last_error = f"发现 {service} 失败: {e}"
    fb = _static_base(service)
    return [fb] if fb else []


def pick(service: str) -> Optional[str]:
    """按轮询挑一个实例。"""
    urls = discover(service)
    if not urls:
        return None
    with _rr_lock:
        i = _rr.get(service, 0) % len(urls)
        _rr[service] = i + 1
    return urls[i]


# ---------------------------------------------------------------------------
# 调用
# ---------------------------------------------------------------------------

def call(service: str, method: str, path: str,
         json_body: Optional[Any] = None,
         timeout: Optional[float] = None,
         retry_next: bool = True) -> Dict[str, Any]:
    """跨服务调用。

    返回 {"ok": bool, "status": int, "data": Any, "error": str, "base": str}
    -- 不抛异常：调用方永远拿到结构化结果，自己决定降级。
    """
    urls = discover(service)
    if not urls:
        return {"ok": False, "status": 0, "data": None, "base": "",
                "error": f"没有可用实例（{service}）：Nacos 不可达且无静态地址"}

    to = float(timeout if timeout is not None else getattr(
        config, "SERVICE_CALL_TIMEOUT", 30))
    last_err = ""
    targets = urls if retry_next else urls[:1]
    for base in targets:
        url = base.rstrip("/") + "/" + path.lstrip("/")
        data = None
        headers = {}
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                raw = resp.read().decode("utf-8", "replace")
                try:
                    parsed = json.loads(raw) if raw else None
                except Exception:
                    parsed = raw
                return {"ok": 200 <= resp.status < 300, "status": resp.status,
                        "data": parsed, "error": "", "base": base}
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            return {"ok": False, "status": e.code, "data": None, "base": base,
                    "error": f"HTTP {e.code}: {body}"}
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            continue

    return {"ok": False, "status": 0, "data": None, "base": urls[0],
            "error": last_err or "调用失败"}


def notify_data_service(event: str, payload: Optional[Dict] = None) -> bool:
    """抓取侧 -> 数据侧的轻量通知（示例：一轮抓取跑完后让数据侧刷新缓存）。

    数据服务没起 / Nacos 没配 都不影响抓取主流程，只打一条警告。
    """
    svc = getattr(config, "DATA_SERVICE_NAME", "mediacrawler-data")
    res = call(svc, "POST", "/data/api/internal/events",
               json_body={"event": event, "payload": payload or {},
                          "ts": int(time.time())},
               timeout=5, retry_next=False)
    if not res["ok"]:
        _log(f"通知数据服务失败（不影响抓取）：{res['error']}")
    return bool(res["ok"])


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------

def status() -> Dict[str, Any]:
    client = nacos_client.get_client()
    return {
        "nacos_enabled": bool(getattr(config, "NACOS_ENABLED", True)),
        "nacos_server": client.server,
        "namespace": client.namespace,
        "group": client.group,
        "registered": [{"service": s, "ip": i, "port": p} for s, i, p in _registered],
        "crawler_instances": discover(getattr(config, "CRAWLER_SERVICE_NAME",
                                              "mediacrawler-crawler"))
        if getattr(config, "NACOS_ENABLED", True) else [],
        "last_error": _last_error,
    }
