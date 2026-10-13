"""Non-persistent model selection for an authorized Harness turn.

The enabled deployment binds one fixed PD identity. No SD ModelConfig or
provider credential is created here; every selection comes from the live PD
catalog and every completion uses its authenticated public model Port.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4
import json
import re

import httpx

from app.public_api.pilotdeck_domain_host import require_pilotdeck_domain_host
from app.public_api.pilotdeck_domain_binding import FixedPilotDeckDomainHostClient


class PublicModelPortError(RuntimeError):
    """A public Port failure with a stable, safe code for the Harness gateway."""

    def __init__(self, code: str, status: int = 502):
        self.code = code if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,80}", code) else "PUBLIC_HOST_MODEL_FAILED"
        self.status = status if 400 <= status <= 599 else 502
        super().__init__(f"{self.code} (HTTP {self.status})")


def _port_error(response: httpx.Response, fallback: str) -> PublicModelPortError:
    try:
        body = response.json()
    except ValueError:
        body = None
    code = body.get("code") if isinstance(body, dict) else None
    if not isinstance(code, str):
        nested = body.get("error") if isinstance(body, dict) else None
        code = nested.get("code") if isinstance(nested, dict) else None
    return PublicModelPortError(code if isinstance(code, str) else fallback, response.status_code)


@dataclass(frozen=True)
class PilotDeckHarnessModel:
    id: str
    provider: str
    model: str
    host: FixedPilotDeckDomainHostClient
    principal: dict[str, str]
    # Internal turn deadline hint. It is never part of the PD model selection
    # or canonical request; Knowledge routing may derive a shorter local wait.
    timeout_seconds: float | None = None


def select_harness_model(context: Any, model_id: str | None = None) -> PilotDeckHarnessModel:
    host = require_pilotdeck_domain_host()
    if not isinstance(host, FixedPilotDeckDomainHostClient) or not host.permits(
        context.tenant_id, context.user_id, context.staff_id
    ):
        raise RuntimeError("PUBLIC_HOST_FIXED_IDENTITY_MISMATCH")
    if not all((host.origin, host.bridge_token, host.pilotdeck_user_id)):
        raise RuntimeError("PUBLIC_HOST_IDENTITY_UNBOUND")
    principal = {"pilotDeckUserId": host.pilotdeck_user_id,
                 "tenantId": host.tenant_id, "actorUserId": host.actor_user_id,
                 "agentId": context.staff_id}
    with httpx.Client(base_url=host.origin, transport=host.transport,
                      timeout=60, follow_redirects=False) as client:
        try:
            description = client.post("/api/module-host/describe",
                headers={"Authorization": f"Bearer {host.bridge_token}"},
                json={"principal": principal})
        except httpx.RequestError as exc:
            raise PublicModelPortError("PUBLIC_HOST_DESCRIBE_UNAVAILABLE", 503) from exc
        if not description.is_success:
            raise _port_error(description, "PUBLIC_HOST_DESCRIBE_FAILED")
        try:
            operations = description.json().get("operations")
        except (ValueError, AttributeError) as exc:
            raise RuntimeError("PUBLIC_HOST_DESCRIBE_INVALID") from exc
        if not isinstance(operations, list) or not all(
            operation in operations for operation in
            ("list_model_catalog", "model_prepare", "model_stream")
        ):
            raise RuntimeError("PUBLIC_HOST_REQUIRED_MODEL_PORT_UNAVAILABLE")
        catalog = host._call(client, principal=principal,
                             operation="list_model_catalog", input={}).json()
    items = catalog.get("data") if isinstance(catalog, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("PUBLIC_HOST_MODEL_CATALOG_INVALID")
    default = catalog.get("defaultSelection")
    if not isinstance(default, dict) or default.get("mode") != "model":
        raise RuntimeError("PUBLIC_HOST_MODEL_SELECTION_AMBIGUOUS")
    selected = [item for item in items if isinstance(item, dict)
                and item.get("provider") == default.get("provider")
                and item.get("model") == default.get("model")]
    if len(selected) != 1:
        raise RuntimeError("PUBLIC_HOST_MODEL_SELECTION_AMBIGUOUS")
    item = selected[0]
    provider, model = item.get("provider"), item.get("model")
    if not isinstance(provider, str) or not provider or not isinstance(model, str) or not model \
            or item.get("id") != f"{provider}/{model}" \
            or item.get("available") is not True \
            or item.get("is_default") is not True:
        raise RuntimeError("PUBLIC_HOST_MODEL_SELECTION_INVALID")
    if model_id and model_id != item["id"]:
        raise RuntimeError("PUBLIC_HOST_EXPLICIT_MODEL_NOT_SELECTED")
    return PilotDeckHarnessModel(str(item["id"]), provider, model, host, principal)


def prepare_model_request(selection: PilotDeckHarnessModel, request: dict[str, Any],
                          *, cancelled=None) -> dict[str, Any]:
    """Validate the exact model/request with the active public Port before streaming."""
    request_id = f"harness-{uuid4().hex}"
    payload = {"requestId": request_id, "modelId": selection.id, "request": request}
    with httpx.Client(base_url=selection.host.origin, transport=selection.host.transport,
                      timeout=selection.timeout_seconds, follow_redirects=False) as client:
        if cancelled is not None and cancelled():
            raise PublicModelPortError("PUBLIC_HOST_MODEL_CANCELLED", 499)
        try:
            prepared = client.post("/api/module-host/call",
                headers={"Authorization": f"Bearer {selection.host.bridge_token}"},
                json={"principal": selection.principal, "operation": "model_prepare",
                      "input": payload})
        except httpx.RequestError as exc:
            raise PublicModelPortError("PUBLIC_HOST_MODEL_PREPARE_UNAVAILABLE", 503) from exc
        if not prepared.is_success:
            raise _port_error(prepared, "PUBLIC_HOST_MODEL_PREPARE_FAILED")
        try:
            body = prepared.json()
        except ValueError as exc:
            raise RuntimeError("PUBLIC_HOST_MODEL_PREPARE_INVALID") from exc
        expected = {"requestedModelId": selection.id,
                    "selectedModelId": selection.model, "providerId": selection.provider}
        if not isinstance(body, dict) or body.get("requestId") != request_id \
                or body.get("selection") != expected or body.get("request") != request:
            raise RuntimeError("PUBLIC_HOST_MODEL_PREPARE_INVALID")
        if cancelled is not None and cancelled():
            raise PublicModelPortError("PUBLIC_HOST_MODEL_CANCELLED", 499)
    return payload


def stream_model_events(selection: PilotDeckHarnessModel, request: dict[str, Any],
                        *, cancelled=None, prepared_input: dict[str, Any] | None = None):
    """Yield validated canonical events from the same prepared model request."""
    payload = prepared_input or prepare_model_request(selection, request, cancelled=cancelled)
    try:
        with httpx.Client(base_url=selection.host.origin, transport=selection.host.transport,
                          timeout=selection.timeout_seconds, follow_redirects=False) as client:
            with client.stream("POST", "/api/module-host/call",
                               headers={"Authorization": f"Bearer {selection.host.bridge_token}"},
                               json={"principal": selection.principal, "operation": "model_stream",
                                     "input": payload}) as response:
                if not response.is_success:
                    response.read()
                    raise _port_error(response, "PUBLIC_HOST_MODEL_STREAM_FAILED")
                if not response.headers.get("content-type", "").startswith("application/x-ndjson"):
                    raise RuntimeError("PUBLIC_HOST_MODEL_STREAM_INVALID")
                if response.headers.get("x-pilotdeck-model-id") != selection.model \
                        or response.headers.get("x-pilotdeck-provider-id") != selection.provider:
                    raise RuntimeError("PUBLIC_HOST_MODEL_STREAM_SELECTION_MISMATCH")
                for line in response.iter_lines():
                    if cancelled is not None and cancelled():
                        raise PublicModelPortError("PUBLIC_HOST_MODEL_CANCELLED", 499)
                    if line.strip():
                        try:
                            event = json.loads(line)
                        except ValueError as exc:
                            raise RuntimeError("PUBLIC_HOST_MODEL_STREAM_INVALID") from exc
                        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                            raise RuntimeError("PUBLIC_HOST_MODEL_STREAM_INVALID")
                        if event["type"] == "error":
                            error = event.get("error")
                            code = error.get("code") if isinstance(error, dict) else None
                            raise PublicModelPortError(code if isinstance(code, str)
                                                       else "PUBLIC_HOST_MODEL_FAILED",
                                                       error.get("status") if isinstance(error, dict)
                                                       and isinstance(error.get("status"), int) else 502)
                        yield event
    except httpx.RequestError as exc:
        raise PublicModelPortError("PUBLIC_HOST_MODEL_STREAM_UNAVAILABLE", 503) from exc


def generate_harness_json(selection: PilotDeckHarnessModel, system_prompt: str,
                          user_payload: dict[str, Any]) -> Any:
    """Use the original JSON repair policy with the same authenticated PD Port."""
    from app.llm.client import (LLMClient, _prepare_user_input, _request_messages,
                                _with_json_mode_instruction)
    from staffdeck_harness.bridge.pilotdeck_model_wire import canonical_request

    class PublicJsonClient(LLMClient):
        def __init__(self):
            pass

        def generate_text(self, prompt, payload, cancellation=None, **kwargs):
            return self._generate_json_candidate(prompt, payload, False, cancellation)

        def _generate_json_candidate(self, prompt, payload, json_mode_supported, cancellation=None):
            if cancellation is not None and getattr(cancellation, "cancelled", False):
                raise RuntimeError("PUBLIC_HOST_MODEL_CANCELLED")
            context, serialized = _prepare_user_input(payload)
            messages = _with_json_mode_instruction(_request_messages(prompt, context, serialized))
            if not messages or messages[0].get("role") != "system":
                raise RuntimeError("PUBLIC_HOST_MODEL_PROMPT_INVALID")
            canonical = canonical_request({"messages": messages, "stream": True}, selection)
            fragments = []
            ended = False
            for event in stream_model_events(selection, canonical):
                if event["type"] == "text_delta" and isinstance(event.get("text"), str):
                    fragments.append(event["text"])
                elif event["type"] == "message_end":
                    if event.get("finishReason") != "stop":
                        raise RuntimeError("PUBLIC_HOST_MODEL_INCOMPLETE")
                    ended = True
                elif event["type"] == "error":
                    raise RuntimeError("PUBLIC_HOST_MODEL_FAILED")
            if not ended or not fragments:
                raise RuntimeError("PUBLIC_HOST_MODEL_INCOMPLETE")
            return "".join(fragments)

    return PublicJsonClient().generate_json(system_prompt, user_payload)
