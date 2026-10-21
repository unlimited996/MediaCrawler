# -*- coding: utf-8 -*-
"""配置读写接口：分组查询、单项更新、批量更新、版本号轮询。

错误码：
  0     成功
  40001 参数缺失/格式错误
  40002 值类型不匹配
  40003 超出取值范围
  40004 枚举值非法
  40005 正则校验失败
  40401 配置项不存在
  40901 版本冲突（乐观锁）
  50001 服务端异常
"""
import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/config", tags=["config"])

BOOL_TRUE = {"true", "1", "yes", "y", "on"}
BOOL_FALSE = {"false", "0", "no", "n", "off"}


class UpdateItem(BaseModel):
    key: str
    value: Any
    version: int = Field(..., description="乐观锁版本号")


class UpdatePayload(BaseModel):
    value: Any
    version: int
    reason: Optional[str] = None


class BatchPayload(BaseModel):
    items: List[UpdateItem]
    reason: Optional[str] = None
    partial: bool = Field(False, description="true=尽力更新；false=全成功或全回滚")


def _ok(data: Any = None) -> Dict[str, Any]:
    return {"code": 0, "message": "ok", "data": data, "request_id": uuid.uuid4().hex[:16]}


def _fail(code: int, message: str, http: int = 400, **extra) -> HTTPException:
    body = {"code": code, "message": message, "data": extra or None,
            "request_id": uuid.uuid4().hex[:16]}
    return HTTPException(status_code=http, detail=body)


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------
def _validate(item, value: Any) -> str:
    """把传入值按 value_type 规范化为存储字符串；非法则抛 HTTPException。"""
    if value is None:
        raise _fail(40001, "配置值不能为空")

    vt = item.value_type

    if vt == "bool":
        if isinstance(value, bool):
            text = "true" if value else "false"
        else:
            s = str(value).strip().lower()
            if s in BOOL_TRUE:
                text = "true"
            elif s in BOOL_FALSE:
                text = "false"
            else:
                raise _fail(40002, f"布尔值只能是 true/false，收到 {value!r}")
        return text

    if vt in ("int", "decimal"):
        try:
            num = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            raise _fail(40002, f"数值格式非法：{value!r}")
        if vt == "int" and num != num.to_integral_value():
            raise _fail(40002, f"需要整数：{value!r}")
        if item.min_value is not None and num < Decimal(str(item.min_value)):
            raise _fail(40003, f"不能小于 {item.min_value}")
        if item.max_value is not None and num > Decimal(str(item.max_value)):
            raise _fail(40003, f"不能大于 {item.max_value}")
        return str(int(num)) if vt == "int" else str(num)

    if vt == "enum":
        text = str(value)
        options = [o.get("value") for o in (item.enum_options or [])]
        if text not in options:
            raise _fail(40004, f"枚举值非法：{text!r}，可选 {options}")
        return text

    if vt == "json":
        import json
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            raise _fail(40002, "JSON 格式非法")

    # string / list
    text = str(value)
    if item.regex_rule and not re.match(item.regex_rule, text):
        raise _fail(40005, f"不满足格式要求：{item.regex_rule}")
    if item.min_value is not None and len(text) < int(item.min_value):
        raise _fail(40003, f"长度不能小于 {int(item.min_value)}")
    if item.max_value is not None and len(text) > int(item.max_value):
        raise _fail(40003, f"长度不能大于 {int(item.max_value)}")
    return text


def _public_value(item, value: Optional[str]) -> Any:
    """返回给前端的值：敏感项掩码。"""
    if item.masked and value:
        return "***"
    return value


def _serialize(item, group_code: str) -> Dict[str, Any]:
    return {
        "key": item.cfg_key,
        "label": item.label,
        "description": item.description or "",
        "value_type": item.value_type,
        "value": _public_value(item, item.current_value),
        "default_value": _public_value(item, item.default_value),
        "enum_options": item.enum_options,
        "min_value": str(item.min_value) if item.min_value is not None else None,
        "max_value": str(item.max_value) if item.max_value is not None else None,
        "regex_rule": item.regex_rule,
        "unit": item.unit,
        "apply_timing": item.apply_timing,
        "is_advanced": bool(item.is_advanced),
        "masked": bool(item.masked),
        "sort_weight": item.sort_weight,
        "version": item.version,
    }


async def _get_engine():
    from config.db_config import resolve_config_db_type
    from database.db_session import get_async_engine

    engine = get_async_engine(resolve_config_db_type())
    if engine is None:
        raise _fail(50001, "配置库不可用，请检查 CONFIG_DB_TYPE", http=500)
    return engine


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------
@router.get("/version")
async def config_version():
    """轻量轮询接口：只返回全局版本号。"""
    from config.runtime_config import runtime_config
    if not runtime_config._loaded:
        try:
            await runtime_config.load()
        except Exception:
            pass
    return _ok({"config_version": runtime_config.config_version})


