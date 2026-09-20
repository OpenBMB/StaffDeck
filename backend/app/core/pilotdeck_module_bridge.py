from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from app.core.harness_agent import HarnessExecutionFenced
from app.core.pilotdeck_agent_loop_client import StaffDeckModuleError
from app.llm import LLMClient

STAFFDECK_RESULT_CARRIER_PREFIX = "__STAFFDECK_TASK_RESULT__="
STAFFDECK_RESULT_CARRIER_SUFFIX = "__END__"
MAX_SUCCESSFUL_KNOWLEDGE_SEARCHES_PER_TASK = 2


class CapabilityInvoker(Protocol):
    def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


def _has_structured_result_fields(action: Mapping[str, Any]) -> bool:
    """Identify host-owned terminal fields without interpreting their meaning."""

    return any(
        key in action
        for key in (
            "status",
            "handoff",
            "slot_updates",
            "next_step_id",
            "structured_result",
            "task_summary",
            "error",
            "retryability",
            "citations",
            "evidence_results",
            "capability_results",
        )
    )


def _unwrap_nested_action(action: dict[str, Any]) -> dict[str, Any]:
    """Decode providers that return a JSON Harness action as reply text.

    StaffDeck's legacy JSON client accepts both a parsed action object and an
    OpenAI message whose content is that JSON object.  Keep the same boundary
    for the sidecar bridge, while leaving ordinary user-facing JSON untouched.
    """

    if action.get("action") != "finish" or not isinstance(action.get("reply_fragment"), str):
        return action
    try:
        nested = json.loads(action["reply_fragment"])
    except (TypeError, json.JSONDecodeError):
        return action
    if not isinstance(nested, dict) or nested.get("action") not in {"finish", "tool"}:
        return action
    merged = dict(nested)
    for key in ("reply_fragment", "task_summary", "structured_result"):
        if key not in merged and key in action:
            merged[key] = action[key]
    return merged


