# -*- coding: utf-8 -*-
"""服务拆分契约测试（crawler / data 两个独立应用 + 服务名路由）。

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
    # 不能把数据接口也挂进来（否则又变成单体）
    assert not any(p.startswith("/data/api/") for p in paths)


def test_data_app_routes_are_prefixed_by_service_name():
    from api.data_app import app

    paths = _paths(app)
    assert "/data/api/stories" in paths
    assert "/data/api/internal/events" in paths
    assert "/data/health" in paths
    # 抓取接口不该出现在 data 应用里（它只做代理）
    assert "/data/api/scheduler/status" not in paths


def test_gateway_proxies_crawler_prefix():
    from api.data_app import app

    paths = _paths(app)
    assert "/crawler/{path:path}" in paths          # HTTP 代理
    assert "/crawler/api/ws/logs" in paths          # WebSocket 代理
    assert "/gateway/status" in paths


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
