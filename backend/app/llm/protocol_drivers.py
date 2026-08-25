from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import base64
import binascii
import json
import re
from types import SimpleNamespace
from threading import Event
from typing import Any, Protocol
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx


_DATA_URL = re.compile(r"^data:(image/(?:jpeg|png|gif|webp));base64,(.+)$", re.DOTALL)
_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_MAX_IMAGE_COUNT = 6
_MAX_TOTAL_IMAGE_BYTES = 18 * 1024 * 1024
_MAX_REQUEST_BYTES = 25 * 1024 * 1024


class ProtocolCallError(Exception):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        provider_code: str | None = None,
        provider_message: str | None = None,
        upstream_body: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
        self.provider_code = provider_code
        self.provider_message = provider_message
        self.upstream_body = upstream_body
        self.request_id = request_id


class CancellationToken:
    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


class ProtocolDriver(Protocol):
    request_kind: str

    def observable_request(
        self,
        request: dict[str, Any],
        *,
        stream: bool,
    ) -> dict[str, Any]: ...

    def complete(self, request: dict[str, Any]) -> Any: ...

    def stream(self, request: dict[str, Any]) -> Iterator[Any]: ...


@dataclass(frozen=True)
class ChatCompletionsDriver:
    client: Any
    request_kind: str = "chat.completions"

    def observable_request(
        self,
        request: dict[str, Any],
        *,
        stream: bool,
    ) -> dict[str, Any]:
        payload = _wire_request(request)
        if stream:
            payload["stream"] = True
        return payload

    def complete(self, request: dict[str, Any]) -> Any:
        _raise_if_cancelled(request)
        return self.client.chat.completions.create(**_wire_request(request))

    def stream(self, request: dict[str, Any]) -> Iterator[Any]:
        _raise_if_cancelled(request)
        stream = self.client.chat.completions.create(**_wire_request(request), stream=True)

        def iterate() -> Iterator[Any]:
            try:
                for chunk in stream:
                    _raise_if_cancelled(request)
                    yield chunk
            finally:
                close = getattr(stream, "close", None)
                if callable(close):
                    close()

        return iterate()


@dataclass(frozen=True)
class OpenAIResponsesDriver:
    client: Any
    request_kind: str = "responses"

    def observable_request(
        self,
        request: dict[str, Any],
        *,
        stream: bool,
    ) -> dict[str, Any]:
        payload = _responses_request(request)
        if stream:
            payload["stream"] = True
        return payload

    def complete(self, request: dict[str, Any]) -> Any:
        _raise_if_cancelled(request)
        try:
            response = self.client.responses.create(**_responses_request(request))
        except ProtocolCallError:
            raise
        except Exception as exc:
            raise _protocol_call_error(exc) from exc
        return _responses_completion(response)

    def stream(self, request: dict[str, Any]) -> Iterator[Any]:
        _raise_if_cancelled(request)
        try:
            events = self.client.responses.create(
                **_responses_request(request),
                stream=True,
            )
        except Exception as exc:
            raise _protocol_call_error(exc) from exc
        response_id = None
        try:
            for event in events:
                _raise_if_cancelled(request)
                event_type = _object_value(event, "type")
                if event_type == "response.created":
                    response_id = _object_value(_object_value(event, "response"), "id")
                    yield _stream_chunk(response_id, provider_event=event)
                    continue
                if event_type == "response.output_text.delta":
                    yield _stream_chunk(
                        response_id,
                        text=str(_object_value(event, "delta") or ""),
                        provider_event=event,
                    )
                    continue
                if event_type in {"response.completed", "response.incomplete"}:
                    response = _object_value(event, "response")
                    response_id = _object_value(response, "id") or response_id
                    yield _stream_chunk(
                        response_id,
                        finish_reason=_responses_finish_reason(response),
                        usage=_responses_usage(_object_value(response, "usage")),
                        provider_event=event,
                    )
                    continue
                if event_type in {"response.failed", "error"}:
                    error = _object_value(event, "error") or _object_value(
                        _object_value(event, "response"), "error"
                    )
                    provider_code, provider_message = _provider_error_fields(error)
                    raise ProtocolCallError(
                        "MODEL_UPSTREAM_ERROR",
                        retryable=True,
                        provider_code=provider_code,
                        provider_message=provider_message,
                        upstream_body=_safe_upstream_body(error),
                    )
        except ProtocolCallError:
            raise
        except Exception as exc:
            raise _protocol_call_error(exc) from exc
        finally:
            close = getattr(events, "close", None)
            if callable(close):
                close()


