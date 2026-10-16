"""Explicit HTTP adapter for optional hosted model and parsing capabilities."""

import base64
import logging
from uuid import uuid4
from typing import Any

import httpx
from dataclasses import dataclass
import os

from .ports import ModelConfig
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CapabilityBinding:
    tenant_id: str
    space_id: str
    endpoint: str
    credential_env: str


class SpaceCapabilities:
    def __init__(self, bindings: list[CapabilityBinding]):
        self.providers = {}
        for binding in bindings:
            key = (binding.tenant_id, binding.space_id)
            if key in self.providers:
                raise ValueError(f"Overlapping capability binding: {key}")
            token = os.getenv(binding.credential_env)
            if not token:
                raise ValueError(f"Capability credential unavailable: {binding.credential_env}")
            self.providers[key] = HttpCapabilities(binding.endpoint, token)

    def scoped(self, tenant_id, space_id, model_id=None):
        provider = self.providers.get((tenant_id, space_id))
        return provider.scoped(tenant_id, space_id, model_id) if provider else None


class HttpCapabilities:
    def __init__(self, url: str, bearer: str):
        self.url, self.bearer = url.rstrip("/"), bearer

    def scoped(self, tenant_id: str, space_id: str, model_id: str | None = None):
        return ScopedCapabilities(self, tenant_id, space_id, model_id)


class ScopedCapabilities:
    def __init__(self, provider, tenant_id, space_id, model_id):
        self.provider = provider
        self.context = {"tenant_id": tenant_id, "space_id": space_id, "model_id": model_id}
        self.model_config = ModelConfig("hosted", model_id or "default", "")

    def call(self, operation: str, value: dict[str, Any]):
        call_id = uuid4().hex
        response = httpx.post(self.provider.url + "/" + operation,
            headers={"Authorization": "Bearer " + self.provider.bearer, "X-KM-Call-ID": call_id},
            json={**self.context, **value}, timeout=120)
        response.raise_for_status()
        logger.warning("KM_CAPABILITY_RESULT call_id=%s operation=%s model_id=%s status=%s",
                       call_id, operation, self.context["model_id"], response.status_code)
        return response.json()

    def generate_json(self, config, prompt, payload):
        return self.call("model", {"prompt": prompt, "payload": payload})

    def parse(self, filename, content):
        return self.parse_document(filename, content, {})

    def parse_document(self, filename, content, context):
        value = self.call("parse", {"filename": filename,
            "content_base64": base64.b64encode(content).decode(), "context": context})
        return value["text"], value["file_type"]
