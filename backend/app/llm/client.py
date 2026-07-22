from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping
import copy
import hashlib
import json
import math
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from openai import OpenAI
from anthropic import Anthropic

from app.config import get_settings
from app.db.models import ModelConfig
from app.llm.model_protocols import ModelApiProtocol
from app.llm.output_policy import operation_output_tokens
from app.llm.protocol_drivers import (
    AnthropicMessagesDriver,
    CancellationToken,
    ChatCompletionsDriver,
    GeminiGenerateContentDriver,
    ProtocolCallError,
)
from app.llm.stage_protocol import (
    STAGE_PROTOCOL_KEY,
    TURN_STAGE_MESSAGES_KEY,
    render_stage_user_message,
)
from app.observability.spans import current_llm_operation, llm_span_attributes, start_llm_call
from app.security.encryption import decrypt_secret


class LLMError(Exception):
    """Raised when an LLM provider request or response normalization fails."""


JSON_REPAIR_ATTEMPTS = 3
EMPTY_RESPONSE_RETRIES = 2
EMPTY_RESPONSE_MESSAGE = "Model returned an empty response"
DEFAULT_MODEL_API_TIMEOUT_SECONDS = 600.0
DEFAULT_INPUT_TOKEN_BUDGET = 32_000
TURN_STAGE_MESSAGE_MARKER = "_agent_turn_message"


class _CurrentStageText(str):
    pass


