"""Explicit SD domain callback binding to the selected PD host runtime.

The account service credential stays server-side. The public API's original
credential/KB PEP runs before this callback; this module owns no job or model.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx

from app.core.turn_planner import TurnPlanner
from app.llm.client import LLMClient, LLMError, _prepare_user_input, _request_messages, _with_json_mode_instruction
from app.llm.stage_protocol import unified_system_prompt
from app.session.session_schema import TurnPlan


@dataclass(frozen=True)
class PilotDeckDomainHostClient:
    origin: str
    bridge_token: str
    pilotdeck_user_id: str
    transport: httpx.BaseTransport | None = None

    def _call(self, client: httpx.Client, *, principal: dict[str, str], operation: str,
              input: dict[str, Any]) -> httpx.Response:
        try:
            response = client.post(
                "/api/module-host/call",
                headers={"Authorization": f"Bearer {self.bridge_token}"},
                json={"principal": principal, "operation": operation, "input": input},
            )
        except httpx.RequestError as exc:
            raise RuntimeError(f"PUBLIC_HOST_{operation.upper()}_UNAVAILABLE") from exc
        if not response.is_success:
            raise RuntimeError(f"PUBLIC_HOST_{operation.upper()}_FAILED: {response.status_code}")
        return response

    def plan_sop_route(
        self, *, tenant_id: str, actor_user_id: str, agent_id: str,
        message: str, session: Any, routing_skills: list[Any],
        conversation_context: dict[str, Any] | None,
    ) -> TurnPlan:
        """Use the selected PD model Port for the original SD planner payload and normalization."""
        if not all((self.origin, self.bridge_token, self.pilotdeck_user_id,
                    tenant_id, actor_user_id, agent_id)):
            raise RuntimeError("PUBLIC_HOST_IDENTITY_UNBOUND")
        principal = {"pilotDeckUserId": self.pilotdeck_user_id,
                     "tenantId": tenant_id, "actorUserId": actor_user_id, "agentId": agent_id}
        planner = TurnPlanner()
        payload = planner.prepare_payload(message, session, routing_skills,
                                          conversation_context, None, [])
        with httpx.Client(base_url=self.origin, transport=self.transport,
                          timeout=60, follow_redirects=False) as client:
            catalog = self._call(client, principal=principal,
                                 operation="list_model_catalog", input={}).json()
            items = catalog.get("data") if isinstance(catalog, dict) else None
            if not isinstance(items, list):
                raise RuntimeError("PUBLIC_HOST_MODEL_CATALOG_INVALID")
            available = [item for item in items if isinstance(item, dict)
                         and item.get("available", item.get("enabled")) is True]
            default = catalog.get("defaultSelection")
            if isinstance(default, dict):
                if default.get("mode", "model") != "model" or not isinstance(default.get("provider"), str) \
                        or not isinstance(default.get("model"), str):
                    raise RuntimeError("PUBLIC_HOST_MODEL_SELECTION_INVALID")
                defaults = [item for item in items if isinstance(item, dict)
                            and item.get("id") == f"{default['provider']}/{default['model']}"
                            and item.get("provider") == default["provider"] and item.get("model") == default["model"]]
            else:
                defaults = [item for item in items if isinstance(item, dict) and item.get("is_default") is True]
            if len(defaults) != 1:
                raise RuntimeError("PUBLIC_HOST_MODEL_SELECTION_UNBOUND")
            selected = defaults[0]
            if selected not in available:
                raise RuntimeError("PUBLIC_HOST_MODEL_SELECTION_INVALID")
            provider, model, model_id = selected.get("provider"), selected.get("model"), selected.get("id")
            if not isinstance(provider, str) or not isinstance(model, str) \
                    or model_id != f"{provider}/{model}" or selected.get("available", selected.get("enabled")) is not True:
                raise RuntimeError("PUBLIC_HOST_MODEL_SELECTION_INVALID")
            class BoundModel(LLMClient):
                # Reuse the owner's JSON decoding/repair without constructing an
                # SD model config, driver, or provider client.
                def __init__(self) -> None:
                    pass

                def generate_text(self, system_prompt: str, user_payload: dict[str, Any],
                                  cancellation=None, **kwargs) -> str:
                    return self._generate_json_candidate(system_prompt, user_payload, False, cancellation)

                def _generate_json_candidate(self, system_prompt: str, user_payload: dict[str, Any],
                                             json_mode_supported: bool, cancellation=None) -> str:
                    context_messages, serialized = _prepare_user_input(user_payload)
                    original_messages = _with_json_mode_instruction(
                        _request_messages(system_prompt, context_messages, serialized))
                    canonical_messages = []
                    for item in original_messages[1:]:
                        if item.get("role") not in {"user", "assistant"} or not isinstance(item.get("content"), str):
                            raise RuntimeError("PUBLIC_HOST_SOP_PROMPT_UNSUPPORTED")
                        canonical_messages.append({"role": item["role"],
                            "content": [{"type": "text", "text": item["content"]}]})
                    response = self_outer._call(client, principal=principal,
                        operation="model_stream", input={
                            "requestId": f"sop-route-{uuid4().hex}", "modelId": model_id,
                            "request": {"provider": provider, "model": model,
                                        "systemPrompt": original_messages[0]["content"],
                                        "messages": canonical_messages,
                                        "stream": True},
                        })
                    if not response.headers.get("content-type", "").startswith("application/x-ndjson"):
                        raise RuntimeError("PUBLIC_HOST_MODEL_STREAM_INVALID")
                    fragments: list[str] = []
                    finished = False
                    for line in response.text.splitlines():
                        if not line.strip():
                            continue
                        try:
                            event = json.loads(line)
                        except ValueError as exc:
                            raise RuntimeError("PUBLIC_HOST_MODEL_STREAM_INVALID") from exc
                        if not isinstance(event, dict):
                            raise RuntimeError("PUBLIC_HOST_MODEL_STREAM_INVALID")
                        if event.get("type") == "text_delta" and isinstance(event.get("text"), str):
                            fragments.append(event["text"])
                        elif event.get("type") == "message_end":
                            if event.get("finishReason") != "stop":
                                raise RuntimeError("PUBLIC_HOST_MODEL_INCOMPLETE")
                            finished = True
                        elif event.get("type") == "error":
                            raise RuntimeError("PUBLIC_HOST_MODEL_FAILED")
                    if not finished or not fragments:
                        raise RuntimeError("PUBLIC_HOST_MODEL_INCOMPLETE")
                    return "".join(fragments)

            self_outer = self
            try:
                plan = planner._generate_validated_plan(BoundModel(), unified_system_prompt(), payload)
            except (LLMError, ValueError, TypeError) as exc:
                raise RuntimeError("PUBLIC_HOST_SOP_PLAN_INVALID") from exc
        return planner.normalize_plan(plan, message, session, routing_skills, [], "normal", None)

    def read_sop_authority(self, *, tenant_id: str, actor_user_id: str, agent_id: str,
                           credential_id: str, context: dict[str, Any]) -> dict[str, Any]:
        from app.public_api.errors import PublicAPIError

        with httpx.Client(base_url=self.origin, transport=self.transport,
                          timeout=10, follow_redirects=False) as client:
            try:
                response = client.post("/api/module-host/call",
                    headers={"Authorization": f"Bearer {self.bridge_token}"},
                    json={"principal": {"pilotDeckUserId": self.pilotdeck_user_id,
                          "tenantId": tenant_id, "actorUserId": actor_user_id, "agentId": agent_id},
                          "operation": "read_sop_authority",
                          "input": {**context, "admissionCredentialId": credential_id}})
                body = response.json()
            except (httpx.RequestError, ValueError) as exc:
                raise RuntimeError("PUBLIC_HOST_SOP_AUTHORITY_UNAVAILABLE") from exc
        if not response.is_success:
            code = body.get("code") if isinstance(body, dict) else None
            raise PublicAPIError(response.status_code,
                code if isinstance(code, str) and code.startswith("SOP_") else "SOP_AUTHORITY_HOST_REJECTED",
                "The admitted SOP owner rejected this request.")
        return body

    def file_parse(
        self, *, tenant_id: str, actor_user_id: str, agent_id: str,
        filename: str, content_base64: str, media_type: str | None,
    ) -> dict[str, Any]:
        if not all((self.origin, self.bridge_token, self.pilotdeck_user_id,
                    tenant_id, actor_user_id, agent_id, filename)):
            raise RuntimeError("PUBLIC_HOST_IDENTITY_UNBOUND")
        with httpx.Client(base_url=self.origin, transport=self.transport,
                          timeout=60, follow_redirects=False) as client:
            response = client.post(
                "/api/module-host/call",
                headers={"Authorization": f"Bearer {self.bridge_token}"},
                json={
                    "principal": {
                        "pilotDeckUserId": self.pilotdeck_user_id,
                        "tenantId": tenant_id,
                        "actorUserId": actor_user_id,
                        "agentId": agent_id,
                    },
                    "operation": "file_parse",
                    "input": {"filename": filename, "content_base64": content_base64,
                              "media_type": media_type, "max_bytes": 20 * 1024 * 1024},
                },
            )
        if not response.is_success:
            try:
                error = response.json()
            except ValueError:
                error = response.text
            raise RuntimeError(f"PUBLIC_HOST_FILE_PARSE_FAILED: {response.status_code} {error}")
        value = response.json()
        if not isinstance(value, dict) or not isinstance(value.get("text"), str) \
                or not isinstance(value.get("metadata"), dict) \
                or not isinstance(value["metadata"].get("fileType"), str):
            raise RuntimeError("PUBLIC_HOST_FILE_PARSE_INVALID_RESPONSE")
        return value


_bound_client: PilotDeckDomainHostClient | None = None


def bind_pilotdeck_domain_host(client: PilotDeckDomainHostClient) -> None:
    """Called once by the SD deployment composition root for the enabled profile."""
    if not isinstance(client, PilotDeckDomainHostClient):
        raise TypeError("PilotDeckDomainHostClient is required")
    global _bound_client
    _bound_client = client


def bind_pilotdeck_domain_host_from_runtime(
    *, gateway_url: str, token_path: str, pilotdeck_user_id: str,
) -> None:
    """Bind the existing authenticated Gateway HTTP Port from deployment inputs."""
    parsed = urlsplit(gateway_url)
    if parsed.scheme not in {"ws", "wss", "http", "https"} or not parsed.hostname \
            or parsed.username or parsed.password or not pilotdeck_user_id.strip():
        raise RuntimeError("PUBLIC_HOST_RUNTIME_BINDING_INVALID")
    token = Path(token_path).read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("PUBLIC_HOST_RUNTIME_TOKEN_MISSING")
    scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme, parsed.scheme)
    origin = urlunsplit((scheme, parsed.netloc, "", "", ""))
    bind_pilotdeck_domain_host(PilotDeckDomainHostClient(origin, token, pilotdeck_user_id))


def require_pilotdeck_domain_host() -> PilotDeckDomainHostClient:
    if _bound_client is None:
        raise RuntimeError("PUBLIC_HOST_FILE_PORT_UNBOUND")
    return _bound_client
