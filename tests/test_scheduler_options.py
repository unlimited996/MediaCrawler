# -*- coding: utf-8 -*-
"""定时任务 options 字段贯穿性回归。

背景（2026-10-29）：前端「勾了/填了保存后毫无变化」，查下来是
**入参 pydantic 模型 `TaskOptions` 没声明该字段** —— pydantic 默认
`extra="ignore"`，未知键在请求进路由之前就被静默丢掉了，
`_normalize_options` 再怎么改都无济于事。

当时连续踩了两次：
  - `max_notes`（本次抓取上限）→ 用户填 100 仍按默认跑；
  - `ignore_risk_cooldown`（风控冷却豁免）→ 勾了保存不生效。

这些测试锁住三件事：
  1. schema 层不能吞字段（含未知键）；
  2. `_normalize_options` 不能吞字段（含未知键）；
  3. 默认值语义（未传 = False / None）。
"""
from api.routers.scheduler import _normalize_options
from api.schemas.scheduler import ScheduledTaskCreate, ScheduledTaskUpdate, TaskOptions


def test_schema_keeps_known_options():
    """显式声明的选项必须穿过 pydantic 入参模型。"""
    out = TaskOptions(**{
        "ignore_risk_cooldown": True,
        "max_notes": 50,
        "enable_media": True,
    }).model_dump()
    assert out["ignore_risk_cooldown"] is True
    assert out["max_notes"] == 50
    assert out["enable_media"] is True


def test_schema_keeps_unknown_option():
    """extra="allow"：将来新增的选项不会被静默吞掉（本次事故的根因）。"""
    out = TaskOptions(**{"some_future_option": "keep-me"}).model_dump()
    assert out.get("some_future_option") == "keep-me"


def test_normalize_keeps_known_and_unknown():
    """归一化白名单同样不能吞字段。"""
    norm = _normalize_options({
        "ignore_risk_cooldown": True,
        "max_notes": 30,
        "some_future_option": 1,
    })
    assert norm["ignore_risk_cooldown"] is True
    assert norm["max_notes"] == 30
    assert norm["some_future_option"] == 1


def test_option_defaults():
    """未传时：ignore 默认 False、max_notes 默认 None（= 用配置默认值）。"""
    norm = _normalize_options(TaskOptions().model_dump())
    assert norm["ignore_risk_cooldown"] is False
    assert norm.get("max_notes") is None


def test_create_and_update_models_accept_fields():
    """create / update 两个入口都要能收到这些字段。"""
    common = dict(name="t", platform="xhs", task_type="account", target="5")
    created = ScheduledTaskCreate(**common, options={
        "ignore_risk_cooldown": True, "max_notes": 20,
    })
    assert created.options.ignore_risk_cooldown is True
    assert created.options.max_notes == 20

    updated = ScheduledTaskUpdate(options={"ignore_risk_cooldown": True})
    dumped = updated.model_dump(exclude_unset=True)["options"]
    assert dumped["ignore_risk_cooldown"] is True


def test_end_to_end_unwrapped_payload():
    """模拟前端 PUT 的真实路径：schema → dump → normalize 全程不丢。"""
    payload = ScheduledTaskUpdate(**{
        "options": {"ignore_risk_cooldown": True, "max_notes": 42},
    })
    data = payload.model_dump(exclude_unset=True)
    norm = _normalize_options(data["options"])
    assert norm["ignore_risk_cooldown"] is True
    assert norm["max_notes"] == 42