class LLMClient:
    def __init__(self, model_config: ModelConfig):
        try:
            protocol = ModelApiProtocol(
                getattr(model_config, "api_protocol", "openai_chat_completions")
            )
        except ValueError as exc:
            raise LLMError("MODEL_PROTOCOL_UNSUPPORTED") from exc
        api_key = decrypt_secret(model_config.api_key_encrypted)
        if not api_key:
            raise LLMError("Model API key is not configured")
        self.timeout_seconds = (
            getattr(model_config, "timeout_seconds", None)
            or get_settings().model_api_timeout_seconds
            or DEFAULT_MODEL_API_TIMEOUT_SECONDS
        )
        self.base_url = str(model_config.base_url or "")
        if protocol is ModelApiProtocol.OPENAI_CHAT_COMPLETIONS:
            self.client = OpenAI(
                api_key=api_key,
                base_url=self.base_url,
                timeout=self.timeout_seconds,
            )
            self.driver = ChatCompletionsDriver(self.client)
        elif protocol is ModelApiProtocol.ANTHROPIC_MESSAGES:
            kwargs: dict[str, Any] = {
                "api_key": api_key,
                "timeout": self.timeout_seconds,
                "max_retries": 0,
            }
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self.client = Anthropic(**kwargs)
            self.driver = AnthropicMessagesDriver(self.client)
        elif protocol is ModelApiProtocol.GEMINI_GENERATE_CONTENT:
            self.client = httpx.Client(timeout=self.timeout_seconds)
            self.driver = GeminiGenerateContentDriver(
                self.client,
                self.base_url,
                api_key,
                model_config.model,
            )
        else:
            raise LLMError("MODEL_PROTOCOL_UNSUPPORTED")
        self.api_protocol = protocol
        self.api_key = api_key
        self.model = model_config.model
        self.temperature = model_config.temperature
        self.max_output_tokens = model_config.max_output_tokens
        legacy_extra_body = getattr(model_config, "legacy_extra_body", {})
        protocol_options = getattr(model_config, "protocol_options", {})
        self.extra_body = _normalize_extra_body(
            legacy_extra_body
            or getattr(model_config, "extra_body_json", {})
            or protocol_options
        )
        settings = get_settings()
        self.thinking_mode = (
            _thinking_mode_from_extra_body(self.extra_body)
            or _thinking_mode_for_model(
                getattr(settings, "model_thinking_mode", ""),
                getattr(settings, "model_thinking_models", ""),
                self.model,
            )
        )

    def generate_text(
        self,
        system_prompt: str,
        user_payload: dict[str, Any] | str,
        response_format: dict[str, str] | None = None,
        cancellation: CancellationToken | None = None,
    ) -> str:
        max_output_tokens = operation_output_tokens(
            current_llm_operation(), self.max_output_tokens
        )
        context_messages, serialized = _prepare_user_input(user_payload)
        request_messages = _request_messages(system_prompt, context_messages, serialized)
        request_messages = _fit_request_messages(request_messages)
        if isinstance(user_payload, dict) and isinstance(
            user_payload.get(STAGE_PROTOCOL_KEY), dict
        ):
            self._last_stage_request_user_content = copy.deepcopy(
                request_messages[-1].get("content")
            )
        request_shape = _request_shape_metrics(
            system_prompt, context_messages, serialized, request_messages
        )
        try:
            request: dict[str, Any] = {
                "model": self.model,
                "messages": request_messages,
                "temperature": self.temperature,
                "max_tokens": max_output_tokens,
            }
            if cancellation is not None:
                request["_cancellation"] = cancellation
            if response_format:
                request["response_format"] = response_format
            request.update(
                _thinking_request_kwargs(
                    getattr(self, "thinking_mode", ""),
                    getattr(self, "extra_body", {}),
                )
            )
            empty_diagnostics: list[str] = []
            for attempt in range(EMPTY_RESPONSE_RETRIES + 1):
                span = start_llm_call(
                    model=self.model,
                    endpoint=_endpoint_label(getattr(self, "base_url", "")),
                    request_kind=self._protocol_driver().request_kind,
                    stream=False,
                    attempt=attempt + 1,
                    retry_count=attempt,
                    max_attempts=EMPTY_RESPONSE_RETRIES + 1,
                    max_output_tokens=max_output_tokens,
                    thinking_mode=getattr(self, "thinking_mode", "") or "provider_default",
                    **request_shape,
                )
                try:
                    completion = self._protocol_driver().complete(request)
                except BaseException as exc:
                    span.fail(exc, **_completion_span_metrics(None))
                    raise
                content = _completion_message_content(completion)
                metrics = _completion_span_metrics(completion)
                if content.strip():
                    span.finish(
                        ttft_ms=span.elapsed_ms(),
                        output_chars=len(content),
                        status="success",
                        **metrics,
                    )
                    if not getattr(self, "_defer_stage_recording", False):
                        _record_stage_exchange(
                            user_payload,
                            content,
                            request_user_content=getattr(
                                self, "_last_stage_request_user_content", None
                            ),
                        )
                    return content
                span.finish(
                    ttft_ms=span.elapsed_ms(),
                    output_chars=0,
                    status="empty",
                    **metrics,
                )
                empty_diagnostics.append(_completion_empty_diagnostic(completion, attempt + 1))
                if attempt >= EMPTY_RESPONSE_RETRIES:
                    raise LLMError(_empty_response_detail(self, empty_diagnostics))
        except Exception as exc:
            if isinstance(exc, LLMError):
                raise
            if isinstance(exc, ProtocolCallError):
                raise LLMError(exc.code) from exc
            raise LLMError(_provider_failure_detail(self, exc)) from exc

    def generate_text_stream(
        self,
        system_prompt: str,
        user_payload: dict[str, Any] | str,
        cancellation: CancellationToken | None = None,
    ) -> Iterator[str]:
        max_output_tokens = operation_output_tokens(
            current_llm_operation(), self.max_output_tokens
        )
        context_messages, serialized = _prepare_user_input(user_payload)
        request_messages = _request_messages(system_prompt, context_messages, serialized)
        request_messages = _fit_request_messages(request_messages)
        if isinstance(user_payload, dict) and isinstance(
            user_payload.get(STAGE_PROTOCOL_KEY), dict
        ):
            self._last_stage_request_user_content = copy.deepcopy(
                request_messages[-1].get("content")
            )
        request_shape = _request_shape_metrics(
            system_prompt, context_messages, serialized, request_messages
        )
        try:
            empty_diagnostics: list[str] = []
            for attempt in range(EMPTY_RESPONSE_RETRIES + 1):
                span = start_llm_call(
                    model=self.model,
                    endpoint=_endpoint_label(getattr(self, "base_url", "")),
                    request_kind=self._protocol_driver().request_kind,
                    stream=True,
                    attempt=attempt + 1,
                    retry_count=attempt,
                    max_attempts=EMPTY_RESPONSE_RETRIES + 1,
                    max_output_tokens=max_output_tokens,
                    thinking_mode=getattr(self, "thinking_mode", "") or "provider_default",
                    **request_shape,
                )
                stream_usage_metrics: dict[str, Any] = {}
                pending_parts: list[str] = []
                recorded_parts: list[str] = []
                emitted_text = False
                chunk_count = 0
                choice_chunk_count = 0
                reasoning_chars = 0
                output_chars = 0
                first_content_ms: float | None = None
                provider_setup_ms: float | None = None
                finish_reasons: set[str] = set()
                response_ids: set[str] = set()
                try:
                    stream_request = {
                        "model": self.model,
                        "messages": request_messages,
                        "temperature": self.temperature,
                        "max_tokens": max_output_tokens,
                        **_thinking_request_kwargs(
                            getattr(self, "thinking_mode", ""),
                            getattr(self, "extra_body", {}),
                        ),
                    }
                    if cancellation is not None:
                        stream_request["_cancellation"] = cancellation
                    stream = self._protocol_driver().stream(stream_request)
                    provider_setup_ms = span.elapsed_ms()
                    for chunk in stream:
                        chunk_count += 1
                        chunk_usage_metrics = _usage_span_metrics(getattr(chunk, "usage", None))
                        if chunk_usage_metrics:
                            stream_usage_metrics.update(chunk_usage_metrics)
                        response_id = _safe_fragment(getattr(chunk, "id", None), 48)
                        if response_id:
                            response_ids.add(response_id)
                        choices = getattr(chunk, "choices", None) or []
                        if not choices:
                            continue
                        choice_chunk_count += len(choices)
                        choice = choices[0]
                        finish_reason = _safe_fragment(getattr(choice, "finish_reason", None), 32)
                        if finish_reason:
                            finish_reasons.add(finish_reason)
                        delta = getattr(choice, "delta", None)
                        reasoning_chars += len(_reasoning_text(delta))
                        content = _content_text(getattr(delta, "content", None))
                        if not content:
                            continue
                        recorded_parts.append(content)
                        output_chars += len(content)
                        if first_content_ms is None:
                            first_content_ms = span.elapsed_ms()
                        if emitted_text:
                            yield content
                            continue
                        pending_parts.append(content)
                        buffered = "".join(pending_parts)
                        if buffered.strip():
                            emitted_text = True
                            pending_parts.clear()
                            yield buffered
                except BaseException as exc:
                    span.fail(
                        exc,
                        provider_setup_ms=provider_setup_ms,
                        ttft_ms=first_content_ms,
                        output_chars=output_chars,
                        stream_chunks=chunk_count,
                        reasoning_chars=reasoning_chars,
                        **stream_usage_metrics,
                    )
                    raise
                if emitted_text:
                    span.finish(
                        provider_setup_ms=provider_setup_ms,
                        ttft_ms=first_content_ms,
                        stream_duration_ms=round(span.elapsed_ms() - (first_content_ms or 0), 3),
                        output_chars=output_chars,
                        stream_chunks=chunk_count,
                        choice_chunks=choice_chunk_count,
                        reasoning_chars=reasoning_chars,
                        finish_reasons=sorted(finish_reasons),
                        provider_response_ids=sorted(response_ids),
                        **stream_usage_metrics,
                    )
                    _record_stage_exchange(
                        user_payload,
                        "".join(recorded_parts),
                        request_user_content=getattr(
                            self, "_last_stage_request_user_content", None
                        ),
                    )
                    return
                span.finish(
                    provider_setup_ms=provider_setup_ms,
                    ttft_ms=None,
                    output_chars=0,
                    stream_chunks=chunk_count,
                    choice_chunks=choice_chunk_count,
                    reasoning_chars=reasoning_chars,
                    finish_reasons=sorted(finish_reasons),
                    provider_response_ids=sorted(response_ids),
                    status="empty",
                    **stream_usage_metrics,
                )
                empty_diagnostics.append(
                    _stream_empty_diagnostic(
                        attempt + 1,
                        chunk_count,
                        choice_chunk_count,
                        reasoning_chars,
                        finish_reasons,
                        response_ids,
                    )
                )
            raise LLMError(_empty_response_detail(self, empty_diagnostics))
        except Exception as exc:
            if isinstance(exc, LLMError):
                raise
            if isinstance(exc, ProtocolCallError):
                raise LLMError(exc.code) from exc
            raise LLMError(_provider_failure_detail(self, exc)) from exc

    def _protocol_driver(
        self,
    ) -> ChatCompletionsDriver | AnthropicMessagesDriver | GeminiGenerateContentDriver:
        driver = getattr(self, "driver", None)
        if driver is None:
            if getattr(self, "api_protocol", ModelApiProtocol.OPENAI_CHAT_COMPLETIONS) is (
                ModelApiProtocol.GEMINI_GENERATE_CONTENT
            ):
                driver = GeminiGenerateContentDriver(
                    self.client,
                    self.base_url,
                    getattr(self, "api_key", ""),
                    self.model,
                )
            else:
                driver = ChatCompletionsDriver(self.client)
            self.driver = driver
        return driver

    def generate_json(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
        cancellation: CancellationToken | None = None,
    ) -> dict[str, Any]:
        outputs: list[str] = []
        next_payload = user_payload
        last_error: json.JSONDecodeError | None = None
        json_mode_supported = True
        for attempt in range(JSON_REPAIR_ATTEMPTS + 1):
            with llm_span_attributes(
                response_mode="json",
                json_attempt=attempt + 1,
                json_retry_count=attempt,
                json_max_attempts=JSON_REPAIR_ATTEMPTS + 1,
            ):
                previous_defer = getattr(self, "_defer_stage_recording", False)
                self._defer_stage_recording = True
                try:
                    text = self._generate_json_candidate(
                        system_prompt, next_payload, json_mode_supported, cancellation
                    )
                    if json_mode_supported and _response_format_unsupported(text):
                        json_mode_supported = False
                        text = self.generate_text(system_prompt, next_payload)
                finally:
                    self._defer_stage_recording = previous_defer
            outputs.append(text)
            try:
                parsed = _loads_llm_json(text)
                _record_stage_exchange(
                    next_payload,
                    text,
                    request_user_content=getattr(
                        self, "_last_stage_request_user_content", None
                    ),
                )
                return parsed
            except json.JSONDecodeError as exc:
                last_error = exc
                _record_stage_exchange(
                    next_payload,
                    text,
                    request_user_content=getattr(
                        self, "_last_stage_request_user_content", None
                    ),
                )
                if attempt >= JSON_REPAIR_ATTEMPTS:
                    break
                next_payload = copy.deepcopy(user_payload)
                if isinstance(user_payload.get(STAGE_PROTOCOL_KEY), dict):
                    next_payload["conversation_context"] = user_payload.get(
                        "conversation_context"
                    )
                next_payload["_json_repair"] = {
                    "attempt": attempt + 1,
                    "max_attempts": JSON_REPAIR_ATTEMPTS,
                    "previous_output": _preview(text),
                    "parser_error": str(exc),
                    "instruction": (
                        "上一轮输出不是合法 JSON。请基于原始任务上下文重新输出完整、可解析的 JSON object。"
                        "字符串内部的双引号必须转义；不要输出 Markdown、解释、代码块或额外文本。"
                    ),
                }
        previews = "; ".join(
            f"attempt_{index + 1}_preview={_preview(output)!r}"
            for index, output in enumerate(outputs)
        )
        raise LLMError(
            f"Model did not return valid JSON after {JSON_REPAIR_ATTEMPTS} repair attempts; {previews}"
        ) from last_error

    def _generate_json_candidate(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
        json_mode_supported: bool,
        cancellation: CancellationToken | None = None,
    ) -> str:
        def call_generate_text(prompt: str, payload: dict[str, Any], **kwargs: Any) -> str:
            if cancellation is not None:
                kwargs["cancellation"] = cancellation
            return self.generate_text(prompt, payload, **kwargs)

        if getattr(self, "api_protocol", ModelApiProtocol.OPENAI_CHAT_COMPLETIONS) is (
            ModelApiProtocol.ANTHROPIC_MESSAGES
        ):
            return call_generate_text(
                system_prompt.rstrip()
                + "\n\n只返回一个合法 JSON object；不要输出 Markdown、代码围栏、解释或额外文本。",
                user_payload,
            )
        if not json_mode_supported:
            return call_generate_text(system_prompt, user_payload)
        try:
            return call_generate_text(
                system_prompt,
                user_payload,
                response_format={"type": "json_object"},
            )
        except TypeError:
            return call_generate_text(system_prompt, user_payload)
        except LLMError as exc:
            message = str(exc)
            if _response_format_unsupported(message):
                return message
            if _empty_response(message):
                return call_generate_text(system_prompt, user_payload)
            raise


