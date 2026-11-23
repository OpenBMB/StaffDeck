"""Alembic 迁移环境:复用应用的方言适配层与配置,SQLite/非 SQLite 一致。

URL 解析优先级:
1. ``sqlalchemy.url``(程序化调用由 ``init_db`` 通过
   ``Config.set_main_option`` 注入;CLI 可配合 ``DATABASE_URL`` 环境变量回退);
2. 应用配置 ``app.config.get_settings().database_url``(经
   ``normalize_database_url`` 归一化,与运行时完全一致)。

引擎参数统一取自 ``app.db.dialect.get_dialect(...).engine_kwargs``,
避免迁移连接与应用运行时连接行为不一致(如连接池预检、SQLite connect_args)。
"""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlmodel import SQLModel

# alembic CLI 可能从任意目录执行:显式把 backend/ 加入 sys.path,保证 app 包可导入。
# 迁移目录放在 app 包内(app/db/migrations),wheel/PyInstaller 打包时随 package-data 一起分发。
# (script_location 本身由 alembic.ini 的 %(here)s 解析为绝对路径。)
# 迁移脚本位于 app/db/migrations/:向上三级即 backend/(app 包所在根目录)
BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app.db.models  # noqa: E402,F401  # 注册全部 SQLModel 表/索引/方言 DDL 事件
from app.config import get_settings  # noqa: E402
from app.db.database_path import normalize_database_url  # noqa: E402
from app.db.dialect import ensure_driver_available, get_dialect  # noqa: E402

# Alembic 会把 env.py 作为脚本执行并注入全局 context。
config = context.config

target_metadata = SQLModel.metadata


def _resolve_database_url() -> str:
    """优先显式配置的 alembic URL,否则回退应用 database_url。"""
    url = (config.get_main_option("sqlalchemy.url") or "").strip()
    if url:
        return url
    return normalize_database_url(get_settings().database_url)


def _build_engine(url: str) -> Engine:
    parsed = make_url(url)
    ensure_driver_available(parsed)
    dialect = get_dialect(parsed.get_backend_name())
    return create_engine(url, **dialect.engine_kwargs(url))


def _include_object(object_, name, type_, reflected, compare_to) -> bool:
    """autogenerate 比对过滤:models.py 用 after_create DDL 创建的部分唯一索引
    (uq_model_configs_tenant_default)不在 metadata 中,数据库里多出的它不应被
    误判为 remove_index;该索引由基线 migration 显式创建。"""
    if type_ == "index" and name == "uq_model_configs_tenant_default":
        return False
    return True


def run_migrations_offline() -> None:
    """离线模式(--sql):只渲染 SQL,不实际连接数据库。"""
    context.configure(
        url=_resolve_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式:按应用同款引擎参数连接后执行迁移。"""
    connectable = _build_engine(_resolve_database_url())
    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                include_object=_include_object,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
