"""Atomic channel configuration updates on the model's selected database bind."""
import json

from sqlalchemy import JSON, Text, cast, func, literal, update
from sqlalchemy.dialects.postgresql import JSONB, array

from app.db.models import ChannelBinding, utc_now


def patch_binding_config(db, binding_id, *, tenant_id=None, set_values=None, remove_keys=(),
                         expected_revision=None, require_active=False, expected_values=None, binding_values=None):
    dialect = db.get_bind(mapper=ChannelBinding).dialect.name
    column = ChannelBinding.config_json
    if dialect == "postgresql":
        expression = cast(func.coalesce(column, literal({}, type_=JSON)), JSONB)
        for key, value in (set_values or {}).items():
            expression = func.jsonb_set(expression, array([key], type_=Text), literal(value, type_=JSONB), True)
        for key in remove_keys:
            expression = expression.op("-")(literal(key, type_=Text))
    elif dialect == "sqlite":
        expression = func.coalesce(column, "{}")
        for key, value in (set_values or {}).items():
            expression = func.json_set(expression, "$." + json.dumps(key), func.json(json.dumps(value, ensure_ascii=False)))
        for key in remove_keys:
            expression = func.json_remove(expression, "$." + json.dumps(key))
    else:
        raise RuntimeError("Selected channel storage does not support atomic JSON updates")
    values = {"updated_at": utc_now()}
    if set_values or remove_keys:
        values["config_json"] = expression
    for key, value in (binding_values or {}).items():
        if key not in {"connected", "status"}:
            raise ValueError(f"unsupported binding runtime field: {key}")
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
