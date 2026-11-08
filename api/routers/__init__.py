# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/api/routers/__init__.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#
# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

"""路由按需导出（PEP 562）。

拆分成 crawler / library 两个服务后，两个 app 都写 `from .routers import (...)`。
若这里顶层导入全部 router，library 会顺带加载 scheduler_router →
task_scheduler → tools.anti_risk → tools.utils → playwright / cv2，
把整套爬虫依赖拖进「只有数据接口 + 前端」的进程。改为惰性后，
每个 app 只加载自己真正 include 的路由模块。
"""
from importlib import import_module

#: 导出名 -> (子模块, 模块内属性名)
_ROUTER_EXPORTS = {
    "crawler_router": ("crawler", "router"),
    "data_router": ("data", "router"),
    "websocket_router": ("websocket", "router"),
    "export_router": ("export", "router"),
    "config_router": ("config_admin", "router"),
    "story_router": ("story", "router"),
    "discover_router": ("discover", "router"),
    "accounts_router": ("accounts", "router"),
    "scheduler_router": ("scheduler", "router"),
    "stance_tags_router": ("stance_tags", "router"),
    "s3_backup_router": ("s3_backup", "router"),
    "recipe_router": ("recipe", "router"),
    "media_router": ("recipe", "media_router"),
    "app_config_router": ("recipe", "app_config_router"),
    "env_router": ("env", "router"),
    "login_session_router": ("login_session", "router"),
}


def __getattr__(name):
    spec = _ROUTER_EXPORTS.get(name)
    if spec is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = spec
    module = import_module(f".{module_name}", __name__)
    return getattr(module, attr)


__all__ = list(_ROUTER_EXPORTS)
