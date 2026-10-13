"""Compose the public domain callback from explicit deployment identity."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from app.public_api.pilotdeck_domain_host import (
    PilotDeckDomainHostClient,
    bind_pilotdeck_domain_host,
)


@dataclass(frozen=True)
class FixedPilotDeckDomainHostClient(PilotDeckDomainHostClient):
    tenant_id: str = ""
    actor_user_id: str = ""
    agent_id: str = ""
    project_bindings: bool = False

    def permits(self, tenant_id, actor_user_id, agent_id):
        return (tenant_id, actor_user_id) == (self.tenant_id, self.actor_user_id) and bool(agent_id) \
            and (self.project_bindings or agent_id == self.agent_id)

    def file_parse(self, *, tenant_id, actor_user_id, agent_id, filename,
                   content_base64, media_type):
        if not self.permits(tenant_id, actor_user_id, agent_id):
            raise RuntimeError("PUBLIC_HOST_FIXED_IDENTITY_MISMATCH")
        return super().file_parse(
            tenant_id=tenant_id, actor_user_id=actor_user_id, agent_id=agent_id,
            filename=filename, content_base64=content_base64, media_type=media_type,
        )

    def plan_sop_route(self, *, tenant_id, actor_user_id, agent_id, message,
                       session, routing_skills, conversation_context):
        if not self.permits(tenant_id, actor_user_id, agent_id):
            raise RuntimeError("PUBLIC_HOST_FIXED_IDENTITY_MISMATCH")
        return super().plan_sop_route(
            tenant_id=tenant_id, actor_user_id=actor_user_id, agent_id=agent_id,
            message=message, session=session, routing_skills=routing_skills,
            conversation_context=conversation_context,
        )


def bind_pilotdeck_domain_client(public_app, env=None) -> None:
    values = os.environ if env is None else env
    if values.get("PILOTDECK_DOMAIN_HOST_ENABLED", "false").lower() not in {"true", "1"}:
        return
    project_bindings = values.get("STAFFDECK_PROJECT_BINDING_ENABLED") == "true"
    required = ("PILOTDECK_GATEWAY_URL", "PILOTDECK_GATEWAY_TOKEN_PATH",
                "PILOTDECK_USER_ID", "STAFFDECK_COPY_TENANT_ID",
                "STAFFDECK_COPY_ACTOR_USER_ID") + (() if project_bindings else ("STAFFDECK_COPY_TARGET_AGENT_ID",))
    if any(not str(values.get(name, "")).strip() for name in required):
        raise RuntimeError("PD_DOMAIN_HOST_CONFIG_REQUIRED")
    url = urlsplit(values["PILOTDECK_GATEWAY_URL"])
    if url.scheme not in {"http", "https", "ws", "wss"} or not url.netloc \
            or url.username or url.password or url.query or url.fragment \
            or url.path not in {"", "/", "/ws"}:
        raise RuntimeError("PD_DOMAIN_HOST_ORIGIN_INVALID")
    token_path = Path(values["PILOTDECK_GATEWAY_TOKEN_PATH"])
    if not token_path.is_absolute():
        raise RuntimeError("PD_DOMAIN_HOST_TOKEN_PATH_INVALID")
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError:
        raise RuntimeError("PD_DOMAIN_HOST_CREDENTIAL_UNAVAILABLE") from None
    if not token:
        raise RuntimeError("PD_DOMAIN_HOST_CREDENTIAL_UNAVAILABLE")
    scheme = {"ws": "http", "wss": "https"}.get(url.scheme, url.scheme)
    client = FixedPilotDeckDomainHostClient(
        origin=f"{scheme}://{url.netloc}", bridge_token=token,
        pilotdeck_user_id=values["PILOTDECK_USER_ID"],
        tenant_id=values["STAFFDECK_COPY_TENANT_ID"],
        actor_user_id=values["STAFFDECK_COPY_ACTOR_USER_ID"],
        agent_id=values.get("STAFFDECK_COPY_TARGET_AGENT_ID", ""), project_bindings=project_bindings,
    )
    bind_pilotdeck_domain_host(client)
    public_app.state.pilotdeck_domain_host = client
