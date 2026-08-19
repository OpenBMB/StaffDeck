from __future__ import annotations

import json
import re
from time import sleep
from typing import Any, Iterator

from app import paths
from app.db.models import ModelConfig
from app.llm import LLMClient, LLMError
from app.skills.llm_limits import skill_model_config
from app.skills.nesting import validate_sop_nesting
from app.skills.skill_reflection import reflect_skill_response, reflect_skill_response_stream
from app.skills.skill_schema import SkillCard, SkillRewriteRequest, SkillRewriteResponse
from app.skills.skill_distiller import (
    _compact_warnings,
    _normalize_tool_suggestions,
    _remove_unknown_tool_actions,
    _tool_action_names_from_suggestions,
    _tool_resolution_warnings,
)
from app.skills.step_ids import skill_card_with_unique_step_ids


PROMPT_PATH = paths.resource_dir() / "app" / "llm" / "prompts" / "skill_editor_prompt.md"
STREAM_INTERVAL_SECONDS = 0.035
BASIC_FIELDS = {
    "name",
    "version",
    "business_domain",
    "description",
    "capability_scope",
    "step_timeout_seconds",
    "trigger_intents",
    "user_utterance_examples",
    "goal",
    "required_info",
    "slot_filling_policy",
    "interruption_policy",
    "response_rules",
}
NODE_FIELDS = {
    "node_id",
    "type",
    "name",
    "instruction",
    "optional",
    "condition",
    "expected_user_info",
    "allowed_actions",
    "knowledge_scope",
    "retry_policy",
    "metadata",
    "sub_sop_id",
    "capability_refs",
}
GRAPH_FIELDS = {"edges", "start_node_id", "terminal_node_ids"}


