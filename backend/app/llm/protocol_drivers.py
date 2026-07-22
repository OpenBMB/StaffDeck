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
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


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

    def complete(self, request: dict[str, Any]) -> Any: ...

    def stream(self, request: dict[str, Any]) -> Iterator[Any]: ...


@dataclass(frozen=True)
class ChatCompletionsDriver:
    client: Any
    request_kind: str = "chat.completions"

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
class AnthropicMessagesDriver:
    client: Any
    request_kind: str = "anthropic.messages"

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
                    )
                    continue
                if event_type == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    if getattr(delta, "type", None) == "text_delta":
                        yield _stream_chunk(
                            response_id,
                            text=str(getattr(delta, "text", "")),
                        )
                    continue
                if event_type == "message_delta":
                    delta = getattr(event, "delta", None)
                    yield _stream_chunk(
                        response_id,
                        finish_reason=getattr(delta, "stop_reason", None),
                        usage=_anthropic_usage(getattr(event, "usage", None)),
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
    if status == 401:
        code = "MODEL_AUTHENTICATION_FAILED"
    elif status == 403:
        code = "MODEL_PERMISSION_DENIED"
    elif status == 404:
        code = "MODEL_ENDPOINT_NOT_FOUND"
    elif status == 429:
        code = "MODEL_RATE_LIMITED"
    else:
        code = "MODEL_UPSTREAM_ERROR"
    raise ProtocolCallError(code, retryable=status == 429 or status >= 500)


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
        "temperature": request["temperature"],
    }
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
    status = getattr(exc, "status_code", None)
    if status == 401 or "authentication" in name:
        return ProtocolCallError("MODEL_AUTHENTICATION_FAILED")
    if status == 403 or "permission" in name:
        return ProtocolCallError("MODEL_PERMISSION_DENIED")
    if status == 404 or "notfound" in name:
        return ProtocolCallError("MODEL_ENDPOINT_NOT_FOUND")
    if status == 429 or "ratelimit" in name:
        return ProtocolCallError("MODEL_RATE_LIMITED", retryable=True)
    if "timeout" in name or "connecterror" in name:
        return ProtocolCallError("MODEL_TIMEOUT", retryable=True)
    return ProtocolCallError("MODEL_UPSTREAM_ERROR", retryable=True)


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
) -> Any:
    choices = []
    if text or finish_reason:
        choices.append(
            SimpleNamespace(
                delta=SimpleNamespace(content=text),
                finish_reason=finish_reason,
            )
        )
    return SimpleNamespace(id=response_id, usage=usage, choices=choices)
