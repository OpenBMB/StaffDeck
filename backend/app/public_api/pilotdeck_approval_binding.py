"""Deployment glue: read the normal Gateway credential, never mint an approval token."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

from app.public_api.pilotdeck_approvals import PilotDeckApprovalClient


def bind_pilotdeck_approval_client(public_app, env=None) -> None:
    values = os.environ if env is None else env
    if values.get("PILOTDECK_APPROVAL_BRIDGE_ENABLED", "false").lower() not in {"true", "1"}:
        return
    project_bindings = values.get("STAFFDECK_PROJECT_BINDING_ENABLED") == "true"
    names = ("PILOTDECK_GATEWAY_URL", "PILOTDECK_GATEWAY_TOKEN_PATH",
             "STAFFDECK_COPY_TENANT_ID") + (() if project_bindings else ("STAFFDECK_COPY_TARGET_AGENT_ID",))
    if any(not str(values.get(name, "")).strip() for name in names):
        raise RuntimeError("PD_APPROVAL_BRIDGE_CONFIG_REQUIRED")
    url = urlsplit(values["PILOTDECK_GATEWAY_URL"])
    if url.scheme not in {"http", "https", "ws", "wss"} or not url.netloc \
            or url.username or url.password or url.query or url.fragment or url.path not in {"", "/", "/ws"}:
        raise RuntimeError("PD_APPROVAL_BRIDGE_ORIGIN_INVALID")
    token_path = Path(values["PILOTDECK_GATEWAY_TOKEN_PATH"])
    if not token_path.is_absolute():
        raise RuntimeError("PD_APPROVAL_BRIDGE_TOKEN_PATH_INVALID")
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError:
        raise RuntimeError("PD_APPROVAL_GATEWAY_CREDENTIAL_UNAVAILABLE") from None
    if not token:
        raise RuntimeError("PD_APPROVAL_GATEWAY_CREDENTIAL_UNAVAILABLE")
    scheme = {"ws": "http", "wss": "https"}.get(url.scheme, url.scheme)
    public_app.state.pilotdeck_approval_client = PilotDeckApprovalClient(
        origin=f"{scheme}://{url.netloc}", bridge_token=token,
        tenant_id=values["STAFFDECK_COPY_TENANT_ID"], agent_id=values.get("STAFFDECK_COPY_TARGET_AGENT_ID", ""),
        project_bindings=project_bindings,
    )
