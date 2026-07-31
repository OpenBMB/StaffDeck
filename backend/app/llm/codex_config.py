from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.llm.model_protocols import ModelApiProtocol

_CODEX_CONFIG_MAX_BYTES = 1024 * 1024
_WIRE_API_PROTOCOLS = {
    "chat": ModelApiProtocol.OPENAI_CHAT_COMPLETIONS,
    "responses": ModelApiProtocol.OPENAI_RESPONSES,
}


def load_local_codex_model_config() -> dict[str, Any]:
    config_path = _codex_config_path()
    try:
        if not config_path.is_file():
            raise FileNotFoundError
        if config_path.stat().st_size > _CODEX_CONFIG_MAX_BYTES:
            raise ValueError
        with config_path.open("rb") as config_file:
            config = tomllib.load(config_file)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="CODEX_CONFIG_NOT_FOUND") from exc
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="CODEX_CONFIG_INVALID") from exc

    provider_id = _required_string(config.get("model_provider"), "CODEX_CONFIG_PROVIDER_MISSING")
    model = _required_string(config.get("model"), "CODEX_CONFIG_MODEL_MISSING")
    providers = config.get("model_providers")
    if not isinstance(providers, dict):
        raise HTTPException(status_code=422, detail="CODEX_CONFIG_PROVIDER_MISSING")
    provider = providers.get(provider_id)
    if not isinstance(provider, dict):
        raise HTTPException(status_code=422, detail="CODEX_CONFIG_PROVIDER_MISSING")

    wire_api = _required_string(provider.get("wire_api"), "CODEX_CONFIG_WIRE_API_MISSING")
    protocol = _WIRE_API_PROTOCOLS.get(wire_api)
    if protocol is None:
        raise HTTPException(status_code=422, detail="CODEX_CONFIG_WIRE_API_UNSUPPORTED")

    base_url = _required_string(provider.get("base_url"), "CODEX_CONFIG_BASE_URL_MISSING")
    bearer_token = _required_string(
        provider.get("experimental_bearer_token"), "CODEX_CONFIG_API_KEY_UNAVAILABLE"
    )
    provider_name = _optional_string(provider.get("name")) or provider_id
    protocol_options: dict[str, Any] = {}
    if protocol is ModelApiProtocol.OPENAI_RESPONSES:
        protocol_options["store"] = not bool(config.get("disable_response_storage"))
        protocol_options["json_mode"] = "prompt"

    return {
        "name": f"Codex {provider_name}",
        "api_protocol": protocol,
        "base_url": base_url,
        "api_key": bearer_token,
        "model": model,
        "protocol_options": protocol_options,
    }


def _codex_config_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    home = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    return home / "config.toml"


def _required_string(value: Any, detail: str) -> str:
    normalized = _optional_string(value)
    if normalized is None:
        raise HTTPException(status_code=422, detail=detail)
    return normalized


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None