class SkillEditor:
    def rewrite(self, request: SkillRewriteRequest, model_config: ModelConfig) -> SkillRewriteResponse:
        client = LLMClient(skill_model_config(model_config))
        payload = self._payload(request)
        raw = client.generate_json(PROMPT_PATH.read_text(encoding="utf-8"), payload)
        response = self._normalize_response(raw, request)
        return reflect_skill_response(
            client=client,
            source_kind="rewrite",
            source_payload=payload,
            response=response,
            candidate_skill=response.draft_skill,
            current_warnings=response.warnings,
            tool_suggestions=response.tool_suggestions,
            normalize_response=lambda review_raw: self._normalize_response(review_raw, request),
        )

    def stream_text(
        self, request: SkillRewriteRequest, model_config: ModelConfig
    ) -> Iterator[dict[str, object]]:
        chunks: list[str] = []
        prompt = PROMPT_PATH.read_text(encoding="utf-8")
        payload = self._payload(request)
        client = LLMClient(skill_model_config(model_config))
        try:
            yield {"event": "status", "data": {"text": "模型正在分析改写范围"}}
            for chunk in client.generate_text_stream(prompt, payload):
                chunks.append(chunk)
            yield {"event": "status", "data": {"text": "正在校验局部改写结果"}}
            response = self._response_from_text("".join(chunks), request)
        except (LLMError, json.JSONDecodeError, TypeError, ValueError) as exc:
            try:
                yield {"event": "status", "data": {"text": "模型输出需要修复，正在重试一次"}}
                repair_text = client.generate_text(
                    prompt,
                    {
                        **payload,
                        "previous_output": "".join(chunks),
                        "previous_error": str(exc),
                        "repair_instruction": (
                            "请基于 current_skill、instruction 和 target_paths 修复上一次输出。"
                            "只输出合法 JSON，可以使用 patches 做局部修改，或返回完整 draft_skill。"
                        ),
                    },
                )
                response = self._response_from_text(repair_text, request)
            except (LLMError, json.JSONDecodeError, TypeError, ValueError) as repair_exc:
                yield {"event": "status", "data": {"text": "模型改写失败，正在保留原版本"}}
                response = SkillRewriteResponse(
                    draft_skill=request.current_skill,
                    assistant_message="改写失败，已保留当前技能内容。",
                    changed_paths=[],
                    warnings=[f"模型未能完成局部改写：{repair_exc}"],
                )
        yield {"event": "status", "data": {"text": "正在校验改写范围与工具接入"}}
        response = yield from reflect_skill_response_stream(
            client=client,
            source_kind="rewrite",
            source_payload=payload,
            response=response,
            candidate_skill=response.draft_skill,
            current_warnings=response.warnings,
            tool_suggestions=response.tool_suggestions,
            normalize_response=lambda review_raw: self._normalize_response(review_raw, request),
        )
        yield {"event": "status", "data": {"text": "正在整理校验后的改写结果"}}
        for chunk in _chunk_text(response.assistant_message):
            yield {"event": "message_chunk", "data": {"content": chunk}}
            sleep(STREAM_INTERVAL_SECONDS)
        yield {"event": "complete", "data": response.model_dump(mode="json")}

    def _response_from_text(self, text: str, request: SkillRewriteRequest) -> SkillRewriteResponse:
        raw = json.loads(_extract_json(text))
        if not isinstance(raw, dict):
            raise ValueError("模型输出不是 JSON object")
        return self._normalize_response(raw, request)

    def _payload(self, request: SkillRewriteRequest) -> dict[str, Any]:
        return {
            "current_skill": request.current_skill.model_dump(mode="json"),
            "instruction": request.instruction,
            "target_path": request.target_path,
            "target_paths": _target_paths(request),
            "target_label": request.target_label,
            "conversation": request.conversation[-12:],
            "available_tools": request.available_tools,
            "available_sops": [
                {
                    key: item.get(key)
                    for key in (
                        "skill_id",
                        "name",
                        "description",
                        "capability_scope",
                        "status",
                        "nested_sop_ids",
                        "selectable",
                        "unavailable_reason",
                    )
                    if key in item
                }
                for item in request.available_sops
                if isinstance(item, dict)
            ],
        }

    def _normalize_response(
        self, raw: dict[str, Any], request: SkillRewriteRequest
    ) -> SkillRewriteResponse:
        target_paths = _target_paths(request)
        patched = _skill_from_patches(raw, request, target_paths)
        draft = (
            patched.model_dump(mode="json")
            if patched is not None
            else raw.get("draft_skill")
            if isinstance(raw.get("draft_skill"), dict)
            else raw
        )
        candidate = SkillCard.model_validate(draft)
        merged = _merge_targets(
            request.current_skill,
            candidate,
            target_paths,
            allow_graph_changes=_response_requests_graph_change(raw, target_paths),
        )
        merged_data = merged.model_dump(mode="json")
        raw_tool_mentions = raw.get("tool_mentions") if isinstance(raw.get("tool_mentions"), list) else raw.get("tool_suggestions")
        tool_resolutions = _normalize_tool_suggestions(raw_tool_mentions, request, [])
        nodes, missing_tool_names = _remove_unknown_tool_actions(
            [node for node in merged_data.get("nodes", []) if isinstance(node, dict)],
            request.available_tools,
            _tool_action_names_from_suggestions(tool_resolutions),
        )
        if nodes:
            merged_data["nodes"] = nodes
            merged = SkillCard.model_validate(merged_data)
        merged, id_warnings = skill_card_with_unique_step_ids(merged)
        validate_sop_nesting(
            merged.skill_id,
            merged.model_dump(mode="json"),
            request.available_sops,
        )
        assistant_message = str(raw.get("assistant_message") or "已完成选中部分的改写。").strip()
        warnings = [str(item) for item in raw.get("warnings", []) if str(item).strip()]
        warnings.extend(warning for warning in id_warnings if warning not in warnings)
        for tool_name in missing_tool_names:
            warning = (
                f"改写结果引用了未配置工具 {tool_name}，已移出 allowed_actions；"
                "如确需该工具，模型必须在 tool_mentions 中提供来自上下文的完整工具提及。"
            )
            if warning not in warnings:
                warnings.append(warning)
        warnings = _compact_warnings(warnings)
        reported_paths = [
            str(item) for item in raw.get("changed_paths", []) if str(item).strip()
        ]
        detected_paths = (
            _changed_paths(request.current_skill, merged)
            if merged.model_dump() != request.current_skill.model_dump()
            else []
        )
        changed_paths = list(dict.fromkeys([*reported_paths, *detected_paths]))
        if missing_tool_names:
            tool_resolutions = _normalize_tool_suggestions(raw_tool_mentions, request, missing_tool_names)
        warnings = _compact_warnings([*warnings, *_tool_resolution_warnings(tool_resolutions)])
        tool_suggestions = [
            item for item in tool_resolutions if item.resolution_status in {"existing", "new_candidate"}
        ]
        return SkillRewriteResponse(
            draft_skill=merged,
            assistant_message=assistant_message,
            changed_paths=changed_paths,
            warnings=warnings,
            tool_suggestions=tool_suggestions,
        )


