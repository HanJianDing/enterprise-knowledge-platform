"""Alembic 迁移环境 — 复用 app 的 Base.metadata 与 settings.database_url。

用法：
- 生成迁移（对照空库出基线）：alembic -x db_url=sqlite+aiosqlite:///./_baseline_tmp.db revision --autogenerate -m "..."
- 对当前数据库打基线标记：alembic stamp head（读 .env 的 DATABASE_URL）
- 应用迁移：alembic upgrade head
"""
import asyncio
import sys
from pathlib import Path
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# 让 alembic 能 import app.*（backend 目录）
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.infrastructure.database import Base
from app.models import (  # noqa: F401 — 导入即注册全部模型到 metadata
    User, Conversation, Message, Document, KnowledgeCategory,
    Department, UserDepartment, KnowledgeBase, KbMember, SystemSetting,
)
from app.config import settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _get_url() -> str:
    """迁移用连接串：优先命令行 -x db_url=... 覆盖，否则取 settings.database_url"""
    x_args = context.get_x_argument(as_dictionary=True)
    return x_args.get("db_url") or settings.database_url


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连数据库"""
    context.configure(
        url=_get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """在线模式：用异步引擎执行（支持 postgresql+asyncpg 与 sqlite+aiosqlite）"""
    config_section = config.get_section(config.config_ini_section, {})
    config_section["sqlalchemy.url"] = _get_url()
    connectable = async_engine_from_config(
        config_section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
