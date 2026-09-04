"""Protocol adapters for the Harness v3 model gateway.

The engine subprocess only speaks the OpenAI chat-completions wire (``messages`` with
``tool_calls`` / ``role: "tool"``, ``tools``, ``tool_choice``, streamed ``delta`` chunks).
StaffDeck's legacy protocol drivers (``app.llm.protocol_drivers``) translate *text* for the
other three protocols but drop tool calls, which the engine relies on for every step. This
module therefore owns a second, tool-aware translation layer that talks to the raw SDK client
held by ``app.llm.client.LLMClient`` (``client.client``) and always hands back plain OpenAI
chat-completion dicts:

- ``complete(wire) -> dict``: an OpenAI ``chat.completion`` response.
- ``stream(wire) -> Iterator[dict]``: OpenAI ``chat.completion.chunk`` dicts.

``wire`` is the request the gateway builds in ``ModelGateway._wire_request``: ``model``,
``messages``, optional ``temperature`` / ``max_tokens`` / ``tools`` / ``tool_choice`` / ``stop``
/ ``stream_options`` / ``extra_body`` (which may carry ``thinking``). ``stream`` is never part
of it. Legacy code under ``app/`` is frozen, so the few helpers we need from there (Gemini
endpoint / header shapes, the Claude-5 temperature rule) are re-implemented locally.

Protocol notes
--------------
- **openai_chat_completions**: a thin pass-through to ``client.driver`` (behaviour unchanged).
- **anthropic_messages**: system → ``system`` string; ``tool_calls`` → ``tool_use`` blocks;
  ``role: "tool"`` → user ``tool_result`` blocks; extended thinking is enabled from
  ``extra_body.thinking``. Thinking-block signatures cannot be round-tripped through the OpenAI
  wire, so thinking + multi-step tool use depends on the provider tolerating their absence.
- **openai_responses**: system/developer messages become ``input`` items (not the
  ``instructions`` parameter) so message ordering is preserved verbatim. ``stop`` has no
  Responses equivalent and is dropped. ``reasoning`` is only sent for ``o*`` / ``gpt-5*``
  models, in which case ``temperature`` is omitted (reasoning models reject it).
- **gemini_generate_content**: raw HTTP against ``{base}/v1beta/models/{model}:generateContent``
  (``:streamGenerateContent?alt=sse`` when streaming). Function-call ids are generated here and
  the ``thoughtSignature`` Gemini attaches to a call is cached per id so it can be echoed back on
  the next turn (Gemini 3 rejects the follow-up without it).

Genuine provider failures raise an ``Exception`` carrying the provider message; the gateway turns
those into ``502 PROVIDER_ERROR``. Unknown message content degrades to text and never raises.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Iterator, Protocol
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

_DATA_URL = re.compile(r"^data:([\w.+-]+/[\w.+-]+);base64,(.+)$", re.DOTALL)
# LLM Center's Claude 5 deployments reject the legacy sampling field (mirrors app.llm).
_ANTHROPIC_NO_TEMPERATURE = re.compile(r"^claude-(?:sonnet|opus)-5(?:$|[-:])")
_ANTHROPIC_DEFAULT_MAX_TOKENS = 4096
_ANTHROPIC_MIN_THINKING_BUDGET = 1024
_ANTHROPIC_MAX_THINKING_BUDGET = 16000
_ANTHROPIC_STOP_REASONS = {
    "end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "tool_use": "tool_calls",
    "refusal": "content_filter",
}
_GEMINI_SCHEMA_DROP = {"additionalProperties", "$schema", "default", "examples"}
_GEMINI_FINISH_REASONS = {
    "STOP": "stop", "MAX_TOKENS": "length", "SAFETY": "content_filter",
    "RECITATION": "content_filter", "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter", "SPII": "content_filter",
}
_GEMINI_SIGNATURE_CACHE_SIZE = 1024


class ProtocolAdapter(Protocol):
    request_kind: str

    def complete(self, wire: dict[str, Any]) -> dict[str, Any]: ...

    def stream(self, wire: dict[str, Any]) -> Iterator[dict[str, Any]]: ...


def adapter_for(client: Any) -> ProtocolAdapter:
    """Pick the adapter for an ``LLMClient`` (or any object exposing the same surface)."""
    protocol = str(getattr(client, "api_protocol", "") or "openai_chat_completions")
    if protocol == "openai_chat_completions":
        return OpenAIChatAdapter(client)
    if protocol == "openai_responses":
        return OpenAIResponsesAdapter(client)
    if protocol == "anthropic_messages":
        return AnthropicAdapter(client)
    if protocol == "gemini_generate_content":
        return GeminiAdapter(client)
    raise ValueError(f"unsupported model protocol: {protocol}")


# -- shared helpers ------------------------------------------------------------------------


def _dump(obj: Any) -> dict[str, Any]:
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump(mode="json", exclude_none=True)
        except TypeError:
            return obj.model_dump()
    if isinstance(obj, dict):
        return obj
    return {"raw": str(obj)}


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _new_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


def _new_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:16]}"


def _close(obj: Any) -> None:
    close = getattr(obj, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            logger.debug("provider stream close failed", exc_info=True)


def _chunk(cid: str, model: str, delta: dict[str, Any], *, finish_reason: str | None = None,
           usage: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if usage:
        out["usage"] = usage
    return out


def _completion(cid: str, model: str, message: dict[str, Any], finish_reason: str,
                usage: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": cid, "object": "chat.completion", "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if usage:
        out["usage"] = usage
    return out


def _usage(prompt: Any, completion: Any, total: Any = None) -> dict[str, Any] | None:
    p, c, t = _int(prompt), _int(completion), _int(total)
    if t is None and p is not None and c is not None:
        t = p + c
    out = {k: v for k, v in (("prompt_tokens", p), ("completion_tokens", c), ("total_tokens", t))
           if v is not None}
    return out or None


def _assistant_message(text: str, tool_calls: list[dict[str, Any]],
                       reasoning: str) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": text or (None if tool_calls else "")}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if reasoning:
        message["reasoning_content"] = reasoning
    return message


def _content_parts(value: Any) -> list[dict[str, str]]:
    """Normalise OpenAI message content to ``[{type: text|image_url, ...}]``; never raises."""
    if value is None:
        return []
    if isinstance(value, str):
        return [{"type": "text", "text": value}] if value else []
    if not isinstance(value, list):
        return [{"type": "text", "text": str(value)}]
    parts: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, str):
            if item:
                parts.append({"type": "text", "text": item})
            continue
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "image_url":
            image = item.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            if isinstance(url, str) and url:
                parts.append({"type": "image_url", "url": url})
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            parts.append({"type": "text", "text": text})
        elif kind not in {"text", None}:
            logger.debug("dropping unsupported content part type=%s", kind)
    return parts


def _content_text(value: Any) -> str:
    return "".join(p["text"] for p in _content_parts(value) if p["type"] == "text")


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if raw is None or raw == "":
        return {}
    try:
        parsed = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except ValueError:
        return {"_raw": str(raw)}
    return parsed if isinstance(parsed, dict) else {"_raw": parsed}


def _args_json(raw: Any) -> str:
    if isinstance(raw, str):
        return raw or "{}"
    if raw is None:
        return "{}"
    try:
        return json.dumps(raw, ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps({"_raw": str(raw)}, ensure_ascii=False)


def _function_tools(tools: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if tool.get("type", "function") == "function" else None
        if fn is None and "name" in tool:
            fn = tool
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        params = fn.get("parameters")
        out.append({
            "name": str(fn["name"]), "description": str(fn.get("description") or ""),
            "parameters": params if isinstance(params, dict) else {},
        })
    return out


def _tool_choice(value: Any) -> tuple[str, str | None] | None:
    """→ ``(mode, name)`` with mode in auto/none/required/function, or None if absent."""
    if value is None:
        return None
    if isinstance(value, str):
        return (value, None) if value in {"auto", "none", "required"} else None
    if isinstance(value, dict):
        kind = value.get("type")
        if kind == "function":
            fn = value.get("function")
            name = fn.get("name") if isinstance(fn, dict) else value.get("name")
            return ("function", str(name)) if name else ("required", None)
        if kind in {"auto", "none", "required"}:
            return (kind, None)
    return None


def _thinking_enabled(wire: dict[str, Any]) -> bool:
    extra = wire.get("extra_body")
    thinking = extra.get("thinking") if isinstance(extra, dict) else None
    return isinstance(thinking, dict) and thinking.get("type") == "enabled"


def _provider_error_message(error: Any) -> str:
    if error is None:
        return "provider error"
    if isinstance(error, str):
        return error
    inner = _get(error, "error")
    if inner is not None and not isinstance(inner, str):
        error = inner
    message = _get(error, "message")
    if message:
        code = _get(error, "code") or _get(error, "type") or _get(error, "status")
        return f"{code}: {message}" if code else str(message)
    return str(error)


# -- openai_chat_completions ----------------------------------------------------------------


class OpenAIChatAdapter:
    """Thin pass-through: the legacy ``ChatCompletionsDriver`` already speaks the wire natively."""

    request_kind = "chat.completions"

    def __init__(self, client: Any):
        self._driver = client.driver
        self.request_kind = getattr(self._driver, "request_kind", self.request_kind)

    def complete(self, wire: dict[str, Any]) -> dict[str, Any]:
        return _dump(self._driver.complete(wire))

    def stream(self, wire: dict[str, Any]) -> Iterator[dict[str, Any]]:
        # The provider call happens here (eagerly), so connection errors surface before any SSE
        # bytes are sent; only the chunk relay is lazy.
        chunks = self._driver.stream(wire)
        return (_dump(chunk) for chunk in chunks)


# -- anthropic_messages ---------------------------------------------------------------------


def anthropic_payload(wire: dict[str, Any]) -> dict[str, Any]:
    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": role, "content": blocks})

    for message in wire.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        content = message.get("content")
        if role in {"system", "developer"}:
            text = _content_text(content)
            if text:
                system_parts.append(text)
            continue
        if role == "tool":
            block: dict[str, Any] = {
                "type": "tool_result", "tool_use_id": str(message.get("tool_call_id") or ""),
            }
            text = _content_text(content)
            if text:
                block["content"] = text
            push("user", [block])
            continue
        if role == "assistant":
            blocks = _anthropic_blocks(content, allow_images=False)
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                blocks.append({
                    "type": "tool_use", "id": str(call.get("id") or _new_call_id()),
                    "name": str(fn.get("name") or ""), "input": _parse_args(fn.get("arguments")),
                })
            push("assistant", blocks)
            continue
        push("user", _anthropic_blocks(content, allow_images=True))

    while messages and messages[0]["role"] == "assistant":
        messages.pop(0)

    model = str(wire.get("model") or "")
    max_tokens = _int(wire.get("max_tokens"))
    if not max_tokens or max_tokens <= 0:
        max_tokens = _ANTHROPIC_DEFAULT_MAX_TOKENS
    thinking = _thinking_enabled(wire)
    payload: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if thinking:
        budget = min(max(_ANTHROPIC_MIN_THINKING_BUDGET, max_tokens // 2),
                     _ANTHROPIC_MAX_THINKING_BUDGET)
        if budget >= max_tokens:  # Anthropic requires budget_tokens < max_tokens
            payload["max_tokens"] = budget + _ANTHROPIC_MIN_THINKING_BUDGET
        payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
    elif wire.get("temperature") is not None and not _ANTHROPIC_NO_TEMPERATURE.match(model):
        payload["temperature"] = wire["temperature"]
    if system_parts:
        payload["system"] = "\n\n".join(system_parts)
    if isinstance(wire.get("stop"), list) and wire["stop"]:
        payload["stop_sequences"] = [str(s) for s in wire["stop"]]

    functions = _function_tools(wire.get("tools"))
    choice = _tool_choice(wire.get("tool_choice"))
    if functions and not (choice and choice[0] == "none"):
        tools = []
        for fn in functions:
            schema = dict(fn["parameters"]) or {"type": "object", "properties": {}}
            schema.setdefault("type", "object")
            tool: dict[str, Any] = {"name": fn["name"], "input_schema": schema}
            if fn["description"]:
                tool["description"] = fn["description"]
            tools.append(tool)
        payload["tools"] = tools
        if choice:
            mode, name = choice
            if thinking and mode in {"required", "function"}:
                mode = "auto"  # extended thinking only allows tool_choice auto
            if mode == "auto":
                payload["tool_choice"] = {"type": "auto"}
            elif mode == "required":
                payload["tool_choice"] = {"type": "any"}
            elif mode == "function":
                payload["tool_choice"] = {"type": "tool", "name": name}
    return payload


def _anthropic_blocks(content: Any, *, allow_images: bool) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for part in _content_parts(content):
        if part["type"] == "text":
            blocks.append({"type": "text", "text": part["text"]})
        elif allow_images:
            match = _DATA_URL.fullmatch(part["url"])
            source = ({"type": "base64", "media_type": match.group(1), "data": match.group(2)}
                      if match else {"type": "url", "url": part["url"]})
            blocks.append({"type": "image", "source": source})
    return blocks


def _anthropic_finish(stop_reason: Any) -> str:
    return _ANTHROPIC_STOP_REASONS.get(str(stop_reason or ""), "stop")


def _anthropic_completion(response: Any, model: str) -> dict[str, Any]:
    text: list[str] = []
    reasoning: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in _get(response, "content") or []:
        kind = _get(block, "type")
        if kind == "text":
            text.append(str(_get(block, "text") or ""))
        elif kind == "tool_use":
            tool_calls.append({
                "id": str(_get(block, "id") or _new_call_id()), "type": "function",
                "function": {"name": str(_get(block, "name") or ""),
                             "arguments": _args_json(_get(block, "input") or {})},
            })
        elif kind == "thinking":
            reasoning.append(str(_get(block, "thinking") or ""))
    finish = _anthropic_finish(_get(response, "stop_reason"))
    if tool_calls and finish == "stop":
        finish = "tool_calls"
    usage = _get(response, "usage")
    return _completion(
        str(_get(response, "id") or _new_id()), model,
        _assistant_message("".join(text), tool_calls, "".join(reasoning)), finish,
        _usage(_get(usage, "input_tokens"), _get(usage, "output_tokens")),
    )


def _anthropic_events(events: Any, model: str) -> Iterator[dict[str, Any]]:
    cid = _new_id()
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    tool_by_block: dict[Any, int] = {}
    args_seen: set[int] = set()
    next_tool = 0
    finished = False
    try:
        for event in events:
            kind = _get(event, "type")
            if kind == "message_start":
                message = _get(event, "message")
                cid = str(_get(message, "id") or cid)
                usage = _get(message, "usage")
                prompt_tokens = _int(_get(usage, "input_tokens"))
                completion_tokens = _int(_get(usage, "output_tokens"))
                yield _chunk(cid, model, {"role": "assistant", "content": ""})
            elif kind == "content_block_start":
                block = _get(event, "content_block")
                if _get(block, "type") == "tool_use":
                    index = next_tool
                    next_tool += 1
                    tool_by_block[_get(event, "index")] = index
                    yield _chunk(cid, model, {"tool_calls": [{
                        "index": index, "id": str(_get(block, "id") or _new_call_id()),
                        "type": "function",
                        "function": {"name": str(_get(block, "name") or ""), "arguments": ""},
                    }]})
            elif kind == "content_block_delta":
                delta = _get(event, "delta")
                delta_type = _get(delta, "type")
                if delta_type == "text_delta":
                    yield _chunk(cid, model, {"content": str(_get(delta, "text") or "")})
                elif delta_type == "input_json_delta":
                    index = tool_by_block.get(_get(event, "index"))
                    partial = str(_get(delta, "partial_json") or "")
                    if index is not None and partial:
                        args_seen.add(index)
                        yield _chunk(cid, model, {"tool_calls": [
                            {"index": index, "function": {"arguments": partial}}]})
                elif delta_type == "thinking_delta":
                    yield _chunk(cid, model, {"reasoning_content": str(_get(delta, "thinking") or "")})
            elif kind == "content_block_stop":
                index = tool_by_block.get(_get(event, "index"))
                if index is not None and index not in args_seen:
                    # Anthropic sends no input_json_delta for empty input; the engine expects JSON.
                    args_seen.add(index)
                    yield _chunk(cid, model, {"tool_calls": [
                        {"index": index, "function": {"arguments": "{}"}}]})
            elif kind == "message_delta":
                delta = _get(event, "delta")
                usage = _get(event, "usage")
                completion_tokens = _int(_get(usage, "output_tokens")) or completion_tokens
                prompt_tokens = _int(_get(usage, "input_tokens")) or prompt_tokens
                finish = _anthropic_finish(_get(delta, "stop_reason"))
                if next_tool and finish == "stop":
                    finish = "tool_calls"
                finished = True
                yield _chunk(cid, model, {}, finish_reason=finish,
                             usage=_usage(prompt_tokens, completion_tokens))
            elif kind == "error":
                raise RuntimeError(_provider_error_message(_get(event, "error") or event))
        if not finished:
            yield _chunk(cid, model, {}, finish_reason="tool_calls" if next_tool else "stop",
                         usage=_usage(prompt_tokens, completion_tokens))
    finally:
        _close(events)


class AnthropicAdapter:
    request_kind = "anthropic.messages"

    def __init__(self, client: Any):
        self._sdk = client.client

    def complete(self, wire: dict[str, Any]) -> dict[str, Any]:
        response = self._sdk.messages.create(**anthropic_payload(wire), stream=False)
        return _anthropic_completion(response, str(wire.get("model") or ""))

    def stream(self, wire: dict[str, Any]) -> Iterator[dict[str, Any]]:
        events = self._sdk.messages.create(**anthropic_payload(wire), stream=True)
        return _anthropic_events(events, str(wire.get("model") or ""))


# -- openai_responses -----------------------------------------------------------------------


def _responses_reasoning_model(model: str) -> bool:
    name = model.lower()
    return name.startswith("o") or "gpt-5" in name


def responses_payload(wire: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for message in wire.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        content = message.get("content")
        if role in {"system", "developer"}:
            text = _content_text(content)
            if text:
                items.append({"role": role, "content": [{"type": "input_text", "text": text}]})
        elif role == "tool":
            items.append({
                "type": "function_call_output", "call_id": str(message.get("tool_call_id") or ""),
                "output": _content_text(content),
            })
        elif role == "assistant":
            text = _content_text(content)
            if text:
                items.append({"role": "assistant",
                              "content": [{"type": "output_text", "text": text}]})
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                items.append({
                    "type": "function_call", "call_id": str(call.get("id") or _new_call_id()),
                    "name": str(fn.get("name") or ""), "arguments": _args_json(fn.get("arguments")),
                })
        else:
            parts: list[dict[str, Any]] = []
            for part in _content_parts(content):
                if part["type"] == "text":
                    parts.append({"type": "input_text", "text": part["text"]})
                else:
                    parts.append({"type": "input_image", "image_url": part["url"]})
            if parts:
                items.append({"role": "user", "content": parts})

    model = str(wire.get("model") or "")
    payload: dict[str, Any] = {"model": model, "input": items, "store": False}
    if _int(wire.get("max_tokens")):
        payload["max_output_tokens"] = wire["max_tokens"]
    if _thinking_enabled(wire) and _responses_reasoning_model(model):
        payload["reasoning"] = {"effort": "medium"}
    elif wire.get("temperature") is not None:
        payload["temperature"] = wire["temperature"]

    functions = _function_tools(wire.get("tools"))
    if functions:
        tools = []
        for fn in functions:
            tool: dict[str, Any] = {
                "type": "function", "name": fn["name"], "parameters": fn["parameters"],
                "strict": False,
            }
            if fn["description"]:
                tool["description"] = fn["description"]
            tools.append(tool)
        payload["tools"] = tools
        choice = _tool_choice(wire.get("tool_choice"))
        if choice:
            mode, name = choice
            payload["tool_choice"] = {"type": "function", "name": name} if mode == "function" else mode
    return payload


def _responses_finish(response: Any, has_tool_calls: bool) -> str:
    if has_tool_calls:
        return "tool_calls"
    if _get(response, "status") == "incomplete":
        reason = _get(_get(response, "incomplete_details"), "reason")
        if reason == "max_output_tokens":
            return "length"
        if reason == "content_filter":
            return "content_filter"
    return "stop"


def _responses_usage(usage: Any) -> dict[str, Any] | None:
    return _usage(_get(usage, "input_tokens"), _get(usage, "output_tokens"),
                  _get(usage, "total_tokens"))


def _responses_completion(response: Any, model: str) -> dict[str, Any]:
    text: list[str] = []
    reasoning: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in _get(response, "output") or []:
        kind = _get(item, "type")
        if kind == "message":
            for part in _get(item, "content") or []:
                part_type = _get(part, "type")
                if part_type == "output_text":
                    text.append(str(_get(part, "text") or ""))
                elif part_type == "refusal":
                    text.append(str(_get(part, "refusal") or ""))
        elif kind == "function_call":
            tool_calls.append({
                "id": str(_get(item, "call_id") or _get(item, "id") or _new_call_id()),
                "type": "function",
                "function": {"name": str(_get(item, "name") or ""),
                             "arguments": _args_json(_get(item, "arguments"))},
            })
        elif kind == "reasoning":
            for summary in _get(item, "summary") or []:
                reasoning.append(str(_get(summary, "text") or ""))
    return _completion(
        str(_get(response, "id") or _new_id()), model,
        _assistant_message("".join(text), tool_calls, "".join(reasoning)),
        _responses_finish(response, bool(tool_calls)), _responses_usage(_get(response, "usage")),
    )


def _responses_events(events: Any, model: str) -> Iterator[dict[str, Any]]:
    cid = _new_id()
    by_item_id: dict[Any, int] = {}
    by_output_index: dict[Any, int] = {}
    args_seen: set[int] = set()
    next_tool = 0
    finished = False
    usage: dict[str, Any] | None = None

    def tool_index(event: Any) -> int | None:
        index = by_item_id.get(_get(event, "item_id"))
        if index is None:
            index = by_output_index.get(_get(event, "output_index"))
        return index

    try:
        for event in events:
            kind = _get(event, "type")
            if kind == "response.created":
                cid = str(_get(_get(event, "response"), "id") or cid)
                yield _chunk(cid, model, {"role": "assistant", "content": ""})
            elif kind == "response.output_item.added":
                item = _get(event, "item")
                if _get(item, "type") == "function_call":
                    index = next_tool
                    next_tool += 1
                    if _get(item, "id") is not None:
                        by_item_id[_get(item, "id")] = index
                    if _get(event, "output_index") is not None:
                        by_output_index[_get(event, "output_index")] = index
                    yield _chunk(cid, model, {"tool_calls": [{
                        "index": index,
                        "id": str(_get(item, "call_id") or _get(item, "id") or _new_call_id()),
                        "type": "function",
                        "function": {"name": str(_get(item, "name") or ""), "arguments": ""},
                    }]})
            elif kind == "response.function_call_arguments.delta":
                index = tool_index(event)
                delta = str(_get(event, "delta") or "")
                if index is not None and delta:
                    args_seen.add(index)
                    yield _chunk(cid, model, {"tool_calls": [
                        {"index": index, "function": {"arguments": delta}}]})
            elif kind == "response.function_call_arguments.done":
                index = tool_index(event)
                if index is not None and index not in args_seen:
                    args_seen.add(index)
                    yield _chunk(cid, model, {"tool_calls": [
                        {"index": index,
                         "function": {"arguments": _args_json(_get(event, "arguments"))}}]})
            elif kind == "response.output_text.delta":
                yield _chunk(cid, model, {"content": str(_get(event, "delta") or "")})
            elif kind == "response.reasoning_summary_text.delta":
                yield _chunk(cid, model, {"reasoning_content": str(_get(event, "delta") or "")})
            elif kind in {"response.completed", "response.incomplete"}:
                response = _get(event, "response")
                cid = str(_get(response, "id") or cid)
                usage = _responses_usage(_get(response, "usage"))
                finished = True
                yield _chunk(cid, model, {}, finish_reason=_responses_finish(response, next_tool > 0),
                             usage=usage)
            elif kind in {"response.failed", "error"}:
                error = _get(event, "error")
                if error is None:
                    error = _get(_get(event, "response"), "error")
                raise RuntimeError(_provider_error_message(error if error is not None else event))
        if not finished:
            yield _chunk(cid, model, {}, finish_reason="tool_calls" if next_tool else "stop",
                         usage=usage)
    finally:
        _close(events)


class OpenAIResponsesAdapter:
    request_kind = "responses"

    def __init__(self, client: Any):
        self._sdk = client.client

    def complete(self, wire: dict[str, Any]) -> dict[str, Any]:
        response = self._sdk.responses.create(**responses_payload(wire))
        return _responses_completion(response, str(wire.get("model") or ""))

    def stream(self, wire: dict[str, Any]) -> Iterator[dict[str, Any]]:
        events = self._sdk.responses.create(**responses_payload(wire), stream=True)
        return _responses_events(events, str(wire.get("model") or ""))


# -- gemini_generate_content ----------------------------------------------------------------

_gemini_signature_lock = threading.Lock()
_gemini_signatures: OrderedDict[str, str] = OrderedDict()


def _remember_gemini_signature(call_id: str, signature: Any) -> None:
    if not isinstance(signature, str) or not signature:
        return
    with _gemini_signature_lock:
        _gemini_signatures[call_id] = signature
        _gemini_signatures.move_to_end(call_id)
        while len(_gemini_signatures) > _GEMINI_SIGNATURE_CACHE_SIZE:
            _gemini_signatures.popitem(last=False)


def _gemini_signature(call_id: str) -> str | None:
    with _gemini_signature_lock:
        return _gemini_signatures.get(call_id)


def _gemini_headers(api_key: str) -> dict[str, str]:
    return {
        "content-type": "application/json",
        "authorization": f"Bearer {api_key}",
        "x-goog-api-key": api_key,
        "accept": "text/event-stream, application/json",
    }


def _gemini_endpoint(base_url: str, model: str, method: str, *, stream: bool = False) -> str:
    parsed = urlsplit(base_url.rstrip("/"))
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1beta"):
        path = f"{path}/v1beta"
    path = f"{path}/models/{quote(model, safe='')}:{method}"
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if stream and ("alt", "sse") not in query:
        query.append(("alt", "sse"))
    return urlunsplit((parsed.scheme, parsed.netloc, path, urlencode(query), ""))


def _gemini_schema(schema: Any) -> Any:
    """Strip JSON-schema keywords Gemini rejects; flatten ``type: [T, "null"]``."""
    if isinstance(schema, list):
        return [_gemini_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _GEMINI_SCHEMA_DROP:
            continue
        if key == "type" and isinstance(value, list):
            non_null = [t for t in value if t != "null"]
            out["type"] = non_null[0] if non_null else "string"
            if "null" in value:
                out["nullable"] = True
            continue
        out[key] = _gemini_schema(value)
    return out


def _gemini_parameters(parameters: dict[str, Any]) -> dict[str, Any] | None:
    schema = _gemini_schema(parameters)
    if not isinstance(schema, dict) or not schema:
        return None
    schema.setdefault("type", "object")
    if str(schema.get("type")).lower() == "object" and not schema.get("properties"):
        return None  # Gemini rejects OBJECT schemas with empty properties; omit for no-arg tools
    return schema


def gemini_payload(wire: dict[str, Any]) -> dict[str, Any]:
    system_parts: list[dict[str, Any]] = []
    contents: list[dict[str, Any]] = []
    names: dict[str, str] = {}  # tool_call_id → function name (functionResponse needs the name)

    def push(role: str, parts: list[dict[str, Any]]) -> None:
        if not parts:
            return
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": parts})

    for message in wire.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        content = message.get("content")
        if role in {"system", "developer"}:
            text = _content_text(content)
            if text:
                system_parts.append({"text": text})
        elif role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            name = names.get(call_id) or str(message.get("name") or call_id or "tool")
            raw = _content_text(content)
            try:
                parsed = json.loads(raw) if raw else None
            except ValueError:
                parsed = None
            result = parsed if parsed is not None else {"text": raw}
            push("user", [{"functionResponse": {"name": name, "response": {"result": result}}}])
        elif role == "assistant":
            parts = [{"text": p["text"]} for p in _content_parts(content) if p["type"] == "text"]
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                call_id = str(call.get("id") or "")
                name = str(fn.get("name") or "")
                if call_id:
                    names[call_id] = name
                part: dict[str, Any] = {
                    "functionCall": {"name": name, "args": _parse_args(fn.get("arguments"))}}
                signature = _gemini_signature(call_id) if call_id else None
                if signature:
                    part["thoughtSignature"] = signature
                parts.append(part)
            push("model", parts)
        else:
            parts = []
            for part in _content_parts(content):
                if part["type"] == "text":
                    parts.append({"text": part["text"]})
                    continue
                match = _DATA_URL.fullmatch(part["url"])
                if match:
                    parts.append({"inlineData": {"mimeType": match.group(1), "data": match.group(2)}})
                else:
                    parts.append({"text": f"[image] {part['url']}"})
            push("user", parts)

    while contents and contents[0]["role"] == "model":
        contents.pop(0)

    generation: dict[str, Any] = {}
    if wire.get("temperature") is not None:
        generation["temperature"] = wire["temperature"]
    if _int(wire.get("max_tokens")):
        generation["maxOutputTokens"] = wire["max_tokens"]
    if isinstance(wire.get("stop"), list) and wire["stop"]:
        generation["stopSequences"] = [str(s) for s in wire["stop"]]
    payload: dict[str, Any] = {"contents": contents}
    if generation:
        payload["generationConfig"] = generation
    if system_parts:
        payload["systemInstruction"] = {"parts": system_parts}

    functions = _function_tools(wire.get("tools"))
    if functions:
        declarations = []
        for fn in functions:
            declaration: dict[str, Any] = {"name": fn["name"]}
            if fn["description"]:
                declaration["description"] = fn["description"]
            parameters = _gemini_parameters(fn["parameters"])
            if parameters:
                declaration["parameters"] = parameters
            declarations.append(declaration)
        payload["tools"] = [{"functionDeclarations": declarations}]
        choice = _tool_choice(wire.get("tool_choice"))
        if choice:
            mode, name = choice
            config: dict[str, Any] = {"mode": {"auto": "AUTO", "none": "NONE"}.get(mode, "ANY")}
            if mode == "function":
                config["allowedFunctionNames"] = [name]
            payload["toolConfig"] = {"functionCallingConfig": config}
    return payload


def _raise_for_gemini(response: Any) -> None:
    status = _int(_get(response, "status_code"))
    if status is None or status < 400:
        return
    read = getattr(response, "read", None)
    if callable(read):
        try:
            read()
        except Exception:  # noqa: BLE001
            pass
    body: Any = None
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        body = getattr(response, "text", None)
    raise RuntimeError(f"gemini HTTP {status}: {_provider_error_message(body)}")


class _GeminiState:
    def __init__(self, cid: str, model: str):
        self.cid = cid
        self.model = model
        self.tool_count = 0
        self.finish: str | None = None
        self.usage: dict[str, Any] | None = None


def _gemini_deltas(data: dict[str, Any], state: _GeminiState) -> Iterator[dict[str, Any]]:
    """Translate one generateContent body (a full response or one SSE frame) into chunks."""
    if isinstance(data.get("error"), dict):
        raise RuntimeError(_provider_error_message(data))
    state.cid = str(data.get("responseId") or state.cid)
    usage = data.get("usageMetadata")
    if isinstance(usage, dict):
        state.usage = _usage(usage.get("promptTokenCount"), usage.get("candidatesTokenCount"),
                             usage.get("totalTokenCount")) or state.usage
    candidates = data.get("candidates") or []
    candidate = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
    content = candidate.get("content") if isinstance(candidate.get("content"), dict) else {}
    for part in content.get("parts") or []:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("functionCall"), dict):
            call = part["functionCall"]
            index = state.tool_count
            state.tool_count += 1
            call_id = str(call.get("id") or _new_call_id())
            _remember_gemini_signature(call_id, part.get("thoughtSignature"))
            yield _chunk(state.cid, state.model, {"tool_calls": [{
                "index": index, "id": call_id, "type": "function",
                "function": {"name": str(call.get("name") or ""),
                             "arguments": _args_json(call.get("args") or {})},
            }]})
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            key = "reasoning_content" if part.get("thought") else "content"
            yield _chunk(state.cid, state.model, {key: text})
    finish_reason = candidate.get("finishReason")
    if not finish_reason and not candidates:
        feedback = data.get("promptFeedback")
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            finish_reason = "SAFETY"
    if finish_reason:
        finish = _GEMINI_FINISH_REASONS.get(str(finish_reason).upper(), "stop")
        if finish == "stop" and state.tool_count:
            finish = "tool_calls"
        state.finish = finish
        yield _chunk(state.cid, state.model, {}, finish_reason=finish, usage=state.usage)


def _gemini_completion(data: dict[str, Any], model: str) -> dict[str, Any]:
    state = _GeminiState(_new_id(), model)
    text: list[str] = []
    reasoning: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for chunk in _gemini_deltas(data, state):
        delta = chunk["choices"][0]["delta"]
        if delta.get("content"):
            text.append(delta["content"])
        if delta.get("reasoning_content"):
            reasoning.append(delta["reasoning_content"])
        for call in delta.get("tool_calls") or []:
            tool_calls.append({"id": call["id"], "type": "function", "function": call["function"]})
    finish = state.finish or ("tool_calls" if tool_calls else "stop")
    return _completion(state.cid, model, _assistant_message("".join(text), tool_calls,
                                                             "".join(reasoning)), finish, state.usage)


def _gemini_events(ctx: Any, response: Any, model: str) -> Iterator[dict[str, Any]]:
    state = _GeminiState(_new_id(), model)
    try:
        yield _chunk(state.cid, model, {"role": "assistant", "content": ""})
        for line in response.iter_lines():
            if isinstance(line, bytes):
                line = line.decode("utf-8", "replace")
            line = line.strip()
            if not line or line.startswith((":", "event:", "id:", "retry:")):
                continue
            raw = line[5:].strip() if line.startswith("data:") else line
            if not raw or raw == "[DONE]":
                continue
            try:
                data = json.loads(raw)
            except ValueError as exc:
                raise RuntimeError(f"gemini stream sent a non-JSON frame: {raw[:200]}") from exc
            if isinstance(data, dict):
                yield from _gemini_deltas(data, state)
        if state.finish is None:
            yield _chunk(state.cid, model, {},
                         finish_reason="tool_calls" if state.tool_count else "stop",
                         usage=state.usage)
    finally:
        ctx.__exit__(None, None, None)


class GeminiAdapter:
    request_kind = "gemini.generate_content"

    def __init__(self, client: Any):
        self._http = client.client
        self._base_url = str(getattr(client, "base_url", "") or "")
        self._api_key = str(getattr(client, "api_key", "") or "")

    def complete(self, wire: dict[str, Any]) -> dict[str, Any]:
        model = str(wire.get("model") or "")
        response = self._http.post(
            _gemini_endpoint(self._base_url, model, "generateContent"),
            headers=_gemini_headers(self._api_key), json=gemini_payload(wire),
        )
        _raise_for_gemini(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError("gemini returned a non-JSON body") from exc
        if not isinstance(data, dict):
            raise RuntimeError("gemini returned an unexpected body shape")
        return _gemini_completion(data, model)

    def stream(self, wire: dict[str, Any]) -> Iterator[dict[str, Any]]:
        model = str(wire.get("model") or "")
        ctx = self._http.stream(
            "POST", _gemini_endpoint(self._base_url, model, "streamGenerateContent", stream=True),
            headers=_gemini_headers(self._api_key), json=gemini_payload(wire),
        )
        response = ctx.__enter__()  # the request is sent here, so HTTP errors surface eagerly
        try:
            _raise_for_gemini(response)
        except BaseException:
            ctx.__exit__(*sys.exc_info())
            raise
        return _gemini_events(ctx, response, model)