def _target_paths(request: SkillRewriteRequest) -> list[str]:
    paths = [path.strip() for path in request.target_paths if path.strip()]
    if not paths:
        paths = [request.target_path.strip() or "all"]
    if "all" in paths:
        return ["all"]
    deduped: list[str] = []
    for path in paths:
        if path not in deduped:
            deduped.append(path)
    return deduped or ["all"]


def _skill_from_patches(
    raw: dict[str, Any],
    request: SkillRewriteRequest,
    target_paths: list[str],
) -> SkillCard | None:
    patches = raw.get("patches")
    if not isinstance(patches, list):
        return None
    data = request.current_skill.model_dump(mode="json")
    applied = False
    ignored_paths: list[str] = []
    for item in patches:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        if not _patch_allowed(data, path, target_paths):
            ignored_paths.append(path)
            continue
        if _apply_patch(data, path, item.get("value")):
            applied = True
    if ignored_paths:
        warnings = raw.get("warnings")
        if not isinstance(warnings, list):
            warnings = []
            raw["warnings"] = warnings
        warnings.append(f"已忽略越界改写路径：{', '.join(ignored_paths)}")
    if not applied:
        return None
    return SkillCard.model_validate(data)


def _patch_allowed(data: dict[str, Any], path: str, target_paths: list[str]) -> bool:
    if "all" in target_paths:
        return _patch_path_is_known(data, path)
    if _basic_patch_field(path):
        return "basic" in target_paths
    if path in GRAPH_FIELDS:
        return "graph" in target_paths or any(
            _is_node_target(target) for target in target_paths
        )
    if path == "nodes":
        return any(_is_node_target(target) for target in target_paths)
    node_index = _patch_node_index(data, path)
    if node_index is None:
        return False
    nodes = [node for node in data.get("nodes", []) if isinstance(node, dict)]
    node_id = str(nodes[node_index].get("node_id") or "")
    return f"nodes[{node_index}]" in target_paths or f"nodes.{node_id}" in target_paths


def _patch_path_is_known(data: dict[str, Any], path: str) -> bool:
    return (
        bool(_basic_patch_field(path))
        or path in {*GRAPH_FIELDS, "nodes"}
        or _patch_node_index(data, path) is not None
    )


def _apply_patch(data: dict[str, Any], path: str, value: Any) -> bool:
    basic_field = _basic_patch_field(path)
    if basic_field:
        data[basic_field] = value
        return True
    if path == "nodes" and isinstance(value, list):
        data["nodes"] = value
        return True
    if path == "edges" and isinstance(value, list):
        data["edges"] = value
        return True
    if path == "start_node_id" and isinstance(value, str):
        data["start_node_id"] = value
        return True
    if path == "terminal_node_ids" and isinstance(value, list):
        data["terminal_node_ids"] = value
        return True
    node_index = _patch_node_index(data, path)
    if node_index is None:
        return False
    node_field = _patch_node_field(path)
    nodes = [node for node in data.get("nodes", []) if isinstance(node, dict)]
    if not (0 <= node_index < len(nodes)):
        return False
    if node_field is None:
        if not isinstance(value, dict):
            return False
        nodes[node_index] = value
    else:
        nodes[node_index][node_field] = value
    data["nodes"] = nodes
    return True


def _basic_patch_field(path: str) -> str | None:
    normalized = path.removeprefix("basic.")
    return normalized if normalized in BASIC_FIELDS else None


