from __future__ import annotations

import json
from pathlib import Path
import re
from time import sleep
from typing import Any, Iterator

from app.db.models import ModelConfig
from app.llm import LLMClient, LLMError
from app.skills.skill_schema import SkillCard, SkillRewriteRequest, SkillRewriteResponse
from app.skills.step_ids import skill_card_with_unique_step_ids


PROMPT_PATH = Path(__file__).resolve().parents[1] / "llm" / "prompts" / "skill_editor_prompt.md"
STREAM_INTERVAL_SECONDS = 0.035
BASIC_FIELDS = {
    "name",
    "version",
    "business_domain",
    "description",
    "trigger_intents",
    "user_utterance_examples",
    "goal",
    "required_info",
    "slot_filling_policy",
    "interruption_policy",
    "response_rules",
}
STEP_FIELDS = {"step_id", "name", "instruction", "expected_user_info", "allowed_actions"}


class SkillEditor:
    def rewrite(self, request: SkillRewriteRequest, model_config: ModelConfig) -> SkillRewriteResponse:
        raw = LLMClient(model_config).generate_json(PROMPT_PATH.read_text(encoding="utf-8"), self._payload(request))
        return self._normalize_response(raw, request)

    def stream_text(
        self, request: SkillRewriteRequest, model_config: ModelConfig
    ) -> Iterator[dict[str, object]]:
        chunks: list[str] = []
        try:
            yield {"event": "status", "data": {"text": "模型正在分析改写范围"}}
            for chunk in LLMClient(model_config).generate_text_stream(
                PROMPT_PATH.read_text(encoding="utf-8"), self._payload(request)
            ):
                chunks.append(chunk)
            yield {"event": "status", "data": {"text": "正在校验局部改写结果"}}
            raw = json.loads(_extract_json("".join(chunks)))
            response = self._normalize_response(raw, request)
        except (LLMError, json.JSONDecodeError, ValueError) as exc:
            yield {"event": "status", "data": {"text": "模型改写失败，正在保留原版本"}}
            response = SkillRewriteResponse(
                draft_skill=request.current_skill,
                assistant_message="改写失败，已保留当前技能内容。",
                changed_paths=[],
                warnings=[f"模型未能完成局部改写：{exc}"],
            )
        for chunk in _chunk_text(response.assistant_message):
            yield {"event": "message_chunk", "data": {"content": chunk}}
            sleep(STREAM_INTERVAL_SECONDS)
        yield {"event": "complete", "data": response.model_dump(mode="json")}

    def _payload(self, request: SkillRewriteRequest) -> dict[str, Any]:
        return {
            "current_skill": request.current_skill.model_dump(mode="json"),
            "instruction": request.instruction,
            "target_path": request.target_path,
            "target_paths": _target_paths(request),
            "target_label": request.target_label,
            "conversation": request.conversation[-12:],
        }

    def _normalize_response(
        self, raw: dict[str, Any], request: SkillRewriteRequest
    ) -> SkillRewriteResponse:
        draft = raw.get("draft_skill") if isinstance(raw.get("draft_skill"), dict) else raw
        candidate, id_warnings = skill_card_with_unique_step_ids(SkillCard.model_validate(draft))
        target_paths = _target_paths(request)
        merged = _merge_targets(request.current_skill, candidate, target_paths)
        assistant_message = str(raw.get("assistant_message") or "已完成选中部分的改写。").strip()
        warnings = [str(item) for item in raw.get("warnings", []) if str(item).strip()]
        warnings.extend(warning for warning in id_warnings if warning not in warnings)
        changed_paths = [str(item) for item in raw.get("changed_paths", []) if str(item).strip()]
        if not changed_paths and merged.model_dump() != request.current_skill.model_dump():
            changed_paths = target_paths
        return SkillRewriteResponse(
            draft_skill=merged,
            assistant_message=assistant_message,
            changed_paths=changed_paths,
            warnings=warnings,
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


def _merge_targets(current: SkillCard, candidate: SkillCard, target_paths: list[str]) -> SkillCard:
    if "all" in target_paths:
        return candidate
    merged = current
    for path in target_paths:
        merged = _merge_target(merged, candidate, path)
    return merged


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

    target_index = _step_target_index(current_data, normalized_path)
    if target_index is not None:
        candidate_steps = [step for step in candidate_data.get("steps", []) if isinstance(step, dict)]
        current_steps = [step for step in current_data.get("steps", []) if isinstance(step, dict)]
        replacement = _replacement_step(
            candidate_steps,
            current_steps[target_index],
            target_index,
            prefer_index=normalized_path.startswith("steps["),
        )
        if isinstance(replacement, dict):
            next_step = dict(current_steps[target_index])
            for field in STEP_FIELDS:
                if field in replacement:
                    next_step[field] = replacement[field]
            current_steps[target_index] = next_step
            current_data["steps"] = current_steps
            return SkillCard.model_validate(current_data)

    return current


def _step_target_index(current_data: dict[str, Any], path: str) -> int | None:
    current_steps = [step for step in current_data.get("steps", []) if isinstance(step, dict)]
    bracket_match = re.fullmatch(r"steps\[(\d+)\]", path)
    if bracket_match:
        index = int(bracket_match.group(1))
        return index if 0 <= index < len(current_steps) else None
    if path.startswith("steps."):
        step_id = path.split(".", 1)[1]
        return next(
            (index for index, step in enumerate(current_steps) if step.get("step_id") == step_id),
            None,
        )
    return None


def _replacement_step(
    candidate_steps: list[dict[str, Any]],
    current_step: dict[str, Any],
    target_index: int,
    prefer_index: bool = False,
) -> dict[str, Any] | None:
    if prefer_index and target_index < len(candidate_steps):
        return candidate_steps[target_index]
    step_id = str(current_step.get("step_id") or "")
    if step_id:
        matching_steps = [step for step in candidate_steps if str(step.get("step_id") or "") == step_id]
        if len(matching_steps) == 1:
            return matching_steps[0]
    if target_index < len(candidate_steps):
        return candidate_steps[target_index]
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
