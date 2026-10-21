# -*- coding: utf-8 -*-
"""账号池与代理池（方案 #2：多账号 + IP 代理池）。

- Cookie 加密存储，不落明文
- 账号按健康度挑选，命中风控后降权 + 冷却，冷却结束自动恢复
- 代理 IP 持久化，按延迟/成功率挑选，连续失败自动剔除
"""
from .pool import AccountPool, ProxyPool, pick_account, pick_proxy, report_account_result, report_proxy_result

__all__ = [
    "AccountPool", "ProxyPool", "pick_account", "pick_proxy",
    "report_account_result", "report_proxy_result",
]
