"""数据库方言可插拔层.

ORM 优先:能用 SQLAlchemy ORM/标准 SQL 表达的一律走通用实现,无法通用的
少数方言点收口到本模块的 DatabaseDialect 提供者。新增数据库 = 新增一个小
适配器并 register_dialect 注册,业务代码零改动:

- postgresql(含高斯):全能力,即 PostgresDialect(psycopg3 驱动,URL
  postgresql+psycopg://);高斯直接复用该适配器。
- mysql:不预装驱动(按需装 pymysql);不支持部分唯一索引——
  models.py 的 uq_model_configs_tenant_default 须由适配器在 DDL 层跳过,
  默认性降级为代码层校验;advisory lock 可用 GET_LOCK(key, 0) 实现。
- dm(达梦):dmPython 驱动(dm+dmPython://),Oracle 系语法;同样不支持
  部分唯一索引;advisory lock 降级为数据目录文件锁。

未注册的后端名回退 GenericDialect:ORM 通用实现 + 文件锁,大部分功能开箱
可用;适配器只补充各自特性(原生 advisory lock、部分索引、日期函数等)。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import Date, cast, func, literal, text
from sqlalchemy.types import Interval

from app.config import get_settings

logger = logging.getLogger(__name__)


@runtime_checkable
class DatabaseDialect(Protocol):
    """数据库方言提供者:引擎参数、日分桶、JSON 配置读改写、advisory lock。"""

    name: str  # sqlite / postgresql / mysql / dm / ...
    supports_partial_index: bool  # 部分唯一索引(WHERE 子句)能力

    def engine_kwargs(self, url: str) -> dict[str, Any]: ...

    def day_bucket(self, column) -> Any: ...

    def json_config_get(self, config: dict | None, key: str) -> Any: ...

    def json_config_set(self, config: dict | None, key: str, value: Any) -> dict: ...

    def json_config_remove(self, config: dict | None, key: str) -> dict: ...

    def acquire_advisory_lock(self, session, key: str) -> bool: ...

    def release_advisory_lock(self, session, key: str) -> None: ...


class BaseDialect:
    """通用默认实现:JSON 读改写 = Python 侧读-改-写;日分桶 = cast(col, Date)
    (标准 SQL);advisory lock = 数据目录文件锁(无原生锁能力后端的兜底)。"""

    name = "generic"
    supports_partial_index = False

    def __init__(self, backend_name: str = "generic") -> None:
        self.name = backend_name
        self._lock_handles: dict[str, Any] = {}

    def engine_kwargs(self, url: str) -> dict[str, Any]:
        return {}

    def day_bucket(self, column) -> Any:
        return cast(column, Date)

    def json_config_get(self, config: dict | None, key: str) -> Any:
        return dict(config or {}).get(key)

    def json_config_set(self, config: dict | None, key: str, value: Any) -> dict:
        patched = dict(config or {})
        patched[key] = value
        return patched

    def json_config_remove(self, config: dict | None, key: str) -> dict:
        patched = dict(config or {})
        patched.pop(key, None)
        return patched

    def acquire_advisory_lock(self, session, key: str) -> bool:
        """数据目录文件锁(与渠道 connector 现行行为一致);已持有同 key 锁时重入成功。"""
        if key in self._lock_handles:
            return True
        bind = session.get_bind()
        database_path = getattr(getattr(bind, "url", None), "database", None)
        if not database_path or database_path == ":memory:":
            logger.error("方言 %s 无原生 advisory lock 且数据库非文件,无法提供进程锁", self.name)
            return False
        lock_path = (
            Path(database_path).resolve().with_name(f"{Path(database_path).name}.{key}.lock")
        )
        handle = lock_path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                if handle.read(1) == b"":
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            handle.close()
            return False
        self._lock_handles[key] = handle
        return True

    def release_advisory_lock(self, session, key: str) -> None:
        handle = self._lock_handles.pop(key, None)
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class SQLiteDialect(BaseDialect):
    """SQLite:文件锁 + date(localtime) 日分桶(均为现行行为,保持不变)。"""

    supports_partial_index = True

    def __init__(self) -> None:
        super().__init__("sqlite")

    def engine_kwargs(self, url: str) -> dict[str, Any]:
        return {"connect_args": {"check_same_thread": False, "timeout": 30}}

    def day_bucket(self, column) -> Any:
        # 服务器本地时区自然日(既有行为)
        return func.date(column, "localtime")


class PostgresDialect(BaseDialect):
    """PostgreSQL/高斯:psycopg3 驱动(postgresql+psycopg://),全能力。"""

    supports_partial_index = True

    def __init__(self) -> None:
        super().__init__("postgresql")

    def day_bucket(self, column) -> Any:
        tz = (get_settings().app_timezone or "").strip()
        if tz:
            # created_at 为 naive UTC:先按 UTC 还原时刻,再换算到应用时区取自然日
            return cast(func.timezone(tz, func.timezone("UTC", column)), Date)
        # 未配置时区:按服务器本地固定偏移换算(与 SQLite localtime 语义对齐)
        offset = datetime.now().astimezone().utcoffset() or timedelta(0)
        interval = literal(offset, type_=Interval)
        return cast(column + interval, Date)

    def acquire_advisory_lock(self, session, key: str) -> bool:
        locked = session.execute(
            text("SELECT pg_try_advisory_lock(hashtext(:key))"),
            {"key": key},
        ).scalar()
        return bool(locked)

    def release_advisory_lock(self, session, key: str) -> None:
        session.execute(text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": key})


class GenericDialect(BaseDialect):
    """未注册后端的回退:ORM 通用实现 + 文件锁。

    扩展新数据库(MySQL/达梦等):继承 BaseDialect 按能力覆写后
    register_dialect("<backend>") 注册即可;supports_partial_index=False 的
    后端须在建表 DDL 层跳过 models.py 的部分唯一索引并改走代码层校验。
    """


_DIALECTS: dict[str, DatabaseDialect] = {}


def register_dialect(backend_name: str, dialect: DatabaseDialect) -> None:
    """注册方言提供者(backend_name 即 engine.url.get_backend_name())。"""
    _DIALECTS[backend_name] = dialect


def get_dialect(backend_name: str) -> DatabaseDialect:
    """按 SQLAlchemy backend 名取方言提供者;未注册回退 GenericDialect。"""
    dialect = _DIALECTS.get(backend_name)
    if dialect is None:
        dialect = _DIALECTS.setdefault(backend_name, GenericDialect(backend_name))
    return dialect


register_dialect("sqlite", SQLiteDialect())
register_dialect("postgresql", PostgresDialect())
