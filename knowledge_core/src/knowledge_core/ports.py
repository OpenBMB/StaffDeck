from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .parser import extract_text


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    model: str
    api_key: str
    timeout_seconds: float = 120


class KnowledgeModelPort(Protocol):
    def generate_json(self, config: ModelConfig, prompt: str, payload: dict[str, Any]) -> Any: ...


class KnowledgeParserPort(Protocol):
    def parse(self, filename: str, content: bytes) -> tuple[str, str]: ...


class LocalParser:
    def parse(self, filename: str, content: bytes) -> tuple[str, str]:
        return extract_text(filename, content)


class OpenAICompatibleModel:
    def generate_json(self, config: ModelConfig, prompt: str, payload: dict[str, Any]) -> Any:
        with httpx.Client(timeout=config.timeout_seconds) as client:
            response = client.post(
                config.base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {config.api_key}"},
                json={
                    "model": config.model,
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                    "response_format": {"type": "json_object"},
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0]
            return json.loads(content)
