"""按访问来源挑 S3 endpoint 的公共工具。

「浏览器直连 S3」的场景（视频 307、图片 307）必须用这个：
  - 内网访客 → 内网 endpoint（走 NAT 回流会连不上）
  - 外网访客 → 公网 endpoint（浏览器访问不到内网地址）
  - 配置成容器名（无点）→ 浏览器解析不了，回退公网并告警
"""
import logging
import os
from urllib.parse import urlparse

from fastapi import Request

log = logging.getLogger(__name__)


def is_private_host(host: str) -> bool:
    """host 是否是私网/回环地址（内网用户就是这样访问的）。"""
    import ipaddress

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(ip.is_private or ip.is_loopback)


def browser_reachable(host: str) -> bool:
    """浏览器能否解析这个主机名。

    IP 一定可以；含点的域名交给局域网 DNS；**容器名（无点）只有 docker
    内部能解析**，给浏览器就是个打不开的地址。
    """
    import ipaddress

    host = (host or "").strip().lower()
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return "." in host


def presign_settings_for(request: Request, public, internal):
    """按访问来源挑预签名用的 S3 endpoint。

    可用 ``S3_PRESIGN_FORCE=internal|public`` 强制覆盖（内网用域名访问时
    自动判定会失效，需要手动指定）。
    """
    force = os.getenv("S3_PRESIGN_FORCE", "auto").strip().lower()
    if force in ("internal", "intranet", "lan"):
        return internal
    if force in ("public", "wan", "external"):
        return public
    if internal.endpoint == public.endpoint:
        return public

    host = (request.headers.get("host") or "").split(":")[0].strip().lower()
    if not host:
        return public
    internal_host = (urlparse(internal.endpoint).hostname or "").lower()
    if not browser_reachable(internal_host):
        log.warning(
            "[s3] S3_INTERNAL_ENDPOINT=%s 是容器名，浏览器无法解析，"
            "播放/看图改用公网地址；要内网直连请填局域网 IP",
            internal.endpoint)
        return public
    if host == internal_host or is_private_host(host):
        return internal
    return public
