"""openGauss(高斯)SQLAlchemy 方言兼容层.

openGauss 使用 PostgreSQL 协议,但有两处与上游 PostgreSQL 不同,直接用默认的
``postgresql+psycopg`` 驱动无法建连/初始化:

1. ``select version()`` 返回 ``(openGauss 5.0.0 build ) compiled at ...``,
   SQLAlchemy 的 PG 方言用正则解析版本失败(无法确定 server_version_info);
2. 服务端返回的 version/设置值在 ``client_encoding`` 未显式指定时可能以 bytes
   返回(SQL_ASCII 初始化库),SQLAlchemy 的版本解析会因此报
   ``cannot use a string pattern on a bytes-like object``。

本模块提供一个最小子类:继承 psycopg3 的 PostgreSQL 方言,版本解析失败时回退到
openGauss 5.x 所基于的 PostgreSQL 9.2 兼容级别(9, 2, 4),并通过
``opengauss+psycopg://`` URL scheme 注册。其余行为(类型编译、RETURNING、事务)
全部继承 PG 方言;``client_encoding=utf8`` 由 ``app.db.dialect.OpenGaussDialect``
的 engine_kwargs 注入。

用法:``DATABASE_URL="opengauss+psycopg://user:pass@host:5432/postgres"``。
psycopg 未安装时本模块不做任何注册(SQLite 桌面端无额外依赖),驱动检查由
``app.db.dialect.ensure_driver_available`` 在启动时给出安装指引。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:  # psycopg 为可选依赖([postgres] extra)
    from sqlalchemy.dialects.postgresql.psycopg import PGDialect_psycopg as _PGDialectPsycopg
except ImportError:  # pragma: no cover - 未安装 psycopg 时走 SQLite/其它后端
    _PGDialectPsycopg = None  # type: ignore[assignment]

# openGauss 5.x 基于 PostgreSQL 9.2 内核,版本解析失败时按该兼容级别工作
OPENGAUSS_PG_COMPAT_VERSION = (9, 2, 4)


if _PGDialectPsycopg is not None:

    class OpenGaussPGDialect(_PGDialectPsycopg):  # type: ignore[misc, valid-type]
        """openGauss 的 psycopg3 方言:PG 方言 + 版本串/编码兼容。"""

        # SQLAlchemy 2.x 编译缓存需要显式声明;显式继承 PG 方言行为
        supports_statement_cache = True

        def _get_server_version_info(self, connection):
            """解析服务端版本;openGauss 的 version 串无法按 PG 正则解析。

            openGauss 返回形如 ``(openGauss 5.0.0 build ) compiled at ...`` 的
            字符串(且未设 client_encoding 时可能是 bytes);解析失败时回退到
            PostgreSQL 9.2 兼容级别,保证 PG 方言按 openGauss 支持的语法生成 SQL。
            """
            try:
                return super()._get_server_version_info(connection)
            except Exception:  # noqa: BLE001 - 任何解析失败都回退兼容级别
                logger.debug("openGauss: 无法解析服务端版本,回退 PostgreSQL 9.2 兼容级别")
                return OPENGAUSS_PG_COMPAT_VERSION

    from sqlalchemy.dialects import registry as _dialect_registry

    # URL: opengauss+psycopg://user:pass@host:5432/dbname
    #
    # 注意:这里不用 registry.register(modulepath, objname)——该 API 在引擎创建时
    # 按模块路径重新 import,而 create_engine() 发生在 app.db 包 __init__ 导入
    # 过程中,会触发 "cannot access submodule 'db' of module 'app'" 的循环导入。
    # 直接注册类加载器(PluginLoader.impls 承载可调用对象)即可,与 SQLAlchemy
    # 内置方言的注册语义一致且无循环导入。
    _dialect_registry.impls["opengauss.psycopg"] = lambda: OpenGaussPGDialect

    OPENGAUSS_SQLALCHEMY_AVAILABLE = True
else:  # pragma: no cover - SQLite 默认安装路径
    OpenGaussPGDialect = None  # type: ignore[assignment,misc]
    OPENGAUSS_SQLALCHEMY_AVAILABLE = False