class StaffDeckPilotDeckModuleBridge:
    """Expose StaffDeck-owned modules to the PilotDeck sidecar."""

    def __init__(
        self,
        *,
        model_client: LLMClient,
        capability_invoker: CapabilityInvoker,
        permission_checker: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
        checkpoint_sink: Callable[[dict[str, Any]], Mapping[str, Any] | None] | None = None,
        remaining_actions: int | None = None,
        successful_knowledge_searches: int = 0,
    ) -> None:
        self.model_client = model_client
        self.capability_invoker = capability_invoker
        self.permission_checker = permission_checker
        self.checkpoint_sink = checkpoint_sink
        self._remaining_actions = remaining_actions
        self._successful_knowledge_searches = max(0, successful_knowledge_searches)

    def __call__(self, module: str, payload: dict[str, Any]) -> Mapping[str, Any]:
        if module == "model":
            return self.model(payload)
        if module == "capability":
            return self.capability(payload)
        if module == "permission":
            return self.permission(payload)
        if module == "checkpoint":
            return self.checkpoint(payload)
        raise ValueError(f"Unknown PilotDeck module: {module}")

    def model(self, payload: dict[str, Any]) -> Mapping[str, Any]:
        """Return buffered canonical text events for one model invocation.

        The existing StaffDeck client exposes provider-normalized text chunks;
        keeping the bridge buffered preserves that API while the sidecar owns
        turn/tool-loop semantics. Native tool deltas can be added to the events
        list later without changing the module envelope.
        """

        request = payload.get("request")
        if isinstance(request, dict):
            system_prompt = str(request.get("systemPrompt") or "")
            raw_messages = request.get("messages")
            canonical_messages = self._canonical_messages(raw_messages)
            execution_context = payload.get("context")
            context_metadata = (
                execution_context.get("metadata")
                if isinstance(execution_context, dict)
                and isinstance(execution_context.get("metadata"), dict)
                else {}
            )
            request_metadata = request.get("metadata") if isinstance(request.get("metadata"), dict) else {}
            user_payload: dict[str, Any] | str = {
                "tools": request.get("tools") or [],
                "tool_choice": request.get("toolChoice"),
                "metadata": {**context_metadata, **request_metadata},
            }
            if canonical_messages:
                user_payload["conversation_context"] = {"messages": canonical_messages}
            if isinstance(raw_messages, list):
                # Keep the lossless canonical projection available to providers
                # that understand tool blocks, while conversation_context remains
                # compatible with StaffDeck's existing provider adapters.
                user_payload["canonical_messages"] = raw_messages
            transcript = self._tool_transcript(raw_messages)
            if transcript:
                user_payload["harness_transcript"] = transcript
        else:
            system_prompt = str(payload.get("systemPrompt") or "")
            user_payload = payload.get("userPayload")
            if not isinstance(user_payload, (dict, str)):
                user_payload = str(user_payload or "")
        remaining_for_model: int | None = None
        if self._remaining_actions is not None:
            if self._remaining_actions <= 0:
                raise StaffDeckModuleError(
                    "ACTION_BUDGET_EXHAUSTED",
                    "StaffDeck action budget exhausted before the next model action.",
                )
            remaining_for_model = self._remaining_actions
        if remaining_for_model is not None and isinstance(user_payload, dict):
            metadata = user_payload.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
                user_payload["metadata"] = metadata
            metadata["remainingActions"] = remaining_for_model
        sequence_generator = getattr(self.model_client, "generate_json_sequence", None)
        action = (
            sequence_generator(system_prompt, user_payload)
            if callable(sequence_generator)
            else self.model_client.generate_json(system_prompt, user_payload)
        )
        actions = self._normalize_actions(action)
        events: list[dict[str, Any]] = [{"type": "message_start", "role": "assistant"}]
        emitted_tool = False
        structured_items: list[dict[str, Any]] = []
        for action_item in actions:
            if action_item.get("action") == "tool" and str(action_item.get("tool_name") or "").strip():
                emitted_tool = True
                call_id = str(action_item.get("tool_call_id") or "staffdeck-call")
                events.append({
                    "type": "tool_call_end",
                    "toolCall": {
                        "id": call_id,
                        "name": str(action_item["tool_name"]),
                        "input": action_item.get("arguments") or {},
                    },
                })
                continue
            if _has_structured_result_fields(action_item):
                structured_items.append(action_item)
            raw_text = action_item.get("reply_fragment") or action_item.get("reply")
            text = str(raw_text or "")
            if text:
                events.append({"type": "text_delta", "text": text})
        if structured_items and not emitted_tool:
            # The generic sidecar transports canonical text only.  This opaque
            # carrier lets the StaffDeck client recover its host-owned action
            # envelope without teaching PilotDeck about StaffDeck semantics.
            carrier = structured_items[-1]
            events.append({
                "type": "text_delta",
                "text": (
                    f"{STAFFDECK_RESULT_CARRIER_PREFIX}"
                    f"{json.dumps(carrier, ensure_ascii=False, separators=(',', ':'), default=str)}"
                    f"{STAFFDECK_RESULT_CARRIER_SUFFIX}"
                ),
            })
        events.append({
            "type": "message_end",
            "finishReason": "tool_call" if emitted_tool else "stop",
        })
        return {
            "events": events,
        }
    @staticmethod
    def _normalize_actions(raw: object) -> list[dict[str, Any]]:
        if isinstance(raw, dict) and isinstance(raw.get("actions"), list):
            items = raw["actions"]
        elif isinstance(raw, list):
            items = raw
        else:
            items = [raw]
        actions = [
            _unwrap_nested_action(item)
            for item in items
            if isinstance(item, dict)
        ]
        if not actions:
            raise TypeError("StaffDeck model returned an empty or invalid JSON action.")
        return actions

    @staticmethod
    def _canonical_messages(messages: object) -> list[dict[str, Any]]:
        if not isinstance(messages, list):
            return []
        projected: list[dict[str, Any]] = []
        for raw in messages:
            if not isinstance(raw, dict):
                continue
            role = str(raw.get("role") or "").strip()
            if role not in {"user", "assistant"}:
                continue
            content = raw.get("content")
            text_parts: list[str] = []
            images: list[dict[str, Any]] = []
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    text_parts.append(block["text"])
                elif block.get("type") == "image" and block.get("source") == "base64":
                    mime = str(block.get("mimeType") or "application/octet-stream")
                    data = block.get("data")
                    if isinstance(data, str) and data:
                        images.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime};base64,{data}",
                                **(
                                    {"detail": block["detail"]}
                                    if block.get("detail") in {"auto", "low", "high"}
                                    else {}
                                ),
                            },
                        })
            for image in raw.get("images", []) if isinstance(raw.get("images"), list) else []:
                if isinstance(image, dict) and image.get("type") == "image_url":
                    image_url = image.get("image_url")
                    if isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
                        images.append({"type": "image_url", "image_url": {"url": image_url["url"]}})
            if text_parts or images:
                projected.append({
                    "role": role,
                    "content": "".join(text_parts),
                    **({"images": images} if images else {}),
                })
        return projected

    @staticmethod
    def _tool_transcript(messages: object) -> list[dict[str, Any]]:
        """Project canonical tool blocks into StaffDeck's existing transcript shape.

        StaffDeck's LLM client already exposes ``harness_transcript`` to the
        model as structured execution history. Keeping tool results there lets
        the host model observe success and failure without inventing a second
        provider-specific message format.
        """
        if not isinstance(messages, list):
            return []
        transcript: list[dict[str, Any]] = []
        tool_names_by_id: dict[str, str] = {}
        for raw in messages:
            if not isinstance(raw, dict):
                continue
            for block in raw.get("content", []) if isinstance(raw.get("content"), list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_call":
                    tool_call_id = str(block.get("id") or "")
                    tool_name = str(block.get("name") or "")
                    if tool_call_id and tool_name:
                        tool_names_by_id[tool_call_id] = tool_name
                    transcript.append({
                        "role": "assistant",
                        "action": "tool",
                        "tool_name": tool_name,
                        "tool_call_id": tool_call_id,
                        "arguments": block.get("input") if isinstance(block.get("input"), dict) else {},
                    })
                elif block.get("type") == "tool_result":
                    result_content = block.get("content")
                    data: Any = None
                    error: dict[str, Any] | None = None
                    if isinstance(result_content, list):
                        json_values = [
                            item.get("value")
                            for item in result_content
                            if isinstance(item, dict) and item.get("type") == "json"
                        ]
                        if json_values:
                            data = json_values[0]
                        text_values = [
                            item.get("text")
                            for item in result_content
                            if isinstance(item, dict) and item.get("type") == "text"
                        ]
                        if data is None and text_values:
                            data = "".join(str(value) for value in text_values)
                    if isinstance(data, str):
                        try:
                            parsed_data = json.loads(data)
                        except (TypeError, ValueError):
                            parsed_data = None
                        if parsed_data is not None:
                            data = parsed_data
                    raw_result = block.get("raw")
                    if isinstance(raw_result, dict):
                        if raw_result.get("type") == "error":
                            raw_error = raw_result.get("error")
                            if isinstance(raw_error, dict):
                                error = dict(raw_error)
                                details = raw_error.get("details")
                                if isinstance(details, dict) and details.get("moduleCode"):
                                    error["code"] = details["moduleCode"]
                            data = raw_result.get("data")
                        elif raw_result.get("type") == "success":
                            data = raw_result.get("data", data)
                    tool_call_id = str(block.get("toolCallId") or "")
                    if block.get("isError") and error is None:
                        error = {
                            "code": "TOOL_EXECUTION_FAILED",
                            "message": str(data or "Tool execution failed."),
                            "retryable": False,
                        }
                    raw_tool_name = raw_result.get("toolName") if isinstance(raw_result, dict) else None
                    transcript.append({
                        "role": "tool",
                        "tool_name": str(block.get("toolName") or raw_tool_name or tool_names_by_id.get(tool_call_id) or ""),
                        "tool_call_id": tool_call_id,
                        "result": {
                            "success": not bool(block.get("isError")),
                            "data": data,
                            "error": error,
                        },
                    })
        return transcript

    def capability(self, payload: dict[str, Any]) -> Mapping[str, Any]:
        name = str(payload.get("name") or "").strip()
        tool_call_id = str(payload.get("toolCallId") or "staffdeck-call")
        arguments = payload.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        if (
            name == "knowledge_search"
            and self._successful_knowledge_searches
            >= MAX_SUCCESSFUL_KNOWLEDGE_SEARCHES_PER_TASK
        ):
            raise StaffDeckModuleError(
                "KNOWLEDGE_SEARCH_BUDGET_EXHAUSTED",
                (
                    "当前 TaskFrame 已完成两次有效知识检索。请使用已有证据完成"
                    "原始需求；不要扩展相邻主题或继续改写同义查询。"
                ),
            )
        # Harness action budgets count capability actions, not model requests.
        # Consume the slot at the same boundary as the legacy task agent so a
        # multi-action model response projects the next iteration's budget
        # identically on both execution paths.
        if self._remaining_actions is not None:
            if self._remaining_actions <= 0:
                raise StaffDeckModuleError(
                    "ACTION_BUDGET_EXHAUSTED",
                    "StaffDeck action budget exhausted before capability execution.",
                )
            self._remaining_actions -= 1
        result = self.capability_invoker.invoke(name, arguments)
        result_error = result.get("error") if isinstance(result, dict) else None
        if (
            isinstance(result, dict)
            and (
                result.get("outcome") == "result_unknown"
                or (
                    isinstance(result_error, dict)
                    and str(result_error.get("code") or "") == "RESULT_UNKNOWN"
                )
            )
        ):
            raise HarnessExecutionFenced(
                (
                    str(result_error.get("message") or "")
                    if isinstance(result_error, dict)
                    else ""
                )
                or "Capability result requires reconciliation."
            )
        if isinstance(result, dict) and result.get("type") in {"success", "error"}:
            return result
        if isinstance(result, dict) and result.get("success") is True:
            if name == "knowledge_search" and _has_usable_knowledge_evidence(result):
                self._successful_knowledge_searches += 1
            return {
                "type": "success",
                "toolCallId": tool_call_id,
                "toolName": name,
                "data": result.get("data"),
                "content": [{"type": "json", "value": result.get("data", result)}],
                "startedAt": "1970-01-01T00:00:00.000Z",
                "completedAt": "1970-01-01T00:00:00.000Z",
            }
        error = result.get("error") if isinstance(result, dict) else None
        error_code = (
            str(error.get("code") or "tool_execution_failed")
            if isinstance(error, dict)
            else "tool_execution_failed"
        )
        error_message = (
            str(error.get("message") or "Capability invocation failed.")
            if isinstance(error, dict)
            else "Capability invocation failed."
        )
        retryable = error.get("retryable") if isinstance(error, dict) else None
        return {
            "type": "error",
            "toolCallId": tool_call_id,
            "toolName": name,
            "error": {
                "code": error_code,
                "message": error_message,
                **({"retryable": bool(retryable)} if retryable is not None else {}),
            },
            "content": [{"type": "text", "text": error_message}],
            "startedAt": "1970-01-01T00:00:00.000Z",
            "completedAt": "1970-01-01T00:00:00.000Z",
        }

    def permission(self, payload: dict[str, Any]) -> Mapping[str, Any]:
        if self.permission_checker is None:
            return {
                "allowed": False,
                "source": "staffdeck",
                "error": {
                    "code": "PERMISSION_UNAVAILABLE",
                    "message": "StaffDeck permission checker is not configured.",
                },
            }
        return dict(self.permission_checker(payload))

    def checkpoint(self, payload: dict[str, Any]) -> Mapping[str, Any]:
        if self.checkpoint_sink is None:
            return {"accepted": True}
        result = self.checkpoint_sink(payload)
        return dict(result or {"accepted": True})


def _has_usable_knowledge_evidence(result: Mapping[str, Any]) -> bool:
    data = result.get("data")
    if not isinstance(data, Mapping):
        return False
    evidence = data.get("evidence_pack")
    return isinstance(evidence, list) and any(isinstance(item, Mapping) for item in evidence)


__all__ = ["StaffDeckPilotDeckModuleBridge"]
