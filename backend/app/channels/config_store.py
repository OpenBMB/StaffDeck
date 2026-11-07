"""Atomic channel configuration updates on the model's selected database bind."""
import json

from sqlalchemy import JSON, Text, cast, func, literal, update
from sqlalchemy.dialects.postgresql import JSONB, array
from sqlmodel import select

from app.db.dialect import get_dialect
from app.db.models import ChannelBinding, utc_now


def _patch_binding_config_generic(db, binding_id, dialect, *, tenant_id=None, set_values=None,
                                  remove_keys=(), expected_revision=None, require_active=False,
                                  expected_values=None, binding_values=None):
    """其它方言的通用回退:方言助手 ORM 读-改-写 + 行锁 + config_revision CAS。

    sqlite/postgresql 有原生 JSON 函数,走上面的单条 UPDATE 原子更新;未注册后端
    (MySQL/达梦等)没有通用 JSON SQL,只能 Python 侧读-改-写。SELECT ... FOR UPDATE
    覆盖读取到写回的窗口(方言不支持时由 SQLAlchemy 自动省略),写回再以读到的
    config_revision 做 CAS,避免与并发补丁互相覆盖。
    """
    hint = get_dialect(dialect)
    binding = db.exec(
        select(ChannelBinding)
        .where(ChannelBinding.id == binding_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if binding is None:
        return False
    if tenant_id is not None and binding.tenant_id != tenant_id:
        return False
    if require_active and binding.status != "active":
        return False
    revision = binding.config_revision
    if expected_revision is not None and revision != expected_revision:
        return False
    for key, value in (expected_values or {}).items():
        if hint.json_config_get(binding.config_json, key) != value:
            return False
    config = binding.config_json
    for key, value in (set_values or {}).items():
        config = hint.json_config_set(config, key, value)
    for key in remove_keys:
        config = hint.json_config_remove(config, key)
    values = {"updated_at": utc_now()}
    if set_values or remove_keys:
        values["config_json"] = config
    values.update(binding_values or {})
    statement = (
        update(ChannelBinding)
        .where(
            ChannelBinding.id == binding_id,
            ChannelBinding.config_revision == revision,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return db.exec(statement).rowcount == 1


def patch_binding_config(db, binding_id, *, tenant_id=None, set_values=None, remove_keys=(),
                         expected_revision=None, require_active=False, expected_values=None, binding_values=None):
    for key in (binding_values or {}):
        if key not in {"connected", "status"}:
            raise ValueError(f"unsupported binding runtime field: {key}")
    bind = db.get_bind(mapper=ChannelBinding)
    # 按 URL backend 判定(SQLAlchemy 方言名):openGauss 复用 PG 方言,但其
    # jsonb_set 接受 varchar[] 与否/路径语义与上游不同,走下面的通用 ORM 回退。
    backend = bind.url.get_backend_name()
    column = ChannelBinding.config_json
    if backend == "postgresql":
        expression = cast(func.coalesce(column, literal({}, type_=JSON)), JSONB)
        for key, value in (set_values or {}).items():
            expression = func.jsonb_set(expression, array([key], type_=Text), literal(value, type_=JSONB), True)
        for key in remove_keys:
            expression = expression.op("-")(literal(key, type_=Text))
    elif backend == "sqlite":
        expression = func.coalesce(column, "{}")
        for key, value in (set_values or {}).items():
            expression = func.json_set(expression, "$." + json.dumps(key), func.json(json.dumps(value, ensure_ascii=False)))
        for key in remove_keys:
            expression = func.json_remove(expression, "$." + json.dumps(key))
    else:
        return _patch_binding_config_generic(
            db,
            binding_id,
            bind.dialect.name,
            tenant_id=tenant_id,
            set_values=set_values,
            remove_keys=remove_keys,
            expected_revision=expected_revision,
            require_active=require_active,
            expected_values=expected_values,
            binding_values=binding_values,
        )
    values = {"updated_at": utc_now()}
    if set_values or remove_keys:
        values["config_json"] = expression
    for key, value in (binding_values or {}).items():
        values[key] = value
    statement = update(ChannelBinding).where(ChannelBinding.id == binding_id)
    if tenant_id is not None:
        statement = statement.where(ChannelBinding.tenant_id == tenant_id)
    if expected_revision is not None:
        statement = statement.where(ChannelBinding.config_revision == expected_revision)
    if require_active:
        statement = statement.where(ChannelBinding.status == "active")
    for key, value in (expected_values or {}).items():
        item = column[key]
        item = item.as_boolean() if isinstance(value, bool) else item.as_integer() if isinstance(value, int) else item.as_string()
        statement = statement.where(item == value)
    return db.exec(statement.values(**values).execution_options(synchronize_session=False)).rowcount == 1
