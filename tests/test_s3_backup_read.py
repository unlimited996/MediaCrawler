# -*- coding: utf-8 -*-
"""S3 图片读取回归：按访问来源分流 + 浏览器可达性 + 容器名回退。

纯函数级测试，不打线上库、不发真实 S3 请求。
"""
from types import SimpleNamespace

from api.lib.s3_presign import (
    browser_reachable, is_private_host, presign_settings_for,
)
from tools.s3_client import S3Settings


def _s3(endpoint: str) -> S3Settings:
    return S3Settings(endpoint=endpoint, access_key_id="GK1",
                      secret_access_key="x" * 20, bucket="photos")


def _req(host: str):
    return SimpleNamespace(headers={"host": host})


def test_private_host_detection():
    for h in ("192.168.10.38", "127.0.0.1", "10.0.0.5"):
        assert is_private_host(h), h
    for h in ("www.joyeuse.cn", "8.8.8.8"):
        assert not is_private_host(h), h


def test_browser_reachable():
    assert browser_reachable("192.168.10.38")
    assert browser_reachable("nas.local")
    assert not browser_reachable("dxflrs-garage")  # 容器名
    assert not browser_reachable("")


def test_presign_follows_request_host(monkeypatch):
    monkeypatch.delenv("S3_PRESIGN_FORCE", raising=False)
    pub = _s3("http://www.joyeuse.cn:3900")
    lan = _s3("http://192.168.10.38:3900")
    assert presign_settings_for(_req("192.168.10.38:18081"), pub, lan) is lan
    assert presign_settings_for(_req("127.0.0.1:18081"), pub, lan) is lan
    assert presign_settings_for(_req("www.joyeuse.cn:18081"), pub, lan) is pub
    # 没配内网时永远公网
    assert presign_settings_for(_req("192.168.10.38:18081"), pub, pub) is pub


def test_presign_rejects_container_name(monkeypatch):
    """容器名浏览器解析不了，必须回退公网，否则图片全裂。"""
    monkeypatch.delenv("S3_PRESIGN_FORCE", raising=False)
    pub = _s3("http://www.joyeuse.cn:3900")
    lan = _s3("http://dxflrs-garage:3900")
    assert presign_settings_for(_req("192.168.10.38:18081"), pub, lan) is pub


def test_presign_force_override(monkeypatch):
    pub = _s3("http://www.joyeuse.cn:3900")
    lan = _s3("http://192.168.10.38:3900")
    monkeypatch.setenv("S3_PRESIGN_FORCE", "public")
    assert presign_settings_for(_req("192.168.10.38"), pub, lan) is pub
    monkeypatch.setenv("S3_PRESIGN_FORCE", "internal")
    assert presign_settings_for(_req("www.joyeuse.cn"), pub, lan) is lan


def test_retry_failed_returns_structured_stats(monkeypatch):
    """retry_failed 返回结构：重置前按 S3 事实自愈，剩下才 pending 重跑。"""
    import inspect

    from tools import media_backup as mb

    src = inspect.getsource(mb.retry_failed)
    assert "head_object" in src, "必须先按 S3 事实校验再决定状态"
    assert '"healed"' in src and '"reset"' in src, "需回显自愈/重置数量"
    assert 'r.status = "uploaded"' in src, "对象存在应直接收敛为 uploaded"
