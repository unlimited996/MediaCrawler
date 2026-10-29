# -*- coding: utf-8 -*-
"""账号池 / 代理池的挑选与反馈逻辑。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .crypto import decrypt_cookie, encrypt_cookie


def _engine_session():
    from config.db_config import resolve_config_db_type
    from database.db_session import get_async_engine

    engine = get_async_engine(resolve_config_db_type())
    if engine is None:
        return None, None
    return engine, AsyncSession(engine, expire_on_commit=False)


class AccountPool:
    """按健康度挑选账号；失败降权 + 冷却。"""

    COOLDOWN_MINUTES = 10
    FAIL_PENALTY = 20
    BAN_THRESHOLD = 3      # 连续失败达到该值直接禁用

    async def add(self, platform: str, account_name: str, cookie: str,
                  login_type: str = "cookie", profile_dir: str = "") -> Optional[int]:
        from database.models_v2 import Account

        engine, session = _engine_session()
        if session is None:
            return None
        try:
            row = (await session.execute(
                select(Account).where(Account.platform == platform,
                                      Account.account_name == account_name)
            )).scalar_one_or_none()
            if row:
                row.cookie_cipher = encrypt_cookie(cookie)
                row.login_type = login_type
                if profile_dir:
                    row.profile_dir = profile_dir
            else:
                row = Account(platform=platform, account_name=account_name,
                              cookie_cipher=encrypt_cookie(cookie), login_type=login_type,
                              profile_dir=profile_dir or None)
                session.add(row)
            await session.commit()
            return row.id
        finally:
            await session.close()
            if engine:
                await engine.dispose()

    async def pick(self, platform: str) -> Optional[Dict[str, Any]]:
        """挑一个可用账号：active 且不在冷却中，健康度最高。"""
        from database.models_v2 import Account

        engine, session = _engine_session()
        if session is None:
            return None
        try:
            now = datetime.now()
            rows = (await session.execute(
                select(Account).where(
                    Account.platform == platform,
                    Account.status == "active",
                ).order_by(Account.health_score.desc(), Account.last_used_at.asc())
            )).scalars().all()

            for r in rows:
                if r.cooldown_until and r.cooldown_until > now:
                    continue
                r.last_used_at = now
                await session.commit()
                return {
                    "id": r.id,
                    "account_name": r.account_name,
                    "cookie": decrypt_cookie(r.cookie_cipher or ""),
                    "profile_dir": r.profile_dir,
                    "health_score": r.health_score,
                }
            return None
        finally:
            await session.close()
            if engine:
                await engine.dispose()

    async def get_account(self, account_id: int) -> Optional[Dict[str, Any]]:
        """按 ID 取账号（含解密后的 cookie），不存在返回 None。

        与 pick() 的区别：pick 按健康度挑「某一个」；定时任务的「登录用户」
        维度盯的是特定账号（可能同时扫码登录多个），必须按 ID 精确取。
        """
        from database.models_v2 import Account

        engine, session = _engine_session()
        if session is None:
            return None
        try:
            row = (await session.execute(
                select(Account).where(Account.id == int(account_id))
            )).scalar_one_or_none()
            if not row:
                return None
            return {
                "id": row.id,
                "account_name": row.account_name,
                "cookie": decrypt_cookie(row.cookie_cipher or ""),
                "profile_dir": row.profile_dir,
                "health_score": row.health_score,
                "status": row.status,
            }
        finally:
            await session.close()
            if engine:
                await engine.dispose()

    async def report(self, account_id: int, ok: bool, risk_code: Optional[str] = None) -> None:
        """反馈结果：成功恢复健康度，失败降权 + 冷却，连续失败过多则禁用。"""
        from database.models_v2 import Account

        engine, session = _engine_session()
        if session is None:
            return
        try:
            row = (await session.execute(
                select(Account).where(Account.id == account_id)
            )).scalar_one_or_none()
            if not row:
                return

            if ok:
                row.success_count = int(row.success_count or 0) + 1
                row.fail_count = 0
                row.health_score = min(100, int(row.health_score or 100) + 5)
                row.cooldown_until = None
            else:
                row.fail_count = int(row.fail_count or 0) + 1
                row.health_score = max(0, int(row.health_score or 100) - self.FAIL_PENALTY)
                # 命中账号级风控（300011）冷却更久
                minutes = self.COOLDOWN_MINUTES * (3 if risk_code == "300011" else 1)
                row.cooldown_until = datetime.now() + timedelta(minutes=minutes)
                if int(row.fail_count or 0) >= self.BAN_THRESHOLD:
                    row.status = "banned"
            await session.commit()
        finally:
            await session.close()
            if engine:
                await engine.dispose()

    async def list_accounts(self, platform: Optional[str] = None) -> List[Dict[str, Any]]:
        from database.models_v2 import Account

        engine, session = _engine_session()
        if session is None:
            return []
        try:
            q = select(Account)
            if platform:
                q = q.where(Account.platform == platform)
            rows = (await session.execute(q.order_by(Account.platform, Account.id))).scalars().all()
            return [{
                "id": r.id, "platform": r.platform, "account_name": r.account_name,
                "status": r.status, "health_score": r.health_score,
                "fail_count": r.fail_count, "success_count": r.success_count,
                "cooldown_until": str(r.cooldown_until) if r.cooldown_until else None,
                "has_cookie": bool(r.cookie_cipher),
            } for r in rows]
        finally:
            await session.close()
            if engine:
                await engine.dispose()


class ProxyPool:
    """代理 IP 池：按延迟与成功率挑选。"""

    async def upsert(self, provider: str, ip: str, port: int, protocol: str = "http",
                     region: str = "", expire_at: Optional[datetime] = None) -> Optional[int]:
        from database.models_v2 import ProxyIp

        engine, session = _engine_session()
        if session is None:
            return None
        try:
            row = (await session.execute(
                select(ProxyIp).where(ProxyIp.ip == ip, ProxyIp.port == port,
                                      ProxyIp.protocol == protocol)
            )).scalar_one_or_none()
            if row:
                row.provider = provider
                if region:
                    row.region = region
                if expire_at:
                    row.expire_at = expire_at
            else:
                row = ProxyIp(provider=provider, ip=ip, port=port, protocol=protocol,
                              region=region or None, expire_at=expire_at)
                session.add(row)
            await session.commit()
            return row.id
        finally:
            await session.close()
            if engine:
                await engine.dispose()

    async def pick(self) -> Optional[Dict[str, Any]]:
        from database.models_v2 import ProxyIp

        engine, session = _engine_session()
        if session is None:
            return None
        try:
            row = (await session.execute(
                select(ProxyIp).where(ProxyIp.status.in_(["ok", "unknown"]))
                .order_by(ProxyIp.latency_ms.asc().nullsfirst(), ProxyIp.fail_count.asc())
            )).scalars().first()
            if not row:
                return None
            return {"id": row.id, "ip": row.ip, "port": row.port,
                    "protocol": row.protocol, "region": row.region}
        finally:
            await session.close()
            if engine:
                await engine.dispose()

    async def report(self, proxy_id: int, ok: bool, latency_ms: Optional[int] = None) -> None:
        from database.models_v2 import ProxyIp

        engine, session = _engine_session()
        if session is None:
            return
        try:
            row = (await session.execute(
                select(ProxyIp).where(ProxyIp.id == proxy_id)
            )).scalar_one_or_none()
            if not row:
                return
            if ok:
                row.success_count = int(row.success_count or 0) + 1
                row.fail_count = 0
                row.status = "ok"
                if latency_ms is not None:
                    row.latency_ms = latency_ms
            else:
                row.fail_count = int(row.fail_count or 0) + 1
                row.status = "dead" if int(row.fail_count or 0) >= 3 else "slow"
            await session.commit()
        finally:
            await session.close()
            if engine:
                await engine.dispose()

    async def list_proxies(self) -> List[Dict[str, Any]]:
        from database.models_v2 import ProxyIp

        engine, session = _engine_session()
        if session is None:
            return []
        try:
            rows = (await session.execute(
                select(ProxyIp).order_by(ProxyIp.status, ProxyIp.id))).scalars().all()
            return [{
                "id": r.id, "provider": r.provider, "ip": r.ip, "port": r.port,
                "protocol": r.protocol, "region": r.region, "status": r.status,
                "latency_ms": r.latency_ms, "success": r.success_count, "fail": r.fail_count,
            } for r in rows]
        finally:
            await session.close()
            if engine:
                await engine.dispose()


# 模块级便捷函数
_pool = AccountPool()
_proxy_pool = ProxyPool()


async def pick_account(platform: str) -> Optional[Dict[str, Any]]:
    return await _pool.pick(platform)


async def report_account_result(account_id: int, ok: bool, risk_code: Optional[str] = None) -> None:
    await _pool.report(account_id, ok, risk_code)


async def pick_proxy() -> Optional[Dict[str, Any]]:
    return await _proxy_pool.pick()


async def report_proxy_result(proxy_id: int, ok: bool, latency_ms: Optional[int] = None) -> None:
    await _proxy_pool.report(proxy_id, ok, latency_ms)
