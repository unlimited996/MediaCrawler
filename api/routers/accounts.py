# -*- coding: utf-8 -*-
"""账号池管理（Cookie 加密存储）。

GET    /api/accounts          列表（**不返回 Cookie 明文**，只给是否可用/健康度）
POST   /api/accounts          手动添加/更新账号
DELETE /api/accounts/{id}     删除账号
"""
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter(prefix="/accounts", tags=["accounts"])


class AccountIn(BaseModel):
    platform: str = Field(..., description="平台代码：xhs/dy/ks/bili/wb/tieba/zhihu")
    account_name: str = Field("default", description="本地标识")
    cookie: str = Field(..., description="浏览器里复制的完整 Cookie 串")
    login_type: str = "cookie"


@router.get("")
async def list_accounts(platform: Optional[str] = None):
    from account import AccountPool

    pool = AccountPool()
    try:
        rows = await pool.list_accounts(platform)
        return {"code": 0, "data": {"items": rows, "total": len(rows)}}
    except Exception as e:
        return {"code": 50001, "message": f"{type(e).__name__}: {str(e)[:200]}"}


@router.post("")
async def upsert_account(payload: AccountIn):
    from account import AccountPool

    if not payload.cookie.strip():
        return {"code": 40001, "message": "cookie 不能为空"}

    pool = AccountPool()
    try:
        account_id = await pool.add(
            payload.platform, payload.account_name, payload.cookie.strip(),
            login_type=payload.login_type,
        )
    except Exception as e:
        return {"code": 50001, "message": f"{type(e).__name__}: {str(e)[:200]}"}

    return {"code": 0, "data": {"id": account_id, "platform": payload.platform,
                                "account_name": payload.account_name}}


@router.delete("/{account_id}")
async def delete_account(account_id: int):
    from sqlalchemy import delete as sa_delete
    from sqlalchemy.ext.asyncio import AsyncSession

    from config.db_config import resolve_config_db_type
    from database.db_session import get_async_engine
    from database.models_v2 import Account

    engine = get_async_engine(resolve_config_db_type())
    if engine is None:
        return {"code": 50001, "message": "配置库不可用"}
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await session.execute(sa_delete(Account).where(Account.id == account_id))
            await session.commit()
        return {"code": 0, "data": {"id": account_id}}
    except Exception as e:
        return {"code": 50001, "message": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        await engine.dispose()
