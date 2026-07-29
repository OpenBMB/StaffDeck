"""数据库方言可插拔层.

ORM 优先:能用 SQLAlchemy ORM/标准 SQL 表达的一律走通用实现,无法通用的
少数方言点收口到本模块的 DatabaseDialect 提供者。新增数据库 = 新增一个小
适配器并 register_dialect 注册,业务代码零改动:

- postgresql(含高斯):全能力,即 PostgresDialect(psycopg3 驱动,URL
  postgresql+psycopg://);高斯直接复用该适配器。
- mysql:不预装驱动(按需装 pymysql);不支持部分唯一索引——
  models.py 的 uq_model_configs_tenant_default 只在 sqlite/postgresql 方言
  创建,其它后端由 init_db 的启动校验兜底;advisory lock 可用 GET_LOCK(key, 0)
  实现,未实现前文件锁会响亮拒绝(见 BaseDialect.acquire_advisory_lock)。
- dm(达梦):dmPython 驱动(dm+dmPython://),Oracle 系语法;同样不支持
  部分唯一索引;无原生锁实现前同样响亮拒绝。

未注册的后端名回退 GenericDialect:ORM 通用实现开箱可用;适配器只补充各自
特性(原生 advisory lock、部分索引、日期函数等)。

日分桶口径注意:SQLite 为服务器本地自然日(func.date(col,'localtime'));
Postgres 按 app_timezone(缺省=服务器本地固定偏移);Generic 为
cast(col, Date) 即数据库服务器时区自然日(云上实例常为 UTC),与 SQLite
口径可能不同——跨库迁移数据后按日统计会整体平移,属预期差异。
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
    session_scoped_advisory_lock: bool  # advisory lock 是否随连接存活(方言须常驻专属连接)

    def engine_kwargs(self, url: str) -> dict[str, Any]: ...

    def day_bucket(self, column) -> Any: ...

    def json_config_get(self, config: dict | None, key: str) -> Any: ...

    def json_config_set(self, config: dict | None, key: str, value: Any) -> dict: ...

    def json_config_remove(self, config: dict | None, key: str) -> dict: ...

    def acquire_advisory_lock(self, session, key: str) -> bool: ...

    def release_advisory_lock(self, session, key: str) -> None: ...

    def check_advisory_lock(self, session, key: str) -> bool: ...


class BaseDialect:
    """通用默认实现:JSON 读改写 = Python 侧读-改-写;日分桶 = cast(col, Date)
    (标准 SQL,按数据库服务器时区取日);advisory lock = SQLite 数据库文件旁的
    文件锁(非 SQLite 文件库响亮拒绝,由具体适配器补原生锁实现)。"""

    name = "generic"
    supports_partial_index = False
    session_scoped_advisory_lock = False

    def __init__(self, backend_name: str = "generic") -> None:
        self.name = backend_name
        self._lock_handles: dict[str, tuple[int, Any]] = {}

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
        """SQLite 数据库文件旁的文件锁;已持有同 key 锁时重入成功。

        只对 SQLite 文件库有效:url.database 对 MySQL/达梦等是库名而非文件路径,
        直接拒绝(响亮失败),避免把库名当路径在 CWD 下生成各进程互不可见的锁文件,
        静默破坏单实例保证。

        fork 防护:继承自父进程的句柄不算持有(只关闭不解锁——解锁会把父进程
        的锁一起放掉),随后按本进程身份真实抢锁。
        """
        held = self._lock_handles.get(key)
        if held is not None:
            held_pid, held_handle = held
            if held_pid == os.getpid():
                return True
            held_handle.close()
            self._lock_handles.pop(key, None)
        bind = session.get_bind()
        url = getattr(bind, "url", None)
        backend_name = url.get_backend_name() if url is not None else ""
        database_path = getattr(url, "database", None)
        if backend_name != "sqlite" or not database_path or database_path == ":memory:":
            logger.error(
                "方言 %s 无原生 advisory lock 且数据库非 SQLite 文件库,无法提供进程锁;"
                "请为该后端实现原生 advisory lock 适配器",
                self.name,
            )
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
        self._lock_handles[key] = (os.getpid(), handle)
        return True

    def release_advisory_lock(self, session, key: str) -> None:
        held = self._lock_handles.pop(key, None)
        if held is None:
            return
        held_pid, handle = held
        if held_pid != os.getpid():
            # fork 子进程:仅关闭继承句柄,不解父进程的锁
            handle.close()
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

    def check_advisory_lock(self, session, key: str) -> bool:
        """文件锁无静默失效模式(句柄随进程存活):只校验本进程仍持有句柄。"""
        held = self._lock_handles.get(key)
        return held is not None and held[0] == os.getpid() and not held[1].closed


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
    """PostgreSQL/高斯:psycopg3 驱动(postgresql+psycopg://),全能力。

    advisory lock 属于**连接**而非会话:持锁期间由本方言常驻一条专属
    `engine.connect()` Connection(AUTOCOMMIT——每条语句即提即放,不留
    idle-in-transaction;Connection 不归池,commit/rollback 不脱钩,
    `pg_backend_pid()` 全程稳定)。绝不能用 Session.commit() 代替:
    commit 会把连接归还池中,锁静默释放或后续 check 取到别的连接。"""

    supports_partial_index = True
    session_scoped_advisory_lock = True

    def __init__(self) -> None:
        super().__init__("postgresql")
        # key -> (pid, Connection);fork 防护同 BaseDialect 文件锁
        self._pg_lock_conns: dict[str, tuple[int, Any]] = {}

    def engine_kwargs(self, url: str) -> dict[str, Any]:
        return {
            # 陈旧连接(PG 重启/idle 超时后)首用前先 ping,失效即重连而非报错
            "pool_pre_ping": True,
            # 主动回收长闲连接,避开服务端/LB 的 idle 掐断
            "pool_recycle": 1800,
            # 后台线程随 binding 数增长,默认 5+10 不够
            "pool_size": 10,
            "max_overflow": 20,
        }

    def day_bucket(self, column) -> Any:
        tz = (get_settings().app_timezone or "").strip()
        if tz:
            # created_at 为 naive UTC:先按 UTC 还原时刻,再换算到应用时区取自然日
            return cast(func.timezone(tz, func.timezone("UTC", column)), Date)
        # 未配置时区:按服务器本地固定偏移换算(与 SQLite localtime 语义对齐)
        offset = datetime.now().astimezone().utcoffset() or timedelta(0)
        interval = literal(offset, type_=Interval)
        return cast(column + interval, Date)

    def _engine_of(self, session):
        bind = session.get_bind()
        return getattr(bind, "engine", bind)

    def acquire_advisory_lock(self, session, key: str) -> bool:
        """以专属 AUTOCOMMIT Connection 取锁并常驻;重入成功,fork 继承不作数。"""
        held = self._pg_lock_conns.get(key)
        if held is not None:
            held_pid, held_conn = held
            if held_pid == os.getpid():
                return True
            # fork 子进程:继承的连接只丢弃引用,不在共享 socket 上做任何操作
            self._pg_lock_conns.pop(key, None)
        # 双 int4 键形式(classid=hashtext(key), objid=0):便于在 pg_locks 中直接校验
        conn = self._engine_of(session).connect().execution_options(
            isolation_level="AUTOCOMMIT"
        )
        try:
            locked = conn.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:key), 0)"),
                {"key": key},
            ).scalar()
        except Exception:
            conn.close()
            raise
        if not locked:
            conn.close()
            return False
        self._pg_lock_conns[key] = (os.getpid(), conn)
        return True

    def release_advisory_lock(self, session, key: str) -> None:
        held = self._pg_lock_conns.pop(key, None)
        if held is None:
            return
        held_pid, conn = held
        if held_pid != os.getpid():
            # fork 子进程:仅丢弃引用,不解父进程的锁
            return
        try:
            conn.execute(text("SELECT pg_advisory_unlock(hashtext(:key), 0)"), {"key": key})
        finally:
            conn.close()

    def check_advisory_lock(self, session, key: str) -> bool:
        """校验本进程常驻的那条连接仍持有该 advisory lock。

        必须在**同一条专属 Connection** 上查 pg_locks(pid 稳定);连接被服务端
        掐断后锁已静默释放,查询会抛错——同样判 False。
        """
        held = self._pg_lock_conns.get(key)
        if held is None:
            return False
        held_pid, conn = held
        if held_pid != os.getpid():
            return False
        try:
            row = conn.execute(
                text(
                    "SELECT 1 FROM pg_locks "
                    "WHERE locktype = 'advisory' AND pid = pg_backend_pid() "
                    "AND classid = hashtext(:key) AND objid = 0"
                ),
                {"key": key},
            ).first()
            return row is not None
        except Exception:
            logger.exception("PG advisory lock 存活校验失败 key=%s", key)
            return False


class GenericDialect(BaseDialect):
    """未注册后端的回退:ORM 通用实现;文件锁仅对 SQLite 文件库有效。

    扩展新数据库(MySQL/达梦等):继承 BaseDialect 按能力覆写后
    register_dialect("<backend>") 注册即可。supports_partial_index=False 的
    后端不会创建 models.py 的部分唯一索引,由 init_db 启动校验兜底;
    未实现原生 advisory lock 时 BaseDialect 会响亮拒绝而非静默错锁。
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
