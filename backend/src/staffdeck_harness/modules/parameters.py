"""Validate public module parameters before persistence, rendering or discovery."""
from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit


def validate_public_parameters(configs):
    if not isinstance(configs, Mapping):
        raise ValueError("module_configs must be an object")

    def check(value):
        if isinstance(value, Mapping):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise ValueError("module parameter names must be strings")
                if any(word in key.lower() for word in ("password", "secret", "token", "api_key", "authorization", "credential")) and not key.endswith("_ref"):
                    raise ValueError("模块参数只能填写部署凭证引用，不允许明文凭证")
                check(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                check(child)
        elif isinstance(value, str):
            if "://" in value:
                parsed = urlsplit(value)
                if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
                    raise ValueError("模块地址不能携带凭证、查询参数或片段")
    for params in configs.values():
        if not isinstance(params, Mapping):
            raise ValueError("each module configuration must be an object")
        check(params)