@dataclass(frozen=True)
class AnthropicMessagesDriver:
    client: Any
    request_kind: str = "anthropic.messages"

    def observable_request(
        self,
        request: dict[str, Any],
        *,
        stream: bool,
    ) -> dict[str, Any]:
        payload = _anthropic_request(request)
        if stream:
            payload["stream"] = True
        return payload

    def complete(self, request: dict[str, Any]) -> Any:
        _raise_if_cancelled(request)
        payload = _anthropic_request(request)
        try:
            response = self.client.messages.create(**payload, stream=False)
        except ProtocolCallError:
            raise
        except Exception as exc:
            raise _protocol_call_error(exc) from exc
        text = "".join(
            str(getattr(block, "text", ""))
            for block in (getattr(response, "content", None) or [])
            if getattr(block, "type", None) == "text"
        )
        usage = getattr(response, "usage", None)
        return SimpleNamespace(
            id=getattr(response, "id", None),
            provider_response=response,
            usage=_anthropic_usage(usage),
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=text),
                    finish_reason=getattr(response, "stop_reason", None),
                )
            ],
        )

    def stream(self, request: dict[str, Any]) -> Iterator[Any]:
        payload = _anthropic_request(request)
        try:
            events = self.client.messages.create(**payload, stream=True)
        except Exception as exc:
            raise _protocol_call_error(exc) from exc
        try:
            response_id = None
            for event in events:
                _raise_if_cancelled(request)
                event_type = getattr(event, "type", None)
                if event_type == "message_start":
                    message = getattr(event, "message", None)
                    response_id = getattr(message, "id", None)
                    yield _stream_chunk(
                        response_id,
                        usage=_anthropic_usage(getattr(message, "usage", None)),
                        provider_event=event,
                    )
                    continue
                if event_type == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    if getattr(delta, "type", None) == "text_delta":
                        yield _stream_chunk(
                            response_id,
                            text=str(getattr(delta, "text", "")),
                            provider_event=event,
                        )
                    continue
                if event_type == "message_delta":
                    delta = getattr(event, "delta", None)
                    yield _stream_chunk(
                        response_id,
                        finish_reason=getattr(delta, "stop_reason", None),
                        usage=_anthropic_usage(getattr(event, "usage", None)),
                        provider_event=event,
                    )
        except ProtocolCallError:
            raise
        except Exception as exc:
            raise _protocol_call_error(exc) from exc
        finally:
            close = getattr(events, "close", None)
            if callable(close):
                close()