def _patch_node_index(data: dict[str, Any], path: str) -> int | None:
    nodes = [node for node in data.get("nodes", []) if isinstance(node, dict)]
    bracket_match = re.fullmatch(r"nodes\[(\d+)\](?:\.[A-Za-z_][A-Za-z0-9_]*)?", path)
    if bracket_match:
        index = int(bracket_match.group(1))
        return index if 0 <= index < len(nodes) else None
    dot_match = re.fullmatch(r"nodes\.([^.]+)(?:\.[A-Za-z_][A-Za-z0-9_]*)?", path)
    if not dot_match:
        return None
    node_id = dot_match.group(1)
    return next((index for index, node in enumerate(nodes) if str(node.get("node_id") or "") == node_id), None)


def _patch_node_field(path: str) -> str | None:
    bracket_match = re.fullmatch(r"nodes\[\d+\]\.([A-Za-z_][A-Za-z0-9_]*)", path)
    dot_match = re.fullmatch(r"nodes\.[^.]+\.([A-Za-z_][A-Za-z0-9_]*)", path)
    field = bracket_match.group(1) if bracket_match else dot_match.group(1) if dot_match else None
    return field if field in NODE_FIELDS else None


def _merge_targets(
    current: SkillCard,
    candidate: SkillCard,
    target_paths: list[str],
    *,
    allow_graph_changes: bool = False,
) -> SkillCard:
    if "all" in target_paths:
        return candidate
    if _has_node_structure_change(current, candidate, target_paths):
        current_data = current.model_dump(mode="json")
        candidate_data = candidate.model_dump(mode="json")
        current_data["nodes"] = [
            node for node in candidate_data.get("nodes", []) if isinstance(node, dict)
        ]
        current_data["edges"] = [edge for edge in candidate_data.get("edges", []) if isinstance(edge, dict)]
        current_data["start_node_id"] = candidate_data.get("start_node_id") or current_data.get("start_node_id")
        current_data["terminal_node_ids"] = candidate_data.get("terminal_node_ids") or current_data.get("terminal_node_ids")
        if "basic" in target_paths:
            for field in BASIC_FIELDS:
                if field in candidate_data:
                    current_data[field] = candidate_data[field]
        return SkillCard.model_validate(current_data)
    merged = current
    for path in target_paths:
        merged = _merge_target(merged, candidate, path)
    if allow_graph_changes:
        merged = _merge_graph_topology(merged, candidate)
    return merged


def _has_node_structure_change(current: SkillCard, candidate: SkillCard, target_paths: list[str]) -> bool:
    if not any(_is_node_target(path) for path in target_paths):
        return False
    current_nodes = [node for node in current.model_dump(mode="json").get("nodes", []) if isinstance(node, dict)]
    candidate_nodes = [node for node in candidate.model_dump(mode="json").get("nodes", []) if isinstance(node, dict)]
    if len(candidate_nodes) != len(current_nodes):
        return True
    current_ids = [str(node.get("node_id") or "") for node in current_nodes]
    candidate_ids = [str(node.get("node_id") or "") for node in candidate_nodes]
    return sorted(current_ids) == sorted(candidate_ids) and current_ids != candidate_ids


def _is_node_target(path: str) -> bool:
    return path.startswith("nodes.") or path.startswith("nodes[")


def _response_requests_graph_change(raw: dict[str, Any], target_paths: list[str]) -> bool:
    if "all" in target_paths or "graph" in target_paths:
        return True
    if "graph" in [str(item) for item in raw.get("changed_paths", [])]:
        return True
    patches = raw.get("patches")
    return isinstance(patches, list) and any(
        isinstance(item, dict) and str(item.get("path") or "") in GRAPH_FIELDS
        for item in patches
    )


def _merge_graph_topology(current: SkillCard, candidate: SkillCard) -> SkillCard:
    current_data = current.model_dump(mode="json")
    candidate_data = candidate.model_dump(mode="json")
    if not any(current_data.get(field) != candidate_data.get(field) for field in GRAPH_FIELDS):
        return current
    for field in GRAPH_FIELDS:
        current_data[field] = candidate_data.get(field)
    return SkillCard.model_validate(current_data)


