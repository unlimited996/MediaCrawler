# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/database/db_session.py
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

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from contextlib import asynccontextmanager
from urllib.parse import quote
from .models import Base
import config
from config.db_config import mysql_db_config, sqlite_db_config, postgres_db_config

# Keep a cache of engines
_engines = {}


def _build_db_url(driver: str, host, port, user, password, db_name: str = "") -> str:
    """拼接数据库 URL，并对用户名/密码做百分号转义。

    密码里的 @ # : / ? 等字符若不转义会让 URL 解析错位会被当成
    host 分隔符，导致认证信息整体错位），必须转义。
    """
    auth = f"{quote(str(user), safe='')}:{quote(str(password), safe='')}"
    netloc = f"{auth}@{host}:{port}"
    return f"{driver}://{netloc}/{db_name}" if db_name else f"{driver}://{netloc}"


async def create_database_if_not_exists(db_type: str):
    if db_type == "mysql" or db_type == "db":
        # Connect to the server without a database
        server_url = _build_db_url(
            "mysql+asyncmy",
            mysql_db_config["host"],
            mysql_db_config["port"],
            mysql_db_config["user"],
            mysql_db_config["password"],
        )
        engine = create_async_engine(server_url, echo=False)
        async with engine.connect() as conn:
            await conn.execute(text(f"CREATE DATABASE IF NOT EXISTS {mysql_db_config['db_name']}"))
        await engine.dispose()
    elif db_type == "postgres":
        # Connect to the default 'postgres' database
        server_url = _build_db_url(
            "postgresql+asyncpg",
            postgres_db_config["host"],
            postgres_db_config["port"],
            postgres_db_config["user"],
            postgres_db_config["password"],
            "postgres",
        )
        print(f"[init_db] Connecting to Postgres: host={postgres_db_config['host']}, port={postgres_db_config['port']}, user={postgres_db_config['user']}, dbname=postgres")
        # Isolation level AUTOCOMMIT is required for CREATE DATABASE
        engine = create_async_engine(server_url, echo=False, isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            # Check if database exists
            result = await conn.execute(text(f"SELECT 1 FROM pg_database WHERE datname = '{postgres_db_config['db_name']}'"))
            if not result.scalar():
                await conn.execute(text(f"CREATE DATABASE {postgres_db_config['db_name']}"))
        await engine.dispose()


def get_async_engine(db_type: str = None):
    if db_type is None:
        db_type = config.SAVE_DATA_OPTION

    if db_type in _engines:
        return _engines[db_type]

    if db_type in ["json", "jsonl", "csv"]:
        return None

    if db_type == "sqlite":
        db_url = f"sqlite+aiosqlite:///{sqlite_db_config['db_path']}"
    elif db_type == "mysql" or db_type == "db":
        db_url = _build_db_url(
            "mysql+asyncmy",
            mysql_db_config["host"],
            mysql_db_config["port"],
            mysql_db_config["user"],
            mysql_db_config["password"],
            mysql_db_config["db_name"],
        )
    elif db_type == "postgres":
        db_url = _build_db_url(
            "postgresql+asyncpg",
            postgres_db_config["host"],
            postgres_db_config["port"],
            postgres_db_config["user"],
            postgres_db_config["password"],
            postgres_db_config["db_name"],
        )
    else:
        raise ValueError(f"Unsupported database type: {db_type}")

    engine = create_async_engine(
        db_url, echo=False,
        # 连接被服务端/中间层掐断（idle 超时、网络抖动、双实例争用）时自动重连，
        # 防止 scheduler tick 报 asyncpg.ConnectionDoesNotExistError。
        pool_pre_ping=True,
        pool_recycle=1800,
    )
    _engines[db_type] = engine
    return engine


class SharedEngineHandle:
    """包住进程级缓存的引擎，只把 ``dispose()`` 变成空操作。

    ``api/routers/*`` 里每个 endpoint 的 finally 都写着 ``await engine.dispose()``，
    原意是「释放资源」，但 get_async_engine 返回的是**全局共享**实例：dispose 会把
    连接池整个销毁，下一个请求就得重新解析域名 + TCP + 认证（远端库走公网时是
    几百毫秒），表面现象就是「一条 LIMIT 1 的 SQL 也要几秒」。

    ``AsyncEngine.dispose`` 是只读属性，没法在实例上打补丁，所以用这个薄壳：
    routers 拿它调 ``dispose()`` 无副作用，其余属性/方法原样透传给真引擎。
    池子的生命周期交给进程本身。
    """

    __slots__ = ("_engine",)

    def __init__(self, engine):
        self._engine = engine

    def __getattr__(self, item):
        return getattr(self._engine, item)

    async def dispose(self, close: bool = True) -> None:  # noqa: ARG002
        return None

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"SharedEngineHandle({self._engine!r})"


async def dispose_all() -> None:
    """真正关闭所有缓存的引擎（仅用于进程退出/测试收尾）。"""
    for db_type in list(_engines.keys()):
        eng = _engines.pop(db_type, None)
        if eng is None:
            continue
        try:
            await eng.dispose()
        except Exception:
            pass


async def create_tables(db_type: str = None):
    if db_type is None:
        db_type = config.SAVE_DATA_OPTION
    await create_database_if_not_exists(db_type)
    engine = get_async_engine(db_type)
    if engine:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def get_session() -> AsyncSession:
    engine = get_async_engine(config.SAVE_DATA_OPTION)
    if not engine:
        yield None
        return
    AsyncSessionFactory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    session = AsyncSessionFactory()
    try:
        yield session
        await session.commit()
    except Exception as e:
        await session.rollback()
        raise e
    finally:
        await session.close()