@dataclass(frozen=True)
class GeminiGenerateContentDriver:
    client: httpx.Client
    base_url: str
    api_key: str
    model: str
    request_kind: str = "gemini.generate_content"

    def observable_request(
        self,
        request: dict[str, Any],
        *,
        stream: bool,
    ) -> dict[str, Any]:
        return _gemini_request(request)

    def complete(self, request: dict[str, Any]) -> Any:
        _raise_if_cancelled(request)
        payload = _gemini_request(request)
        try:
            response = self.client.post(
                _gemini_endpoint(self.base_url, self.model, "generateContent"),
                headers=_gemini_headers(self.api_key),
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise _protocol_call_error(exc) from exc
        _raise_for_gemini_response(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise ProtocolCallError("MODEL_INVALID_PROVIDER_RESPONSE") from exc
        return _gemini_completion(data)

    def stream(self, request: dict[str, Any]) -> Iterator[Any]:
        _raise_if_cancelled(request)
        payload = _gemini_request(request)
        try:
            with self.client.stream(
                "POST",
                _gemini_endpoint(self.base_url, self.model, "streamGenerateContent", stream=True),
                headers=_gemini_headers(self.api_key),
                json=payload,
            ) as response:
                _raise_for_gemini_response(response)
                for line in response.iter_lines():
                    _raise_if_cancelled(request)
                    if not line:
                        continue
                    raw = line[5:].strip() if line.startswith("data:") else line.strip()
                    if raw == "[DONE]":
                        continue
                    try:
                        data = json.loads(raw)
                    except ValueError as exc:
                        raise ProtocolCallError("MODEL_INVALID_PROVIDER_RESPONSE") from exc
                    yield _gemini_completion(data)
        except ProtocolCallError:
            raise
        except httpx.HTTPError as exc:
            raise _protocol_call_error(exc) from exc


def _responses_request(request: dict[str, Any]) -> dict[str, Any]:
    input_items: list[dict[str, Any]] = []
    image_count = 0
    total_image_bytes = 0
    for message in request.get("messages") or []:
        role = str(message.get("role") or "")
        if role not in {"system", "developer", "user", "assistant"}:
            continue
        content, content_image_count, content_image_bytes = _responses_content(
            message.get("content"), role
        )
        if not content:
            continue
        image_count += content_image_count
        total_image_bytes += content_image_bytes
        input_items.append({"role": role, "content": content})
    if image_count > _MAX_IMAGE_COUNT:
        raise ValueError("MODEL_TOO_MANY_IMAGES")
    if total_image_bytes > _MAX_TOTAL_IMAGE_BYTES:
        raise ValueError("MODEL_REQUEST_TOO_LARGE")
    payload: dict[str, Any] = {
        "model": request["model"],
        "input": input_items,
        "temperature": request["temperature"],
        "max_output_tokens": request["max_tokens"],
        "store": False,
    }
    response_format = request.get("response_format")
    if response_format and response_format.get("type") == "json_object":
        payload["text"] = {"format": {"type": "json_object"}}
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > _MAX_REQUEST_BYTES:
        raise ValueError("MODEL_REQUEST_TOO_LARGE")
    return payload


def _responses_content(value: Any, role: str) -> tuple[Any, int, int]:
    if isinstance(value, str):
        return value, 0, 0
    if not isinstance(value, list):
        return "", 0, 0
    parts: list[dict[str, Any]] = []
    image_count = 0
    total_image_bytes = 0
    for item in value:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            text = str(item.get("text") or "")
            if text:
                parts.append({"type": "input_text", "text": text})
            continue
        if item.get("type") != "image_url" or role != "user":
            continue
        image = item.get("image_url")
        url = str(image.get("url") or "") if isinstance(image, dict) else ""
        if not url:
            continue
        match = _DATA_URL.fullmatch(url)
        if match:
            try:
                decoded = base64.b64decode(match.group(2), validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError("MODEL_IMAGE_DATA_URL_INVALID") from exc
            if len(decoded) > _MAX_IMAGE_BYTES:
                raise ValueError("MODEL_IMAGE_TOO_LARGE")
            image_count += 1
            total_image_bytes += len(decoded)
        parts.append({"type": "input_image", "image_url": url})
    return parts, image_count, total_image_bytes


def _responses_completion(response: Any) -> Any:
    text_parts: list[str] = []
    for item in _object_value(response, "output") or []:
        if _object_value(item, "type") != "message":
            continue
        for part in _object_value(item, "content") or []:
            if _object_value(part, "type") == "output_text":
                text_parts.append(str(_object_value(part, "text") or ""))
    text = "".join(text_parts)
    return SimpleNamespace(
        id=_object_value(response, "id"),
        provider_response=response,
        usage=_responses_usage(_object_value(response, "usage")),
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text),
                finish_reason=_responses_finish_reason(response),
            )
        ],
    )


def _responses_finish_reason(response: Any) -> str | None:
    status = _object_value(response, "status")
    if status == "completed":
        return "stop"
    if status == "incomplete":
        details = _object_value(response, "incomplete_details")
        reason = _object_value(details, "reason")
        return "length" if reason == "max_output_tokens" else str(reason or "incomplete")
    return str(status) if status else None


def _responses_usage(value: Any) -> Any:
    if value is None:
        return None
    return SimpleNamespace(
        prompt_tokens=_object_value(value, "input_tokens"),
        completion_tokens=_object_value(value, "output_tokens"),
        total_tokens=_object_value(value, "total_tokens"),
        input_tokens_details=_object_value(value, "input_tokens_details"),
    )