def _merge_target(current: SkillCard, candidate: SkillCard, target_path: str) -> SkillCard:
    normalized_path = target_path.strip() or "all"
    if normalized_path == "all":
        return candidate

    current_data = current.model_dump(mode="json")
    candidate_data = candidate.model_dump(mode="json")
    if normalized_path == "basic":
        for field in BASIC_FIELDS:
            if field in candidate_data:
                current_data[field] = candidate_data[field]
        return SkillCard.model_validate(current_data)

    target_index = _node_target_index(current_data, normalized_path)
    if target_index is not None:
        candidate_nodes = [node for node in candidate_data.get("nodes", []) if isinstance(node, dict)]
        current_nodes = [node for node in current_data.get("nodes", []) if isinstance(node, dict)]
        replacement = _replacement_node(
            candidate_nodes,
            current_nodes[target_index],
            target_index,
            prefer_index=normalized_path.startswith("nodes["),
        )
        if isinstance(replacement, dict):
            next_node = dict(current_nodes[target_index])
            previous_node_id = str(next_node.get("node_id") or "")
            for field in NODE_FIELDS:
                if field in replacement:
                    next_node[field] = replacement[field]
            current_nodes[target_index] = next_node
            current_data["nodes"] = current_nodes
            next_node_id = str(next_node.get("node_id") or "")
            if previous_node_id and next_node_id and previous_node_id != next_node_id:
                _replace_node_reference(current_data, previous_node_id, next_node_id)
            return SkillCard.model_validate(current_data)

    return current


def _replace_node_reference(data: dict[str, Any], old_node_id: str, new_node_id: str) -> None:
    if data.get("start_node_id") == old_node_id:
        data["start_node_id"] = new_node_id
    data["terminal_node_ids"] = [
        new_node_id if node_id == old_node_id else node_id
        for node_id in data.get("terminal_node_ids", [])
    ]
    for edge in data.get("edges", []):
        if not isinstance(edge, dict):
            continue
        if edge.get("source_node_id") == old_node_id:
            edge["source_node_id"] = new_node_id
        if edge.get("next_node_id") == old_node_id:
            edge["next_node_id"] = new_node_id


def _changed_paths(previous: SkillCard, next_skill: SkillCard) -> list[str]:
    previous_data = previous.model_dump(mode="json")
    next_data = next_skill.model_dump(mode="json")
    changed: list[str] = []
    if any(previous_data.get(field) != next_data.get(field) for field in BASIC_FIELDS):
        changed.append("basic")
    if any(previous_data.get(field) != next_data.get(field) for field in GRAPH_FIELDS):
        changed.append("graph")
    previous_nodes = [node for node in previous_data.get("nodes", []) if isinstance(node, dict)]
    next_nodes = [node for node in next_data.get("nodes", []) if isinstance(node, dict)]
    for index in range(max(len(previous_nodes), len(next_nodes))):
        previous_node = previous_nodes[index] if index < len(previous_nodes) else None
        next_node = next_nodes[index] if index < len(next_nodes) else None
        if previous_node != next_node:
            changed.append(f"nodes[{index}]")
    return changed


def _node_target_index(current_data: dict[str, Any], path: str) -> int | None:
    current_nodes = [node for node in current_data.get("nodes", []) if isinstance(node, dict)]
    bracket_match = re.fullmatch(r"nodes\[(\d+)\]", path)
    if bracket_match:
        index = int(bracket_match.group(1))
        return index if 0 <= index < len(current_nodes) else None
    if path.startswith("nodes."):
        node_id = path.split(".", 1)[1]
        return next(
            (index for index, node in enumerate(current_nodes) if node.get("node_id") == node_id),
            None,
        )
    return None


def _replacement_node(
    candidate_nodes: list[dict[str, Any]],
    current_node: dict[str, Any],
    target_index: int,
    prefer_index: bool = False,
) -> dict[str, Any] | None:
    if prefer_index and target_index < len(candidate_nodes):
        return candidate_nodes[target_index]
    node_id = str(current_node.get("node_id") or "")
    if node_id:
        matching_nodes = [node for node in candidate_nodes if str(node.get("node_id") or "") == node_id]
        if len(matching_nodes) == 1:
            return matching_nodes[0]
    if target_index < len(candidate_nodes):
        return candidate_nodes[target_index]
    return None


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


def _chunk_text(text: str, size: int = 12) -> Iterator[str]:
    for index in range(0, len(text), size):
        yield text[index : index + size]
