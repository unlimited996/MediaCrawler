# -*- coding: utf-8 -*-
"""极简 Nacos 服务注册 / 发现客户端（零第三方依赖，兼容 Nacos 1.x/2.x/3.x）。

为什么要自己写
--------------
官方 `nacos-sdk-python` 依赖多、容器里还要多装包，而且对「Nacos 没起」的降级
行为不好控制。我们只需要 4 件事：注册、保活、摘除、查询实例。

API 版本差异（实测 Nacos 3.2.4）
-------------------------------
* **3.x**：命名服务拆成「客户端 API」`/nacos/v3/client/ns/**`
  （客户端接口不需要 admin 鉴权；`/nacos/v3/admin/**` 才要登录）。
  **没有 beat 接口**（PUT/POST `/instance/beat` 都返回 501）。
* **1.x/2.x**：`/nacos/v1/ns/**`，有心跳 `PUT /instance/beat`。

所以这里做两套适配：
* 写操作用 `POST/DELETE /instance`（v3/ v1 参数一致）；
* **保活**：v3 下每 `heartbeat_sec` 秒**重新注册一次**（实测：间隔 5s 可稳定保持
  healthy，停 20s 后变 unhealthy、30s 后被摘除），v1 下走 beat 接口。

设计原则：**永不因为 Nacos 挂了而影响主服务**
  * 注册失败只打日志，服务照常启动（`register()` 返回 False 而不是抛异常）；
  * 查询失败返回空列表，调用方自己决定走静态地址兜底；
  * 保活线程是 daemon，进程退出即停。

环境变量（全部可选，见 config/base_config.py）：
    NACOS_SERVER       www.joyeuse.cn:9000   ← 注意是 **8848/OpenAPI 端口**，不是控制台 8085
    NACOS_NAMESPACE    public
    NACOS_GROUP        DEFAULT_GROUP
    NACOS_USERNAME / NACOS_PASSWORD   （只有 admin 接口需要，client 接口不用）
    NACOS_API          auto / v3 / v1
    NACOS_ENABLED      1 / 0
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

#: 单次请求超时（秒）。Nacos 是旁路组件，宁可慢一步也不能拖住业务线程
_TIMEOUT = 3.0


def _log(msg: str) -> None:
    print(f"[nacos] {msg}", flush=True)


class NacosClient:
    """最小可用的 Nacos 客户端（v3 客户端 API / v1 OpenAPI 自适应）。"""

    #: 各版本接口前缀
    _PATHS = {
        "v3": {"instance": "/nacos/v3/client/ns/instance",
               "list": "/nacos/v3/client/ns/instance/list"},
        "v1": {"instance": "/nacos/v1/ns/instance",
               "list": "/nacos/v1/ns/instance/list",
               "beat": "/nacos/v1/ns/instance/beat"},
    }

    def __init__(self, server: str, namespace: str = "public",
                 group: str = "DEFAULT_GROUP",
                 username: str = "", password: str = "",
                 api: str = "auto", heartbeat_sec: float = 5.0):
        self.server = (server or "").strip().rstrip("/")
        if self.server and not self.server.startswith("http"):
            self.server = "http://" + self.server
        self.namespace = namespace or "public"
        self.group = group or "DEFAULT_GROUP"
        self.username = username or ""
        self.password = password or ""
        self.heartbeat_sec = max(2.0, float(heartbeat_sec or 5))
        self._api_pref = (api or "auto").lower()
        self._api_detected: Optional[str] = None
        self._running = False
        self._stopping = threading.Event()
        self._monitors: Dict[str, threading.Thread] = {}
        self._token: str = ""

    # ---------------- 基础 ----------------

    @property
    def enabled(self) -> bool:
        return bool(self.server)

    @property
    def api_version(self) -> str:
        if self._api_detected:
            return self._api_detected
        if self._api_pref in ("v1", "v3"):
            self._api_detected = self._api_pref
        return self._api_detected or "unknown"

    def _url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        q = dict(params or {})
        if self.namespace:
            q.setdefault("namespaceId", self.namespace)
        if self.group:
            q.setdefault("groupName", self.group)
        qs = urllib.parse.urlencode({k: v for k, v in q.items() if v is not None})
        return f"{self.server}{path}" + (f"?{qs}" if qs else "")

    def _request(self, method: str, path: str,
                 params: Optional[Dict[str, Any]] = None,
                 body: Optional[Dict[str, Any]] = None,
                 timeout: float = _TIMEOUT) -> Tuple[int, str]:
        """发一个请求，返回 (status, text)；网络失败返回 (0, "")。"""
        if not self.enabled:
            return 0, ""
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(self._url(path, params), data=data,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read().decode("utf-8", "replace")
            except Exception:
                return e.code, ""
        except Exception:
            return 0, ""

    @staticmethod
    def _json(text: str) -> Optional[Any]:
        try:
            return json.loads(text) if text else None
        except Exception:
            return None

    def _ok(self, status: int, text: str) -> bool:
        """判定写操作是否成功：v3 看 code==0，v1 看返回 "ok"。"""
        if status == 0:
            return False
        payload = self._json(text)
        if isinstance(payload, dict):
            if "code" in payload:
                return int(payload.get("code", -1)) == 0
            # v1 的异常结构
            return status == 200
        return status == 200 and (text or "").strip().lower() in ("ok", "true")

    def _detect(self, service: str) -> str:
        """探测服务端 API 版本（结果缓存）。"""
        if self._api_detected:
            return self._api_detected
        if self._api_pref in ("v1", "v3"):
            self._api_detected = self._api_pref
            return self._api_detected
        for ver in ("v3", "v1"):
            status, text = self._request(
                "GET", self._PATHS[ver]["list"],
                params={"serviceName": service or "__probe__", "healthyOnly": "false"})
            if status == 200 and self._json(text) is not None:
                self._api_detected = ver
                _log(f"服务端 API 版本探测：{ver}（{self.server}）")
                return ver
        return "v1"

    # ---------------- 鉴权（仅 admin 接口需要）----------------

    def login(self) -> bool:
        """Nacos 开启鉴权时换 accessToken；客户端 API 通常不需要。"""
        if not self.username:
            return False
        for path in ("/nacos/v3/auth/user/login", "/nacos/v1/auth/login"):
            status, text = self._request("POST", path, body={
                "username": self.username, "password": self.password})
            payload = self._json(text) or {}
            token = ""
            if isinstance(payload, dict):
                token = payload.get("accessToken") or ""
                data = payload.get("data")
                if not token and isinstance(data, dict):
                    token = data.get("accessToken") or ""
            if token:
                self._token = token
                return True
        return False

    # ---------------- 注册 / 摘除 ----------------

    def register(self, service: str, ip: str, port: int,
                 ephemeral: bool = True, weight: float = 1.0,
                 metadata: Optional[Dict[str, str]] = None) -> bool:
        if not self.enabled:
            return False
        ver = self._detect(service)
        body = {
            "serviceName": service,
            "ip": ip,
            "port": port,
            "weight": weight,
            "ephemeral": "true" if ephemeral else "false",
            "enabled": "true",
            "healthy": "true",
            "clusterName": "DEFAULT",
        }
        if metadata:
            body["metadata"] = json.dumps(metadata, ensure_ascii=False)

        status, text = self._request("POST", self._PATHS[ver]["instance"], body=body)
        ok = self._ok(status, text)
        if ok:
            _log(f"已注册 {service} -> {ip}:{port}"
                 f"（api={ver} group={self.group} ns={self.namespace}）")
            self._start_keepalive(service, ip, port, ephemeral, weight, metadata)
        else:
            _log(f"注册失败 {service} -> {ip}:{port}（HTTP {status}），"
                 f"服务继续启动，不影响业务")
        return ok

    def deregister(self, service: str, ip: str, port: int,
                   ephemeral: bool = True) -> bool:
        if not self.enabled:
            return False
        ver = self._detect(service)
        status, text = self._request(
            "DELETE", self._PATHS[ver]["instance"],
            params={"serviceName": service, "ip": ip, "port": port,
                    "ephemeral": "true" if ephemeral else "false",
                    "clusterName": "DEFAULT"})
        ok = self._ok(status, text)
        if ok:
            self._stop_keepalive(service, ip, port)
        return ok

    # ---------------- 保活 ----------------
    #  v3 没有 beat 接口 → 定时「重新注册」既刷新健康状态又续期
    #  v1 有 beat     → 走官方心跳，语义更准

    def _start_keepalive(self, service: str, ip: str, port: int,
                         ephemeral: bool, weight: float,
                         metadata: Optional[Dict[str, str]]) -> None:
        if not ephemeral:
            return
        key = f"{service}@{ip}:{port}"
        if key in self._monitors and self._monitors[key].is_alive():
            return
        ver = self.api_version
        beat_path = self._PATHS["v1"].get("beat")
        beat_body = json.dumps({"serviceName": service, "ip": ip, "port": port,
                                "cluster": "DEFAULT", "weight": weight})
        reg_body = {
            "serviceName": service, "ip": ip, "port": port,
            "weight": weight, "ephemeral": "true", "enabled": "true",
            "healthy": "true", "clusterName": "DEFAULT",
        }
        if metadata:
            reg_body["metadata"] = json.dumps(metadata, ensure_ascii=False)

        def _loop() -> None:
            while not self._stopping.is_set():
                self._stopping.wait(self.heartbeat_sec)
                if self._stopping.is_set():
                    break
                if ver == "v1" and beat_path:
                    status, text = self._request(
                        "PUT", beat_path,
                        params={"serviceName": service, "beat": beat_body})
                    # v1 心跳返回 404 说明实例已被摘掉（Nacos 重启/过期）→ 重新注册
                    if status == 200 and "ok" in (text or "").lower():
                        continue
                self._request("POST", self._PATHS[ver]["instance"], body=reg_body)

        t = threading.Thread(target=_loop, name=f"nacos-keepalive-{service}",
                             daemon=True)
        t.start()
        self._monitors[key] = t

    def _stop_keepalive(self, service: str, ip: str, port: int) -> None:
        key = f"{service}@{ip}:{port}"
        self._monitors.pop(key, None)

    def stop(self) -> None:
        self._stopping.set()

    # ---------------- 发现 ----------------

    def discover(self, service: str) -> List[Dict[str, Any]]:
        """查实例列表，返回 [{'ip','port','healthy','weight'}...]；失败返回空。"""
        if not self.enabled:
            return []
        ver = self._detect(service)
        status, text = self._request(
            "GET", self._PATHS[ver]["list"],
            params={"serviceName": service, "healthyOnly": "false"})
        if status != 200:
            return []
        payload = self._json(text)
        if isinstance(payload, dict):
            hosts = payload.get("data") if "data" in payload else payload.get("hosts")
        else:
            hosts = payload
        out: List[Dict[str, Any]] = []
        for h in (hosts or []):
            try:
                if not h.get("enabled", True):
                    continue
                out.append({
                    "ip": h.get("ip"),
                    "port": int(h.get("port")),
                    "healthy": bool(h.get("healthy", True)),
                    "weight": float(h.get("weight") or 1),
                })
            except Exception:
                continue
        out.sort(key=lambda x: (not x["healthy"], -x["weight"]))
        return out

    def services(self) -> List[str]:
        """服务名列表（只有 admin 接口有，需要鉴权；拿不到就返回空）。"""
        if not self.username and not self._token:
            return []
        for path in ("/nacos/v3/admin/ns/service/list",
                     "/nacos/v1/ns/service/list"):
            params: Dict[str, Any] = {"pageNo": 1, "pageSize": 200}
            if self._token:
                params["accessToken"] = self._token
            status, text = self._request("GET", path, params=params)
            if status != 200:
                continue
            payload = self._json(text) or {}
            data = payload.get("data") if "data" in payload else payload
            if isinstance(data, dict):
                return list(data.get("doms") or data.get("services") or [])
            if isinstance(data, list):
                return [str(x) for x in data]
        return []


# ---------------------------------------------------------------------------
# 单例 + 便捷函数
# ---------------------------------------------------------------------------

_client: Optional[NacosClient] = None
_lock = threading.Lock()


def get_client() -> NacosClient:
    """按环境变量构建全局客户端（只建一次）。"""
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is None:
            _client = NacosClient(
                server=os.getenv("NACOS_SERVER", "www.joyeuse.cn:9000"),
                namespace=os.getenv("NACOS_NAMESPACE", "public"),
                group=os.getenv("NACOS_GROUP", "DEFAULT_GROUP"),
                username=os.getenv("NACOS_USERNAME", ""),
                password=os.getenv("NACOS_PASSWORD", ""),
                api=os.getenv("NACOS_API", "auto"),
                heartbeat_sec=float(os.getenv("NACOS_HEARTBEAT_SEC", "5") or 5),
            )
    return _client


def local_ip() -> str:
    """探测本机对外 IP（容器里即容器 IP），失败回退 127.0.0.1。

    不能用 gethostbyname(gethostname())：容器/多网卡下常返回 127.0.1.1。
    """
    for target in ("8.8.8.8", "1.1.1.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect((target, 80))
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith("127."):
                return ip
        except Exception:
            continue
    return "127.0.0.1"