def _object_value(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _gemini_headers(api_key: str) -> dict[str, str]:
    return {
        "content-type": "application/json",
        "authorization": f"Bearer {api_key}",
        "x-goog-api-key": api_key,
        "accept": "text/event-stream, application/json",
    }


def _gemini_endpoint(
    base_url: str, model: str, method: str, *, stream: bool = False
) -> str:
    parsed = urlsplit(base_url.rstrip("/"))
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1beta"):
        path = f"{path}/v1beta"
    path = f"{path}/models/{quote(model, safe='')}:{method}"
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if stream and ("alt", "sse") not in query:
        query.append(("alt", "sse"))
    return urlunsplit((parsed.scheme, parsed.netloc, path, urlencode(query), ""))


def _gemini_request(request: dict[str, Any]) -> dict[str, Any]:
    contents: list[dict[str, Any]] = []
    system_parts: list[dict[str, Any]] = []
    for message in request.get("messages") or []:
        role = str(message.get("role") or "")
        parts = _gemini_content_parts(message.get("content"), role)
        if not parts:
            continue
        if role == "system":
            system_parts.extend(parts)
            continue
        gemini_role = "model" if role == "assistant" else "user"
        if contents and contents[-1]["role"] == gemini_role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": gemini_role, "parts": parts})
    if contents and contents[0]["role"] == "model":
        contents.pop(0)
    payload: dict[str, Any] = {
        "contents": contents,
        "generationConfig": {
            "temperature": request["temperature"],
            "maxOutputTokens": request["max_tokens"],
        },
    }
    if system_parts:
        payload["systemInstruction"] = {"parts": system_parts}
    response_format = request.get("response_format")
    if response_format and response_format.get("type") == "json_object":
        payload["generationConfig"]["responseMimeType"] = "application/json"
    image_parts = [
        part
        for item in contents
        for part in item["parts"]
        if isinstance(part, dict) and "inlineData" in part
    ]
    if len(image_parts) > _MAX_IMAGE_COUNT:
        raise ValueError("MODEL_TOO_MANY_IMAGES")
    total_image_bytes = sum(
        len(base64.b64decode(part["inlineData"]["data"], validate=True))
        for part in image_parts
    )
    if total_image_bytes > _MAX_TOTAL_IMAGE_BYTES:
        raise ValueError("MODEL_REQUEST_TOO_LARGE")
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > _MAX_REQUEST_BYTES:
        raise ValueError("MODEL_REQUEST_TOO_LARGE")
    return payload


def _gemini_content_parts(value: Any, role: str) -> list[dict[str, Any]]:
    if isinstance(value, str):
        return [{"text": value}] if value else []
    if not isinstance(value, list):
        return []
    parts: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            text = str(item.get("text") or "")
            if text:
                parts.append({"text": text})
            continue
        if item.get("type") != "image_url" or role != "user":
            continue
        image = item.get("image_url")
        url = str(image.get("url") or "") if isinstance(image, dict) else ""
        match = _DATA_URL.fullmatch(url)
        if not match:
            raise ValueError("MODEL_IMAGE_DATA_URL_INVALID")
        try:
            decoded = base64.b64decode(match.group(2), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("MODEL_IMAGE_DATA_URL_INVALID") from exc
        if len(decoded) > _MAX_IMAGE_BYTES:
            raise ValueError("MODEL_IMAGE_TOO_LARGE")
        parts.append(
            {
                "inlineData": {
                    "mimeType": match.group(1),
                    "data": match.group(2),
                }
            }
        )
    if sum(1 for part in parts if "inlineData" in part) > _MAX_IMAGE_COUNT:
        raise ValueError("MODEL_TOO_MANY_IMAGES")
    return parts


def _raise_for_gemini_response(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    status = response.status_code
    code, retryable = _model_error_classification(status=status)
    try:
        body: Any = response.json()
    except (ValueError, json.JSONDecodeError):
        body = response.text
    provider_code, provider_message = _provider_error_fields(body)
    raise ProtocolCallError(
        code,
        retryable=retryable,
        status_code=status,
        provider_code=provider_code,
        provider_message=provider_message,
        upstream_body=_safe_upstream_body(body),
        request_id=response.headers.get("x-request-id") or response.headers.get("request-id"),
    )


def _gemini_completion(data: dict[str, Any]) -> Any:
    candidates = data.get("candidates") or []
    candidate = candidates[0] if candidates else {}
    content = candidate.get("content") or {}
    text = "".join(
        str(part.get("text") or "")
        for part in content.get("parts") or []
        if isinstance(part, dict) and not part.get("thought")
    )
    usage = data.get("usageMetadata") or {}
    prompt_tokens = usage.get("promptTokenCount")
    output_tokens = usage.get("candidatesTokenCount")
    return SimpleNamespace(
        id=data.get("responseId"),
        provider_response=data,
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=output_tokens,
            total_tokens=usage.get("totalTokenCount"),
        ),
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text),
                finish_reason=candidate.get("finishReason"),
                delta=SimpleNamespace(content=text),
            )
        ]
        if text or candidate.get("finishReason")
        else [],
    )


