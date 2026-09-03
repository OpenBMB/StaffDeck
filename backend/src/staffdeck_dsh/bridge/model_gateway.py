"""OpenAI-compatible model gateway for the DSH subprocess.

DSH speaks ``POST {baseURL}/chat/completions`` (OpenAI wire, DeepSeek
extensions). Instead of handing the Node process a real API key and letting it
talk to the provider itself, the bridge points ``baseURL`` at this endpoint,
served on the same localhost port as the capability MCP server, and uses the
per-turn **activation token** as the API key.

Every request is therefore executed by StaffDeck's own model module
(``app.llm.client.LLMClient`` + its protocol drivers) for the ModelConfig that
the turn resolved: protocol, credentials, thinking policy, temperature, output
cap and observability (``llm_call_started`` / ``llm_call_finished`` on the
turn's trace) all behave exactly as on Harness v2. Provider credentials never
enter the DSH process.

Request handling
----------------
- ``Authorization: Bearer <activation token>`` → the activation (404 if the
  turn is over, 401 if missing).
- Only ``openai_chat_completions`` model configs can serve tool calls today; the
  other protocol drivers have no tool mapping, so they answer 400 with an
  operator-readable message rather than silently dropping tools.
- ``thinking`` / ``reasoning_effort`` sent by DSH are ignored: the ModelConfig
  is the source of truth (``LLMClient.thinking_mode`` / ``extra_body``).
- Streaming responses are passed through chunk by chunk as SSE; the provider's
  own chunk shape (including ``reasoning_content`` and ``tool_calls`` deltas)
  is preserved, ending with ``data: [DONE]``.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator, Callable, Iterator

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from staffdeck_dsh.bridge.capability_mcp import ActivationRegistry

logger = logging.getLogger(__name__)

CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
# DSH sends a 256k default; we never let a single step exceed the model's own cap.
_MAX_TOKENS_FLOOR = 256


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "type": code.lower(), "message": message}}, status_code=status)


def _bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


def _dump(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump(mode="json", exclude_none=True)
        except TypeError:
            return obj.model_dump()
    if isinstance(obj, dict):
        return obj
    return {"raw": str(obj)}


class ModelGateway:
    """Mounted on the bridge's Starlette app; one instance per DshRuntime."""

    def __init__(self, registry: ActivationRegistry, *, client_factory: Callable[[Any], Any] | None = None):
        self.registry = registry
        self._client_factory = client_factory or self._default_client

    @staticmethod
    def _default_client(model_config: Any) -> Any:
        from app.llm.client import LLMClient

        return LLMClient(model_config)

    def mount(self, app: Any) -> None:
        app.add_route(CHAT_COMPLETIONS_PATH, self.chat_completions, methods=["POST"])

    # -- endpoint --------------------------------------------------------------

    async def chat_completions(self, request: Request):
        token = _bearer(request)
        if not token:
            return _error(401, "MISSING_CREDENTIAL", "missing activation token")
        act = self.registry.get(token)
        if act is None:
            return _error(404, "ACTIVATION_FENCED", "no live activation for this token (turn finished or cancelled)")
        model_config = getattr(act.host, "model_config", None)
        if model_config is None:
            return _error(409, "MODEL_NOT_CONFIGURED", "该员工没有可用的默认模型，请先在「模型配置」中设置")
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return _error(400, "MALFORMED_REQUEST", "request body is not JSON")
        if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
            return _error(400, "MALFORMED_REQUEST", "messages must be a list")

        try:
            client = self._client_factory(model_config)
        except Exception as exc:  # noqa: BLE001 — bad credentials / unsupported protocol
            return _error(400, "MODEL_CLIENT_ERROR", f"模型配置无法使用：{exc}")
        protocol = str(getattr(client, "api_protocol", "openai_chat_completions"))
        if protocol != "openai_chat_completions":
            return _error(400, "UNSUPPORTED_PROTOCOL", f"Harness v3 目前只支持 OpenAI Chat Completions 协议的模型，当前模型使用 {protocol}；请为该员工选择兼容的模型或改用 Harness v2 引擎")

        wire = self._wire_request(client, model_config, body)
        stream = bool(body.get("stream", True))
        driver = client.driver
        trace = getattr(act.host, "trace", None)
        started = time.perf_counter()
        span = {
            "operation": "dsh.step", "model": client.model, "model_name": getattr(client, "model_config_name", "") or client.model,
            "endpoint": getattr(client, "base_url", ""), "request_kind": getattr(driver, "request_kind", "chat.completions"),
            "stream": stream, "thinking_mode": getattr(client, "thinking_mode", "") or "provider_default",
            "request_message_count": len(body.get("messages") or []), "tool_count": len(body.get("tools") or []),
            "engine": "dsh",
        }
        if trace:
            trace("llm_call_started", dict(span))

        loop_run = __import__("asyncio").get_running_loop().run_in_executor
        if not stream:
            try:
                completion = await loop_run(None, driver.complete, wire)
            except Exception as exc:  # noqa: BLE001
                if trace:
                    trace("llm_call_failed", {**span, "duration_ms": _ms(started), "error": str(exc)[:500]})
                return _error(502, "PROVIDER_ERROR", str(exc)[:500])
            data = _dump(completion)
            if trace:
                trace("llm_call_finished", {**span, "duration_ms": _ms(started), "status": "success", **_usage(data.get("usage"))})
            return JSONResponse(data)

        # Streaming: pull the synchronous provider iterator on a worker thread and relay chunks as SSE.
        try:
            chunks: Iterator[Any] = await loop_run(None, driver.stream, wire)
        except Exception as exc:  # noqa: BLE001
            if trace:
                trace("llm_call_failed", {**span, "duration_ms": _ms(started), "error": str(exc)[:500]})
            return _error(502, "PROVIDER_ERROR", str(exc)[:500])

        async def body_iter() -> AsyncIterator[bytes]:
            usage: dict[str, Any] = {}
            finish_reason: str | None = None
            loop = __import__("asyncio").get_running_loop()
            it = iter(chunks)
            try:
                while True:
                    chunk = await loop.run_in_executor(None, next, it, None)
                    if chunk is None:
                        break
                    data = _dump(chunk)
                    if isinstance(data.get("usage"), dict):
                        usage = data["usage"]
                    for choice in data.get("choices") or []:
                        if isinstance(choice, dict) and choice.get("finish_reason"):
                            finish_reason = str(choice["finish_reason"])
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode()
                yield b"data: [DONE]\n\n"
                if trace:
                    trace("llm_call_finished", {**span, "duration_ms": _ms(started), "status": "success", "finish_reason": finish_reason, **_usage(usage)})
            except Exception as exc:  # noqa: BLE001
                logger.exception("model gateway stream failed")
                if trace:
                    trace("llm_call_failed", {**span, "duration_ms": _ms(started), "error": str(exc)[:500]})
                # Mid-stream: the only thing DSH can act on is an error payload before [DONE]
                yield f"data: {json.dumps({'error': {'code': 'PROVIDER_ERROR', 'message': str(exc)[:500]}})}\n\n".encode()

        return StreamingResponse(body_iter(), media_type="text/event-stream", headers={"cache-control": "no-cache", "x-accel-buffering": "no"})

    # -- request shaping ----------------------------------------------------------

    @staticmethod
    def _wire_request(client: Any, model_config: Any, body: dict[str, Any]) -> dict[str, Any]:
        from app.llm.client import _thinking_request_kwargs

        cap = int(getattr(model_config, "max_output_tokens", 0) or 0)
        requested = body.get("max_tokens")
        max_tokens = cap if not isinstance(requested, int) or requested <= 0 else (min(requested, cap) if cap else requested)
        # ``stream`` is never part of the wire dict: ChatCompletionsDriver.stream() adds it itself and
        # complete() must not carry it (the OpenAI SDK rejects duplicate keywords).
        streaming = bool(body.get("stream", True))
        wire: dict[str, Any] = {
            "model": client.model,                       # the ModelConfig decides, not the subprocess
            "messages": body["messages"],
            "temperature": getattr(client, "temperature", None),
        }
        if wire["temperature"] is None:
            wire.pop("temperature")
        if max_tokens and max_tokens >= _MAX_TOKENS_FLOOR:
            wire["max_tokens"] = max_tokens
        if isinstance(body.get("tools"), list) and body["tools"]:
            wire["tools"] = body["tools"]
            if body.get("tool_choice") is not None:
                wire["tool_choice"] = body["tool_choice"]
        if isinstance(body.get("stop"), list) and body["stop"]:
            wire["stop"] = body["stop"]
        if streaming and isinstance(body.get("stream_options"), dict):
            wire["stream_options"] = body["stream_options"]
        # Thinking policy = ModelConfig (merged extra_body / protocol options), never DSH's own default.
        wire.update(_thinking_request_kwargs(getattr(client, "thinking_mode", ""), getattr(client, "extra_body", {})))
        return wire


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _usage(usage: Any) -> dict[str, Any]:
    if not isinstance(usage, dict):
        return {}
    out = {}
    for src, dst in (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens"), ("total_tokens", "total_tokens")):
        if isinstance(usage.get(src), int):
            out[dst] = usage[src]
    return out