def _completion_message_content(completion: Any) -> str:
    try:
        choice = completion.choices[0]
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None)
    except (IndexError, TypeError, AttributeError):
        return ""
    return _content_text(content)


def _request_shape_metrics(
    system_prompt: str,
    context_messages: list[dict[str, Any]],
    serialized_payload: str | list[dict[str, Any]],
    request_messages: list[dict[str, Any]],
) -> dict[str, Any]:
    context_chars = sum(
        len(_content_text(message.get("content"))) for message in context_messages
    )
    request_chars = sum(
        len(_content_text(message.get("content"))) for message in request_messages
    )
    return {
        "system_prompt_chars": len(system_prompt),
        "context_message_count": len(context_messages),
        "context_text_chars": context_chars,
        "payload_chars": len(_content_text(serialized_payload)),
        "request_text_chars": request_chars,
        "request_message_count": len(request_messages),
        "request_message_roles": [str(message.get("role") or "") for message in request_messages],
        "request_message_chars": [
            len(_content_text(message.get("content"))) for message in request_messages
        ],
        "request_prefix_fingerprints": _request_prefix_fingerprints(request_messages),
    }


def _request_prefix_fingerprints(messages: list[dict[str, Any]]) -> list[str]:
    digest = hashlib.sha256()
    fingerprints: list[str] = []
    for message in messages:
        serialized = json.dumps(
            {
                "role": str(message.get("role") or ""),
                "content": message.get("content"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        digest.update(serialized.encode("utf-8"))
        digest.update(b"\n")
        fingerprints.append(digest.hexdigest()[:16])
    return fingerprints


def _normalize_thinking_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in {"enabled", "disabled"} else ""


def _thinking_mode_for_model(mode: Any, configured_models: Any, model: Any) -> str:
    normalized_mode = _normalize_thinking_mode(mode)
    if not normalized_mode:
        return ""
    allowed_models = {
        item.strip().lower()
        for item in str(configured_models or "").split(",")
        if item.strip()
    }
    if allowed_models and str(model or "").strip().lower() not in allowed_models:
        return ""
    return normalized_mode


def _normalize_extra_body(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): _mutable_copy(item) for key, item in value.items()}


def _mutable_copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _mutable_copy(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_mutable_copy(item) for item in value]
    return copy.deepcopy(value)


def _thinking_mode_from_extra_body(extra_body: Any) -> str:
    normalized = _normalize_extra_body(extra_body)
    thinking = normalized.get("thinking")
    if not isinstance(thinking, dict):
        return ""
    return _normalize_thinking_mode(thinking.get("type"))


def _thinking_request_kwargs(mode: Any, extra_body: Any = None) -> dict[str, Any]:
    body = _normalize_extra_body(extra_body)
    normalized = _normalize_thinking_mode(mode)
    if normalized:
        thinking = body.get("thinking")
        body["thinking"] = {
            **(thinking if isinstance(thinking, dict) else {}),
            "type": normalized,
        }
    return {"extra_body": body} if body else {}


def _request_messages(
    system_prompt: str,
    context_messages: list[dict[str, Any]],
    serialized_payload: str | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt.rstrip()}
    ]
    messages.extend(context_messages)
    if serialized_payload != "{}":
        current_input = serialized_payload
        if (
            context_messages
            and isinstance(serialized_payload, str)
            and not isinstance(serialized_payload, _CurrentStageText)
        ):
            current_input = (
                "本轮输入（仅用于当前调用，不写入对话历史）：\n"
                f"{serialized_payload}"
            )
        messages.append(
            {
                "role": "user",
                "content": current_input,
            }
        )
    elif not context_messages:
        messages.append({"role": "user", "content": "{}"})
    return messages


def _fit_request_messages(
    messages: list[dict[str, Any]], token_budget: int = DEFAULT_INPUT_TOKEN_BUDGET
) -> list[dict[str, Any]]:
    projected = copy.deepcopy(messages)
    while len(projected) > 2 and _request_tokens(projected) > token_budget:
        removable_index = next(
            (
                index
                for index in range(1, len(projected) - 1)
                if not _is_history_summary_message(projected[index])
                and not _is_turn_stage_message(projected[index])
            ),
            None,
        )
        if removable_index is None:
            break
        projected.pop(removable_index)

    while len(projected) > 2 and _request_tokens(projected) > token_budget:
        removable_index = next(
            (
                index
                for index in range(1, len(projected) - 1)
                if not _is_turn_stage_message(projected[index])
            ),
            None,
        )
        if removable_index is None:
            break
        projected.pop(removable_index)

    _trim_turn_stage_messages(projected, token_budget)
    _drop_oldest_turn_stage_exchanges(projected, token_budget)

    if projected and _request_tokens(projected) > token_budget:
        fixed_tokens = _request_tokens(projected[:-1])
        projected[-1] = _trim_request_message(
            projected[-1], max(1, token_budget - fixed_tokens)
        )
    return [
        {key: value for key, value in message.items() if key != TURN_STAGE_MESSAGE_MARKER}
        for message in projected
    ]


def _request_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(
        max(1, math.ceil(len(_content_text(message.get("content")).encode("utf-8")) / 4))
        + 6
        for message in messages
    )


def _is_history_summary_message(message: dict[str, Any]) -> bool:
    content = _content_text(message.get("content")).lstrip()
    return content.startswith(
        ("历史的信息可以被总结为：", "近期的历史信息总结为：")
    )


def _is_turn_stage_message(message: dict[str, Any]) -> bool:
    return message.get(TURN_STAGE_MESSAGE_MARKER) is True


def _trim_turn_stage_messages(
    messages: list[dict[str, Any]], token_budget: int
) -> None:
    while _request_tokens(messages) > token_budget:
        candidates = [
            (len(_content_text(message.get("content"))), index)
            for index, message in enumerate(messages[1:-1], start=1)
            if _is_turn_stage_message(message)
            and len(_content_text(message.get("content"))) > 512
        ]
        if not candidates:
            break
        current_length, index = max(candidates)
        excess_tokens = _request_tokens(messages) - token_budget
        target_tokens = max(128, math.ceil(current_length / 4) - excess_tokens)
        trimmed = _trim_request_message(messages[index], target_tokens)
        trimmed[TURN_STAGE_MESSAGE_MARKER] = True
        if len(_content_text(trimmed.get("content"))) >= current_length:
            break
        messages[index] = trimmed


def _drop_oldest_turn_stage_exchanges(
    messages: list[dict[str, Any]], token_budget: int
) -> None:
    while _request_tokens(messages) > token_budget:
        stage_indices = [
            index
            for index, message in enumerate(messages[1:-1], start=1)
            if _is_turn_stage_message(message)
        ]
        if len(stage_indices) <= 2:
            break
        first_index = stage_indices[0]
        remove_count = 1
        if (
            len(stage_indices) > 1
            and stage_indices[1] == first_index + 1
            and messages[first_index].get("role") == "user"
            and messages[first_index + 1].get("role") == "assistant"
        ):
            remove_count = 2
        del messages[first_index : first_index + remove_count]


def _trim_request_message(
    message: dict[str, Any], token_budget: int
) -> dict[str, Any]:
    content = message.get("content")
    byte_budget = max(4, token_budget * 4)
    if isinstance(content, list):
        parts = copy.deepcopy(content)
        text_part = next(
            (
                part
                for part in parts
                if isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            ),
            None,
        )
        if text_part is not None:
            text_part["text"] = _trim_request_text(text_part["text"], byte_budget)
        return {**message, "content": parts}
    return {**message, "content": _trim_request_text(str(content or ""), byte_budget)}


def _trim_request_text(text: str, byte_budget: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= byte_budget:
        return text
    marker = "\n...<输入超过 32k，已省略中间部分>...\n"
    marker_bytes = len(marker.encode("utf-8"))
    available = max(8, byte_budget - marker_bytes)
    head_size = int(available * 0.7)
    tail_size = available - head_size
    head = encoded[:head_size].decode("utf-8", errors="ignore")
    tail = encoded[-tail_size:].decode("utf-8", errors="ignore")
    return f"{head}{marker}{tail}"


def _completion_span_metrics(completion: Any) -> dict[str, Any]:
    if completion is None:
        return {}
    choices = getattr(completion, "choices", None) or []
    finish_reason = None
    message = None
    if choices:
        finish_reason = _safe_fragment(getattr(choices[0], "finish_reason", None), 32) or None
        message = getattr(choices[0], "message", None)
    usage = getattr(completion, "usage", None)
    return {
        "provider_response_id": _safe_fragment(getattr(completion, "id", None), 48) or None,
        "finish_reason": finish_reason,
        "reasoning_chars": len(_reasoning_text(message)),
        **_usage_span_metrics(usage),
    }


def _usage_span_metrics(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    input_tokens = _usage_value(usage, "prompt_tokens", "input_tokens")
    output_tokens = _usage_value(usage, "completion_tokens", "output_tokens")
    total_tokens = _usage_value(usage, "total_tokens")
    prompt_details = _usage_object(usage, "prompt_tokens_details", "input_tokens_details")
    cached_input_tokens = _usage_value(
        prompt_details,
        "cached_tokens",
        "cache_read_tokens",
        "cache_read_input_tokens",
    )
    if cached_input_tokens is None:
        cached_input_tokens = _usage_value(
            usage,
            "cached_tokens",
            "prompt_cache_hit_tokens",
            "cache_read_input_tokens",
        )
    metrics: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_input_tokens": cached_input_tokens,
    }
    if input_tokens is not None and cached_input_tokens is not None:
        metrics["uncached_input_tokens"] = max(0, input_tokens - cached_input_tokens)
    return {key: value for key, value in metrics.items() if value is not None}


def _usage_object(source: Any, *names: str) -> Any:
    for name in names:
        value = source.get(name) if isinstance(source, dict) else getattr(source, name, None)
        if value is not None:
            return value
    return None


def _usage_value(source: Any, *names: str) -> int | None:
    value = _usage_object(source, *names)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_content_part_text(item) for item in content)
    return _content_part_text(content)


def _content_part_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    text: Any = item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
    if isinstance(text, str):
        return text
    if isinstance(text, dict) and isinstance(text.get("value"), str):
        return text["value"]
    value = getattr(text, "value", None)
    return value if isinstance(value, str) else ""


def _completion_empty_diagnostic(completion: Any, attempt: int) -> str:
    choices = getattr(completion, "choices", None) or []
    response_id = _safe_fragment(getattr(completion, "id", None), 48) or "missing"
    if not choices:
        return f"attempt_{attempt}: response_id={response_id}, choices=0"
    choice = choices[0]
    message = getattr(choice, "message", None)
    finish_reason = _safe_fragment(getattr(choice, "finish_reason", None), 32) or "missing"
    refusal = _safe_fragment(getattr(message, "refusal", None), 80)
    reasoning_chars = len(_reasoning_text(message))
    tool_calls = getattr(message, "tool_calls", None) or []
    content = getattr(message, "content", None)
    content_shape = _content_shape(content)
    usage = getattr(completion, "usage", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    parts = [
        f"attempt_{attempt}: response_id={response_id}",
        f"choices={len(choices)}",
        f"finish_reason={finish_reason}",
        f"content={content_shape}",
        f"reasoning_chars={reasoning_chars}",
        f"tool_calls={len(tool_calls)}",
    ]
    if refusal:
        parts.append(f"refusal={refusal}")
    if completion_tokens is not None:
        parts.append(f"completion_tokens={completion_tokens}")
    return ", ".join(parts)


def _stream_empty_diagnostic(
    attempt: int,
    chunk_count: int,
    choice_chunk_count: int,
    reasoning_chars: int,
    finish_reasons: set[str],
    response_ids: set[str],
) -> str:
    return (
        f"attempt_{attempt}: stream_chunks={chunk_count}, choice_chunks={choice_chunk_count}, "
        f"finish_reason={','.join(sorted(finish_reasons)) or 'missing'}, text_chars=0, "
        f"reasoning_chars={reasoning_chars}, response_id={','.join(sorted(response_ids)) or 'missing'}"
    )


def _empty_response_detail(client: Any, diagnostics: list[str]) -> str:
    attempts = EMPTY_RESPONSE_RETRIES + 1
    model = _safe_fragment(getattr(client, "model", None), 80) or "unknown"
    endpoint = _endpoint_label(getattr(client, "base_url", None))
    response_details = " | ".join(diagnostics)
    return (
        f"{EMPTY_RESPONSE_MESSAGE} after {attempts} attempts; provider returned no usable message.content; "
        f"model={model}; endpoint={endpoint}; {response_details}"
    )


def _provider_failure_detail(client: Any, exc: Exception) -> str:
    model = _safe_fragment(getattr(client, "model", None), 80) or "unknown"
    endpoint = _endpoint_label(getattr(client, "base_url", None))
    timeout = getattr(client, "timeout_seconds", None)
    status_code = getattr(exc, "status_code", None)
    request_id = _safe_fragment(getattr(exc, "request_id", None), 64)
    error_type = type(exc).__name__
    message = _safe_fragment(exc, 240) or "no provider error message"
    provider_code = ""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error_body = body.get("error") if isinstance(body.get("error"), dict) else body
        provider_code = _safe_fragment(error_body.get("code") or error_body.get("type"), 64)
        provider_message = _safe_fragment(error_body.get("message"), 160)
        if provider_message and provider_message not in message:
            message = f"{message}; provider_message={provider_message}"
    details = [
        f"LLM provider request failed ({error_type})",
        f"message={message}",
        f"model={model}",
        f"endpoint={endpoint}",
    ]
    if status_code is not None:
        details.append(f"status_code={status_code}")
    if provider_code:
        details.append(f"provider_code={provider_code}")
    if request_id:
        details.append(f"request_id={request_id}")
    if timeout is not None:
        details.append(f"timeout_seconds={timeout}")
    return "; ".join(details)


def _content_shape(content: Any) -> str:
    if content is None:
        return "null"
    text = _content_text(content)
    if isinstance(content, str):
        return f"string({len(content)} chars{' whitespace' if content and not content.strip() else ''})"
    if isinstance(content, list):
        return f"list({len(content)} parts, {len(text)} text_chars)"
    return f"{type(content).__name__}({len(text)} text_chars)"


def _reasoning_text(value: Any) -> str:
    if value is None:
        return ""
    for key in ("reasoning_content", "reasoning", "thinking"):
        content = value.get(key) if isinstance(value, dict) else getattr(value, key, None)
        text = _content_text(content)
        if text:
            return text
    return ""


def _safe_fragment(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "sk-***", text)
    text = re.sub(r"\bpt-[A-Za-z0-9_-]{8,}\b", "pt-***", text)
    text = re.sub(
        r"(?i)(api[_-]?key|authorization|access[_-]?token|token)=([^&\s;]+)",
        r"\1=***",
        text,
    )
    return text[:limit]


def _endpoint_label(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "unknown"
    parsed = urlsplit(raw)
    if not parsed.hostname:
        return "configured-endpoint"
    host = parsed.hostname
    try:
        port = parsed.port
    except ValueError:
        return "configured-endpoint"
    if port:
        host = f"{host}:{port}"
    path = parsed.path.rstrip("/")
    return _safe_fragment(f"{parsed.scheme or 'http'}://{host}{path}", 160)


def _extract_json(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").strip()
        if stripped.startswith("json"):
            stripped = stripped[4:].strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end >= start:
        return stripped[start : end + 1]
    return stripped


def _loads_llm_json(text: str) -> Any:
    candidate = _extract_json(text)
    last_error: json.JSONDecodeError | None = None
    seen: set[str] = set()
    for variant in _json_candidate_variants(candidate):
        if variant in seen:
            continue
        seen.add(variant)
        try:
            return json.loads(variant)
        except json.JSONDecodeError as exc:
            last_error = exc
    try:
        literal = ast.literal_eval(candidate)
    except (SyntaxError, ValueError):
        literal = None
    if isinstance(literal, (dict, list)):
        return literal
    if last_error is not None:
        raise last_error
    raise json.JSONDecodeError("Could not decode JSON", candidate, 0)


def _json_candidate_variants(text: str) -> tuple[str, ...]:
    stripped = text.strip()
    no_trailing_commas = _remove_trailing_commas(stripped)
    repaired_strings = _repair_json_string_content(stripped)
    repaired_strings_no_trailing = _remove_trailing_commas(repaired_strings)
    return (
        stripped,
        no_trailing_commas,
        repaired_strings,
        repaired_strings_no_trailing,
    )


def _remove_trailing_commas(text: str) -> str:
    return re.sub(r",\s*([}\]])", r"\1", text)


def _repair_json_string_content(text: str) -> str:
    output: list[str] = []
    in_string = False
    index = 0
    while index < len(text):
        char = text[index]
        if not in_string:
            output.append(char)
            if char == '"':
                in_string = True
            index += 1
            continue
        if char == "\\":
            output.append(char)
            index += 1
            if index < len(text):
                output.append(text[index])
                index += 1
            continue
        if char == '"':
            if _quote_likely_closes_string(text, index):
                output.append(char)
                in_string = False
            else:
                output.append('\\"')
            index += 1
            continue
        if char == "\n":
            output.append("\\n")
        elif char == "\r":
            output.append("\\r")
        elif char == "\t":
            output.append("\\t")
        else:
            output.append(char)
        index += 1
    return "".join(output)


def _quote_likely_closes_string(text: str, quote_index: int) -> bool:
    index = quote_index + 1
    while index < len(text) and text[index].isspace():
        index += 1
    return index >= len(text) or text[index] in {":", ",", "}", "]"}


def _preview(text: str, limit: int = 1200) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...<truncated>"


def _response_format_unsupported(message: str) -> bool:
    lowered = message.lower()
    return "response_format" in lowered and any(
        phrase in lowered
        for phrase in (
            "unsupported",
            "not support",
            "not_supported",
            "unknown parameter",
            "unrecognized",
            "extra inputs are not permitted",
            "invalid parameter",
        )
    )


def _empty_response(message: str) -> bool:
    return EMPTY_RESPONSE_MESSAGE.lower() in message.lower()


def _project_context_messages(
    user_payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = copy.deepcopy(user_payload)
    context = payload.pop("conversation_context", None)
    if not isinstance(context, dict):
        return [], _drop_empty_values(payload)
    messages = context.get("messages", [])
    if not isinstance(messages, list):
        return [], _drop_empty_values(payload)
    projected: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip()
        content = str(message.get("content") or "").strip()
        images = _normalize_image_parts(message.get("images"))
        if role not in {"user", "assistant"} or (not content and not images):
            continue
        if images and role == "user":
            projected.append(
                {
                    "role": role,
                    "content": [
                        {"type": "text", "text": content or "（用户上传了图片附件）"},
                        *images,
                    ],
                }
            )
        else:
            projected.append({"role": role, "content": content})
    current_user_message = str(payload.get("user_message") or "").strip()
    latest_user_message = next(
        (
            _content_text(message.get("content")).strip()
            for message in reversed(projected)
            if message.get("role") == "user"
        ),
        "",
    )
    if current_user_message and current_user_message == latest_user_message:
        payload.pop("user_message", None)
    return projected, _drop_empty_values(payload)


def _prepare_user_input(
    user_payload: dict[str, Any] | str,
) -> tuple[list[dict[str, Any]], str | list[dict[str, Any]]]:
    if isinstance(user_payload, str):
        return [], user_payload.strip()
    if isinstance(user_payload.get(STAGE_PROTOCOL_KEY), dict):
        return _prepare_stage_user_input(user_payload)
    context_messages, projected_payload = _project_context_messages(user_payload)
    return context_messages, json.dumps(projected_payload, ensure_ascii=False)


def _prepare_stage_user_input(
    user_payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], str | list[dict[str, Any]]]:
    context = user_payload.get("conversation_context")
    payload = copy.deepcopy(
        {
            key: value
            for key, value in user_payload.items()
            if key != "conversation_context"
        }
    )
    payload.pop(STAGE_PROTOCOL_KEY, None)
    context_messages = _project_messages_from_context(context)
    user_message = str(payload.pop("user_message", "") or "").strip()
    current_images: list[dict[str, Any]] = []
    for index in range(len(context_messages) - 1, -1, -1):
        message = context_messages[index]
        if message.get("role") != "user":
            continue
        if _content_text(message.get("content")).strip() != user_message:
            break
        content = message.get("content")
        if isinstance(content, list):
            current_images = [
                item
                for item in content
                if isinstance(item, dict) and item.get("type") == "image_url"
            ]
        context_messages.pop(index)
        break

    turn_stage_messages = _project_turn_stage_messages(context)
    context_messages.extend(turn_stage_messages)
    serialized = render_stage_user_message(
        user_payload, include_turn_header=not turn_stage_messages
    )
    if not current_images:
        return context_messages, _CurrentStageText(serialized)
    return context_messages, [
        {"type": "text", "text": serialized},
        *current_images,
    ]


def _project_messages_from_context(context: Any) -> list[dict[str, Any]]:
    if not isinstance(context, dict) or not isinstance(context.get("messages"), list):
        return []
    projected: list[dict[str, Any]] = []
    for message in context["messages"]:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip()
        content = str(message.get("content") or "").strip()
        images = _normalize_image_parts(message.get("images"))
        if role not in {"user", "assistant"} or (not content and not images):
            continue
        if images and role == "user":
            projected.append(
                {
                    "role": role,
                    "content": [
                        {"type": "text", "text": content or "（用户上传了图片附件）"},
                        *images,
                    ],
                }
            )
        else:
            projected.append({"role": role, "content": content})
    return projected


def _project_turn_stage_messages(context: Any) -> list[dict[str, Any]]:
    if not isinstance(context, dict):
        return []
    messages = context.get(TURN_STAGE_MESSAGES_KEY)
    if not isinstance(messages, list):
        return []
    projected: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip()
        content = message.get("content")
        if role not in {"user", "assistant"} or not _content_text(content).strip():
            continue
        projected.append(
            {
                "role": role,
                "content": content,
                TURN_STAGE_MESSAGE_MARKER: True,
            }
        )
    return projected


def _record_stage_exchange(
    context_payload: dict[str, Any] | str,
    assistant_content: str,
    *,
    request_user_content: Any = None,
) -> None:
    if not isinstance(context_payload, dict):
        return
    if not isinstance(context_payload.get(STAGE_PROTOCOL_KEY), dict):
        return
    context = context_payload.get("conversation_context")
    if not isinstance(context, dict):
        return
    turn_messages = context.setdefault(TURN_STAGE_MESSAGES_KEY, [])
    if not isinstance(turn_messages, list):
        return
    content = str(assistant_content or "").strip()
    if not content:
        return
    user_content = request_user_content
    if not _content_text(user_content).strip():
        user_content = render_stage_user_message(
            context_payload,
            include_turn_header=not _project_turn_stage_messages(context),
        )
    turn_messages.extend(
        [
            {"role": "user", "content": copy.deepcopy(user_content)},
            {"role": "assistant", "content": content},
        ]
    )


def _drop_empty_values(value: Any) -> Any:
    if isinstance(value, dict):
        projected = {
            key: _drop_empty_values(item)
            for key, item in value.items()
        }
        return {
            key: item
            for key, item in projected.items()
            if item is not None and item != "" and item != [] and item != {}
        }
    if isinstance(value, list):
        projected = [_drop_empty_values(item) for item in value]
        return [
            item
            for item in projected
            if item is not None and item != "" and item != [] and item != {}
        ]
    return value


def _normalize_image_parts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    parts: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "image_url" and isinstance(item.get("image_url"), dict):
            url = str(item["image_url"].get("url") or "").strip()
            if not url:
                continue
            image_url: dict[str, Any] = {"url": url}
            detail = str(item["image_url"].get("detail") or "").strip()
            if detail:
                image_url["detail"] = detail
            parts.append({"type": "image_url", "image_url": image_url})
    return parts