def _anthropic_request(request: dict[str, Any]) -> dict[str, Any]:
    messages = list(request.get("messages") or [])
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            system_parts.append(_content_text(message.get("content")))
            continue
        if role not in {"user", "assistant"}:
            continue
        blocks = _anthropic_content(message.get("content"), role)
        if not blocks:
            continue
        if converted and converted[-1]["role"] == role:
            converted[-1]["content"].extend(blocks)
        else:
            converted.append({"role": role, "content": blocks})
    if converted and converted[0]["role"] == "assistant":
        converted.pop(0)
    payload = {
        "model": request["model"],
        "messages": converted,
        "max_tokens": request["max_tokens"],
    }
    # LLM Center's Claude 5 deployments reject the legacy sampling field.
    # Keep temperature for older Anthropic-compatible deployments.
    temperature = request.get("temperature")
    model = str(request.get("model") or "")
    if temperature is not None and not re.match(r"^claude-(?:sonnet|opus)-5(?:$|[-:])", model):
        payload["temperature"] = temperature
    system = "\n\n".join(part for part in system_parts if part)
    if system:
        payload["system"] = system
    image_count = 0
    total_image_bytes = 0
    for message in converted:
        for block in message["content"]:
            if block.get("type") != "image":
                continue
            image_count += 1
            decoded_size = len(base64.b64decode(block["source"]["data"], validate=True))
            if decoded_size > _MAX_IMAGE_BYTES:
                raise ValueError("MODEL_IMAGE_TOO_LARGE")
            total_image_bytes += decoded_size
    if image_count > _MAX_IMAGE_COUNT:
        raise ValueError("MODEL_TOO_MANY_IMAGES")
    if total_image_bytes > _MAX_TOTAL_IMAGE_BYTES:
        raise ValueError("MODEL_REQUEST_TOO_LARGE")
    if len(str(payload).encode("utf-8")) > _MAX_REQUEST_BYTES:
        raise ValueError("MODEL_REQUEST_TOO_LARGE")
    return payload


def _protocol_call_error(exc: Exception) -> ProtocolCallError:
    name = type(exc).__name__.lower()
    status = _status_code(exc)
    body = getattr(exc, "body", None)
    if body is None:
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                body = response.json()
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                body = getattr(response, "text", None)
    provider_code, provider_message = _provider_error_fields(body)
    if not provider_message:
        provider_message = _safe_fragment(exc, 400)
    details = {
        "status_code": status,
        "provider_code": provider_code,
        "provider_message": provider_message,
        "upstream_body": _safe_upstream_body(body),
        "request_id": _safe_fragment(getattr(exc, "request_id", None), 128),
    }
    code, retryable = _model_error_classification(status=status, exception_name=name)
    return ProtocolCallError(code, retryable=retryable, **details)


def _model_error_classification(
    *,
    status: int | None,
    exception_name: str = "",
) -> tuple[str, bool]:
    if status == 401 or "authentication" in exception_name:
        return "MODEL_AUTHENTICATION_FAILED", False
    if status == 403 or "permission" in exception_name:
        return "MODEL_PERMISSION_DENIED", False
    if status == 404 or "notfound" in exception_name:
        return "MODEL_ENDPOINT_NOT_FOUND", False
    if status == 429 or "ratelimit" in exception_name:
        return "MODEL_RATE_LIMITED", True
    if status in {408, 504} or "timeout" in exception_name or "connecterror" in exception_name:
        return "MODEL_TIMEOUT", True
    if status in {400, 422}:
        return "MODEL_INVALID_REQUEST", False
    if status == 409:
        return "MODEL_UPSTREAM_CONFLICT", False
    if status == 413:
        return "MODEL_REQUEST_TOO_LARGE", False
    if status in {500, 502, 503}:
        return "MODEL_UPSTREAM_UNAVAILABLE", True
    return "MODEL_UPSTREAM_ERROR", status is None or status >= 500