@router.get("/groups")
async def list_groups(include_advanced: bool = False, group: Optional[str] = None):
    """分组查询全部配置项。"""
    from database.models_v2 import CrawlConfigGroup, CrawlConfigItem

    engine = await _get_engine()
    async with AsyncSession(engine, expire_on_commit=False) as session:
        q = select(CrawlConfigGroup).order_by(CrawlConfigGroup.sort_weight)
        if group:
            q = q.where(CrawlConfigGroup.code == group)
        groups = (await session.execute(q)).scalars().all()

        result = []
        max_version = 0
        for g in groups:
            iq = select(CrawlConfigItem).where(
                CrawlConfigItem.group_id == g.id, CrawlConfigItem.visible.is_(True)
            ).order_by(CrawlConfigItem.sort_weight, CrawlConfigItem.cfg_key)
            items = (await session.execute(iq)).scalars().all()
            if not include_advanced:
                items = [i for i in items if not i.is_advanced]
            max_version = max([max_version] + [int(i.version or 1) for i in items])
            result.append({
                "code": g.code, "name": g.name, "description": g.description,
                "sort_weight": g.sort_weight, "icon": g.icon,
                "items": [_serialize(i, g.code) for i in items],
            })

    return _ok({"config_version": max_version, "groups": result})


# --------------------------------------------------------------------------
# 更新
# --------------------------------------------------------------------------
@router.put("/items/{key}")
async def update_item(key: str, payload: UpdatePayload):
    """单项更新（乐观锁 + 幂等 + 审计）。"""
    from config.runtime_config import runtime_config
    from database.models_v2 import CrawlConfigAudit, CrawlConfigItem

    engine = await _get_engine()
    async with AsyncSession(engine, expire_on_commit=False) as session:
        item = (await session.execute(
            select(CrawlConfigItem).where(CrawlConfigItem.cfg_key == key)
        )).scalar_one_or_none()
        if not item:
            raise _fail(40401, f"配置项不存在：{key}", http=404)

        new_text = _validate(item, payload.value)

        # 幂等：值没变直接返回，不动 version、不写审计
        if (item.current_value or "") == new_text:
            return _ok({
                "key": key, "value": _public_value(item, item.current_value),
                "version": item.version, "apply_timing": item.apply_timing,
                "changed": False,
            })

        # 乐观锁：只有 version 匹配才更新
        if int(item.version or 1) != int(payload.version):
            raise _fail(
                40901,
                "配置已被其他操作修改，请刷新后重试",
                http=409,
                current_value=_public_value(item, item.current_value),
                current_version=item.version,
                your_value=payload.value,
            )

        old_text = item.current_value
        item.current_value = new_text
        item.version = int(item.version or 1) + 1
        item.updated_by = payload.reason or "web"
        session.add(CrawlConfigAudit(
            cfg_key=key, old_value=old_text, new_value=new_text,
            operator="web", reason=payload.reason,
        ))
        try:
            await session.commit()
        except IntegrityError as e:
            await session.rollback()
            raise _fail(50001, f"写入失败：{e}", http=500)

        # 立即生效类配置：热加载
        if item.apply_timing == "immediate":
            try:
                await runtime_config.refresh()
            except Exception:
                pass

        return _ok({
            "key": key,
            "value": _public_value(item, item.current_value),
            "version": item.version,
            "apply_timing": item.apply_timing,
            "changed": True,
            "config_version": item.version,
        })


@router.post("/items/batch")
async def update_batch_alias(payload: BatchPayload):
    """与 /items:batch 等价，避免 URL 中的冒号需要百分号编码。"""
    return await update_batch(payload)


@router.post("/items:batch")
async def update_batch(payload: BatchPayload):
    """批量更新：默认全成功或全回滚；partial=true 时尽力更新。"""
    from config.runtime_config import runtime_config
    from database.models_v2 import CrawlConfigAudit, CrawlConfigItem

    if not payload.items:
        raise _fail(40001, "items 不能为空")

    engine = await _get_engine()
    updated, failed, need_reload = [], [], False

    async with AsyncSession(engine, expire_on_commit=False) as session:
        for req in payload.items:
            try:
                item = (await session.execute(
                    select(CrawlConfigItem).where(CrawlConfigItem.cfg_key == req.key)
                )).scalar_one_or_none()
                if not item:
                    raise _fail(40401, f"配置项不存在：{req.key}", http=404)

                new_text = _validate(item, req.value)

                if (item.current_value or "") == new_text:   # 幂等
                    updated.append({"key": req.key, "version": item.version, "changed": False})
                    continue

                if int(item.version or 1) != int(req.version):
                    raise _fail(40901, "版本冲突", http=409,
                                current_value=_public_value(item, item.current_value),
                                current_version=item.version)

                old_text = item.current_value
                item.current_value = new_text
                item.version = int(item.version or 1) + 1
                session.add(CrawlConfigAudit(
                    cfg_key=req.key, old_value=old_text, new_value=new_text,
                    operator="web", reason=payload.reason,
                ))
                updated.append({"key": req.key, "value": _public_value(item, item.current_value),
                                "version": item.version, "apply_timing": item.apply_timing,
                                "changed": True})
                if item.apply_timing == "immediate":
                    need_reload = True
            except HTTPException as e:
                failed.append({"key": req.key, "code": e.detail["code"],
                               "message": e.detail["message"]})
                if not payload.partial:
                    await session.rollback()
                    return _ok({"updated": [], "failed": failed, "rolled_back": True})

        try:
            await session.commit()
        except IntegrityError as e:
            await session.rollback()
            raise _fail(50001, f"写入失败：{e}", http=500)

    if need_reload:
        try:
            await runtime_config.refresh()
        except Exception:
            pass

    return _ok({"updated": updated, "failed": failed, "rolled_back": False})
