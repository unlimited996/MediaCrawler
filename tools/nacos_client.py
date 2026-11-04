# -*- coding: utf-8 -*-
"""极简 Nacos 服务注册 / 发现客户端（零第三方依赖）。

为什么不直接用 `nacos-sdk-python`：
  * 官方 SDK 依赖较多、在容器里要额外装包，且对「Nacos 没起」时的降级
    行为不好控制；
  * 我们只需要 4 个能力：注册、心跳、摘除、查询实例。Nacos 的 **OpenAPI
    (v1)** 足够，而且同一套接口在 Nacos 1.x / 2.x 上都能用。

设计原则：**永不因为 Nacos 挂了而影响主服务**
  * 注册失败只打日志，服务照常启动（`register()` 返回 False 而不是抛异常）；
  * 查询失败返回空列表，调用方自己决定走静态地址兜底；
  * 心跳线程是 daemon，进程退出即停。

环境变量（全部可选，见 config/base_config.py）：
    NACOS_SERVER       www.joyeuse.cn:8085
    NACOS_NAMESPACE    public
    NACOS_GROUP        DEFAULT_GROUP
    NACOS_USERNAME / NACOS_PASSWORD   （Nacos 开了鉴权时填）
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
from typing import Any, Dict, List, Optional

#: 单次请求超时（秒）。Nacos 是旁路组件，宁可慢一步也不能拖住业务线程
_TIMEOUT = 3.0


def _log(msg: str) -> None:
    print(f"[nacos] {msg}", flush=True)


class NacosClient:
    """最小可用的 Nacos 客户端（HTTP OpenAPI v1）。"""

    def __init__(self, server: str, namespace: str = "public",
                 group: str = "DEFAULT_GROUP",
                 username: str = "", password: str = ""):
        self.server = (server or "").strip().rstrip("/")
        if self.server and not self.server.startswith("http"):
            self.server = "http://" + self.server
        self.namespace = namespace or "public"
        self.group = group or "DEFAULT_GROUP"
        self.username = username or ""
        self.password = password or ""
        self._token: str = ""
        self._token_at: float = 0.0
        self._heartbeat_threads: Dict[str, threading.Thread] = {}
        self._stop = threading.Event()

    # ---------------- 基础 ----------------

    @property
    def enabled(self) -> bool:
        return bool(self.server)

    def _url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        q = dict(params or {})
        if self.namespace:
            q.setdefault("namespaceId", self.namespace)
        if self.group:
            q.setdefault("groupName", self.group)
        if self._token:
            q.setdefault("accessToken", self._token)
        qs = urllib.parse.urlencode({k: v for k, v in q.items() if v is not None})
        return f"{self.server}{path}" + (f"?{qs}" if qs else "")

    def _request(self, method: str, path: str,
                 params: Optional[Dict[str, Any]] = None,
                 body: Optional[Dict[str, Any]] = None,
                 timeout: float = _TIMEOUT) -> Optional[str]:
        """发一个请求，返回响应文本；失败返回 None（不抛）。"""
        if not self.enabled:
            return None
        data = None
        headers = {}
        if body is not None:
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(self._url(path, params), data=data,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception:
            return None

    def login(self) -> bool:
        """Nacos 开启鉴权时先换 accessToken；未开启会返回 403/404，忽略即可。"""
        if not self.username:
            return False
        body = {"username": self.username, "password": self.password}
        try:
            req = urllib.request.Request(
                f"{self.server}/nacos/v1/auth/login",
                data=urllib.parse.urlencode(body).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST")
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            token = payload.get("accessToken") or ""
            if token:
                self._token = token
                self._token_at = time.time()
                return True
        except Exception as e:
            _log(f"鉴权失败（若 Nacos 未开鉴权可忽略）：{e}")
        return False

    # ---------------- 注册 ----------------

    def register(self, service: str, ip: str, port: int,
                 ephemeral: bool = True, weight: float = 1.0,
                 metadata: Optional[Dict[str, str]] = None) -> bool:
        if not self.enabled:
            return False
        if self.username and not self._token:
            self.login()
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
        resp = self._request("POST", "/nacos/v1/ns/instance", body=body)
        ok = resp is not None and resp.strip().lower() == "ok"
        if ok:
            _log(f"已注册 {service} -> {ip}:{port}（group={self.group} ns={self.namespace}）")
            self._start_heartbeat(service, ip, port, ephemeral)
        else:
            _log(f"注册失败 {service} -> {ip}:{port}（Nacos 不可达或参数被拒），"
                 f"服务继续启动，不影响业务")
        return ok

    def deregister(self, service: str, ip: str, port: int,
                   ephemeral: bool = True) -> bool:
        if not self.enabled:
            return False
        params = {"serviceName": service, "ip": ip, "port": port,
                  "ephemeral": "true" if ephemeral else "false"}
        resp = self._request("DELETE", "/nacos/v1/ns/instance", params=params)
        return resp is not None and resp.strip().lower() == "ok"

    # ---------------- 心跳 ----------------

    def _start_heartbeat(self, service: str, ip: str, port: int,
                         ephemeral: bool, interval: float = 5.0) -> None:
        if not ephemeral:
            return
        key = f"{service}@{ip}:{port}"
        if key in self._heartbeat_threads and self._heartbeat_threads[key].is_alive():
            return

        def _beat() -> None:
            beat = json.dumps({"serviceName": service, "ip": ip, "port": port,
                               "cluster": "DEFAULT", "weight": 1})
            while not self._stop.is_set():
                resp = self._request(
                    "PUT", "/nacos/v1/ns/instance/beat",
                    params={"serviceName": service, "beat": beat})
                # 心跳返回 404 说明实例已被摘掉（Nacos 重启/过期），重新注册
                if resp is None or "ok" not in resp.lower():
                    self._request("POST", "/nacos/v1/ns/instance", body={
                        "serviceName": service, "ip": ip, "port": port,
                        "ephemeral": "true", "enabled": "true",
                        "healthy": "true", "weight": 1,
                        "clusterName": "DEFAULT"})
                self._stop.wait(interval)

        t = threading.Thread(target=_beat, name=f"nacos-beat-{service}",
                             daemon=True)
        t.start()
        self._heartbeat_threads[key] = t

    def stop(self) -> None:
        self._stop.set()

    # ---------------- 发现 ----------------

    def discover(self, service: str) -> List[Dict[str, Any]]:
        """查实例列表，返回 [{'ip','port','healthy'}...]；失败返回空列表。"""
        if not self.enabled:
            return []
        if self.username and not self._token:
            self.login()
        params = {"serviceName": service, "healthyOnly": "true"}
        resp = self._request("GET", "/nacos/v1/ns/instance/list", params=params)
        if not resp:
            return []
        try:
            payload = json.loads(resp)
        except Exception:
            return []
        hosts = payload.get("hosts") or []
        out: List[Dict[str, Any]] = []
        for h in hosts:
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
        # 健康的优先，权重大的优先
        out.sort(key=lambda x: (not x["healthy"], -x["weight"]))
        return out

    def services(self) -> List[str]:
        resp = self._request("GET", "/nacos/v1/ns/service/list",
                             params={"pageNo": 1, "pageSize": 200})
        if not resp:
            return []
        try:
            return list((json.loads(resp) or {}).get("doms") or [])
        except Exception:
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
                server=os.getenv("NACOS_SERVER", "www.joyeuse.cn:8085"),
                namespace=os.getenv("NACOS_NAMESPACE", "public"),
                group=os.getenv("NACOS_GROUP", "DEFAULT_GROUP"),
                username=os.getenv("NACOS_USERNAME", ""),
                password=os.getenv("NACOS_PASSWORD", ""),
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
