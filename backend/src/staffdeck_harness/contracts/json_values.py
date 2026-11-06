"""Explicit, recursive JSON wire contract shared by modules and the runtime.

Do not stringify arbitrary objects: that hides module bugs and can leak repr data.
"""
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from math import isfinite
from uuid import UUID

from .errors import ModuleSdkError


class JsonValueError(ModuleSdkError):
    code = "JSON_CONTRACT_INVALID"

    def __init__(self, path, reason):
        super().__init__(f"JSON 数据契约错误：{path}（{reason}）", details={
            "path": path, "reason": reason, "retryable": False,
            "business_replay_allowed": False,
        })


def json_safe(value, *, path="$", _active=None, _depth=0):
    """Return a detached JSON tree; preserve precision and reject unsafe values."""
    if _depth > 64:
        raise JsonValueError(path, "maximum nesting exceeded")
    if isinstance(value, Enum):
        return json_safe(value.value, path=path, _active=_active, _depth=_depth + 1)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise JsonValueError(path, "non-finite number")
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise JsonValueError(path, "non-finite decimal")
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    active = set() if _active is None else _active
    identity = id(value)
    if identity in active:
        raise JsonValueError(path, "cyclic reference")
    active.add(identity)
    try:
        if is_dataclass(value) and not isinstance(value, type):
            value = {field.name: getattr(value, field.name) for field in fields(value)}
        if isinstance(value, Mapping):
            result = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise JsonValueError(path, "object key must be a string")
                result[key] = json_safe(item, path=f"{path}.{key}", _active=active, _depth=_depth + 1)
            return result
        if isinstance(value, (list, tuple)):
            return [json_safe(item, path=f"{path}[{i}]", _active=active, _depth=_depth + 1)
                    for i, item in enumerate(value)]
        raise JsonValueError(path, f"unsupported type {type(value).__name__}")
    finally:
        active.remove(identity)