def _status_code(exc: Exception) -> int | None:
    raw = getattr(exc, "status_code", None)
    if raw is None:
        raw = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _provider_error_fields(body: Any) -> tuple[str | None, str | None]:
    candidate = body
    if isinstance(candidate, dict) and isinstance(candidate.get("error"), dict):
        candidate = candidate["error"]
    if not isinstance(candidate, dict):
        return None, None
    code = _safe_fragment(
        candidate.get("code") or candidate.get("type") or candidate.get("status"),
        128,
    )
    message = _safe_fragment(candidate.get("message") or candidate.get("detail"), 400)
    return code, message


def _safe_upstream_body(body: Any) -> str | None:
    if body is None:
        return None
    redacted = _redact_sensitive_values(body)
    if isinstance(redacted, str):
        return _safe_fragment(redacted, 2_000)
    try:
        return _safe_fragment(
            json.dumps(redacted, ensure_ascii=False, separators=(",", ":")),
            2_000,
        )
    except (TypeError, ValueError):
        return _safe_fragment(redacted, 2_000)


def _redact_sensitive_values(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(marker in normalized for marker in ("api_key", "authorization", "token", "secret")):
                redacted[str(key)] = "[redacted]"
            else:
                redacted[str(key)] = _redact_sensitive_values(item)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive_values(item) for item in value[:50]]
    if isinstance(value, str):
        return _redact_sensitive_text(value)
    return value


def _redact_sensitive_text(value: str) -> str:
    redacted = re.sub(
        r"(?i)(api[-_ ]?key|authorization|access[-_ ]?token|secret)"
        r"(\s*[\"']?\s*[:=]\s*[\"']?)([^\s\"',;}]+)",
        r"\1\2[redacted]",
        value,
    )
    return re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/=]+", "Bearer [redacted]", redacted)


def _safe_fragment(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    return text if len(text) <= limit else f"{text[:limit]}…"


def _raise_if_cancelled(request: dict[str, Any]) -> None:
    token = request.get("_cancellation")
    if isinstance(token, CancellationToken) and token.cancelled:
        raise ProtocolCallError("MODEL_CANCELLED")


def _wire_request(request: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in request.items() if not key.startswith("_")}


def _anthropic_content(value: Any, role: str) -> list[dict[str, Any]]:
    if isinstance(value, str):
        return [{"type": "text", "text": value}] if value else []
    if not isinstance(value, list):
        return []
    blocks: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            text = str(item.get("text") or "")
            if text:
                blocks.append({"type": "text", "text": text})
            continue
        if item.get("type") != "image_url" or role != "user":
            continue
        image = item.get("image_url")
        url = str(image.get("url") or "") if isinstance(image, dict) else ""
        match = _DATA_URL.fullmatch(url)
        if not match:
            raise ValueError("MODEL_IMAGE_DATA_URL_INVALID")
        try:
            base64.b64decode(match.group(2), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("MODEL_IMAGE_DATA_URL_INVALID") from exc
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": match.group(1),
                    "data": match.group(2),
                },
            }
        )
    return blocks


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    return "".join(
        str(item.get("text") or "")
        for item in value
        if isinstance(item, dict) and item.get("type") == "text"
    )


def _anthropic_usage(value: Any) -> Any:
    if value is None:
        return None
    input_tokens = getattr(value, "input_tokens", None)
    output_tokens = getattr(value, "output_tokens", None)
    return SimpleNamespace(
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        total_tokens=(input_tokens + output_tokens)
        if isinstance(input_tokens, int) and isinstance(output_tokens, int)
        else None,
    )


def _stream_chunk(
    response_id: Any,
    *,
    text: str = "",
    finish_reason: Any = None,
    usage: Any = None,
    provider_event: Any = None,
) -> Any:
    choices = []
    if text or finish_reason:
        choices.append(
            SimpleNamespace(
                delta=SimpleNamespace(content=text),
                finish_reason=finish_reason,
            )
        )
    return SimpleNamespace(
        id=response_id,
        usage=usage,
        choices=choices,
        provider_event=provider_event,
    )
