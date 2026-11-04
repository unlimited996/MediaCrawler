# -*- coding: utf-8 -*-
"""服务拆分契约测试（crawler / library 两个独立应用 + 服务名路由）。

只做**静态契约**校验，不触发 startup（不会连库），保证 CI 里能快速跑。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


def _paths(app):
    return {getattr(r, "path", "") for r in app.routes}


def test_crawler_app_routes_are_prefixed_by_service_name():
    from api.crawler_app import app

    paths = _paths(app)
    # 抓取相关接口都在 /crawler/api 下
    assert "/crawler/api/scheduler/status" in paths
    assert "/crawler/api/crawler/status" in paths
    assert "/crawler/api/config/groups" in paths
    # 健康检查两个入口都要有（容器 HEALTHCHECK 用带前缀的）
    assert "/crawler/health" in paths
    assert "/health" in paths
    # 不能把内容库接口也挂进来（否则又变成单体）
    assert not any(p.startswith("/library/api/") for p in paths)


def test_library_app_routes_are_prefixed_by_service_name():
    from api.library_app import app

    paths = _paths(app)
    assert "/library/api/stories" in paths
    # 文件/媒体资源子路径已从笼统的 /data 改为 /assets
    assert "/library/api/assets/files" in paths
    assert "/library/api/assets/media/{platform}/{content_id}/{filename}" in paths
    assert "/library/api/internal/events" in paths
    assert "/library/health" in paths
    # 抓取接口不该出现在内容库里（它只做代理）
    assert "/library/api/scheduler/status" not in paths
    # 旧的 /data 前缀必须彻底消失（否则前端还会打到旧地址）
    assert not any(p.startswith("/data/api") for p in paths)


def test_gateway_proxies_crawler_prefix():
    from api.library_app import app

    paths = _paths(app)
    assert "/crawler/{path:path}" in paths          # HTTP 代理
    assert "/crawler/api/ws/logs" in paths          # WebSocket 代理
    assert "/gateway/status" in paths


def test_media_urls_carry_library_prefix():
    """story 接口返回给浏览器的媒体链接必须带 /library/api 前缀。"""
    from api.service_paths import asset_url, library_api, media_url

    assert library_api("assets/raw/a.png") == "/library/api/assets/raw/a.png"
    assert asset_url("raw", "x/y.png") == "/library/api/assets/raw/x/y.png"
    assert media_url("xhs", "abc", "1.png") == "/library/api/assets/media/xhs/abc/1.png"


def test_discovery_falls_back_to_static_url_without_nacos(monkeypatch):
    import config
    from api.services import discovery

    monkeypatch.setattr(config, "NACOS_ENABLED", False, raising=False)
    urls = discovery.discover(getattr(config, "CRAWLER_SERVICE_NAME"))
    assert urls, "Nacos 关掉时必须回退到 CRAWLER_BASE_URL，而不是空列表"
    assert urls[0].startswith("http")


def test_nacos_client_disabled_is_noop():
    from tools.nacos_client import NacosClient

    c = NacosClient(server="")
    assert c.enabled is False
    assert c.discover("anything") == []
    assert c.register("svc", "127.0.0.1", 1) is False
