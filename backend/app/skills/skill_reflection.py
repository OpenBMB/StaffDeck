from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, TypeVar

from app import paths
from app.llm import LLMClient, LLMError
from app.skills.skill_schema import SkillCard, ToolSuggestion


PROMPT_PATH = paths.resource_dir() / "app" / "llm" / "prompts" / "skill_reflection_prompt.md"
MAX_REFLECTION_ROUNDS = 3
RUBRIC_LABELS: dict[str, str] = {
    "source_alignment": "来源一致性",
    "closed_loop": "闭环能力",
    "adaptive_progression": "自适应推进",
    "tool_grounding": "工具依据",
    "tool_call_format": "工具调用格式",
    "graph_integrity": "图结构完整性",
    "nested_sop_grounding": "子 SOP 依据",
    "capability_policy": "能力可见性与强制执行",
    "side_effect_confirmation": "副作用确认",
    "interruption_and_recovery": "中断恢复",
}
RUBRICS = [
    {
        "name": name,
        "label": label,
    }
    for name, label in RUBRIC_LABELS.items()
]

ResponseT = TypeVar("ResponseT")
StatusCallback = Callable[[str], None]
NormalizeResponse = Callable[[dict[str, Any]], ResponseT]


def reflect_skill_response(
    *,
    client: LLMClient,
    source_kind: str,
    source_payload: dict[str, Any],
    response: ResponseT,
    candidate_skill: SkillCard,
    current_warnings: list[str],
    tool_suggestions: list[ToolSuggestion],
    normalize_response: NormalizeResponse[ResponseT],
    status_callback: StatusCallback | None = None,
) -> ResponseT:
    events = reflect_skill_response_stream(
        client=client,
        source_kind=source_kind,
        source_payload=source_payload,
        response=response,
        candidate_skill=candidate_skill,
        current_warnings=current_warnings,
        tool_suggestions=tool_suggestions,
        normalize_response=normalize_response,
    )
    while True:
        try:
            event = next(events)
            if event.get("event") == "status":
                text = event.get("data", {}).get("text") if isinstance(event.get("data"), dict) else None
                _emit(status_callback, str(text or ""))
        except StopIteration as stop:
            return stop.value


def reflect_skill_response_stream(
    *,
    client: LLMClient,
    source_kind: str,
    source_payload: dict[str, Any],
    response: ResponseT,
    candidate_skill: SkillCard,
    current_warnings: list[str],
    tool_suggestions: list[ToolSuggestion],
    normalize_response: NormalizeResponse[ResponseT],
):
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    reviewed = response
    reviewed_skill = candidate_skill
    warnings = list(current_warnings)
    suggestions = list(tool_suggestions)
    reflection_history: list[dict[str, Any]] = []

    for round_index in range(1, MAX_REFLECTION_ROUNDS + 1):
        yield _status_event(f"正在校验技能结果（{round_index}/{MAX_REFLECTION_ROUNDS}）")
        yield _status_event(
            "校验范围：来源一致性、闭环能力、自适应推进、图结构、子 SOP、"
            "能力策略、工具依据、副作用确认、中断恢复"
        )
        try:
            review = _model_review(
                client,
                prompt,
                {
                    "source_kind": source_kind,
                    "source": source_payload,
                    "candidate_skill": reviewed_skill.model_dump(mode="json"),
                    "current_warnings": warnings,
                    "tool_suggestions": [item.model_dump(mode="json") for item in suggestions],
                    "rubrics": RUBRICS,
                    "reflection_round": round_index,
                    "max_reflection_rounds": MAX_REFLECTION_ROUNDS,
                    "reflection_history": reflection_history,
                },
            )
        except (LLMError, json.JSONDecodeError, TypeError, ValueError) as exc:
            yield _status_event("校验失败，保留当前技能草稿")
            return normalize_response(
                {
                    "draft_skill": reviewed_skill.model_dump(mode="json"),
                    "warnings": [*warnings, f"模型校验未能完成，已保留当前技能草稿：{exc}"],
                    "tool_mentions": [item.model_dump(mode="json") for item in suggestions],
                }
            )

        reflection_history.append(_reflection_history_item(review))
        review_warnings = _warnings_from_review(review, source_kind)
        if review_warnings:
            warnings.extend(review_warnings)

        failed = _failed_rubrics(review)
        if failed:
            for item in failed[:4]:
                yield _status_event(f"校验发现：{_rubric_label(item)} - {_finding_text(item)}")
        summary = str(review.get("summary") or "").strip()
        if summary:
            yield _status_event(f"校验结论：{summary}")

        if bool(review.get("passed")):
            yield _status_event("校验通过，技能草稿满足当前要求")
            return normalize_response(
                {
                    "draft_skill": reviewed_skill.model_dump(mode="json"),
                    "warnings": warnings,
                    "tool_mentions": [
                        *[item.model_dump(mode="json") for item in suggestions],
                        *_list_of_dicts(review.get("tool_mentions")),
                    ],
                }
            )

        revised_skill = review.get("draft_skill")
        if not isinstance(revised_skill, dict):
            yield _status_event("校验未通过，但模型未返回可修正草稿")
            return normalize_response(
                {
                    "draft_skill": reviewed_skill.model_dump(mode="json"),
                    "warnings": [
                        *warnings,
                        "模型校验未通过，但未返回可修正 Skill Card，已保留当前草稿。",
                    ],
                    "tool_mentions": [
                        *[item.model_dump(mode="json") for item in suggestions],
                        *_list_of_dicts(review.get("tool_mentions")),
                    ],
                }
            )

        yield _status_event(f"校验未通过，正在应用第 {round_index} 轮修正")
        reviewed = normalize_response(
            {
                "draft_skill": revised_skill,
                "warnings": warnings,
                "tool_mentions": [
                    *[item.model_dump(mode="json") for item in suggestions],
                    *_list_of_dicts(review.get("tool_mentions")),
                ],
            }
        )
        reviewed_skill = getattr(reviewed, "draft_skill")
        warnings = list(getattr(reviewed, "warnings", warnings))
        suggestions = list(getattr(reviewed, "tool_suggestions", suggestions))

    yield _status_event("校验达到上限，保留最后一版技能草稿")
    return normalize_response(
        {
            "draft_skill": reviewed_skill.model_dump(mode="json"),
            "warnings": [*warnings, f"模型校验已达到 {MAX_REFLECTION_ROUNDS} 轮上限，保留最后一版技能草稿。"],
            "tool_mentions": [item.model_dump(mode="json") for item in suggestions],
        }
    )


def _model_review(client: LLMClient, prompt: str, payload: dict[str, Any]) -> dict[str, Any]:
    text = client.generate_text(prompt, payload)
    raw = json.loads(_extract_json(text))
    if not isinstance(raw, dict):
        raise ValueError("反思模型输出不是 JSON object")
    return raw


def _warnings_from_review(review: dict[str, Any], source_kind: str) -> list[str]:
    warnings: list[str] = []
    for item in _string_list(review.get("source_warnings")):
        warnings.append(f"{_source_label(source_kind)}本身可能存在问题：{item}")
    for item in _string_list(review.get("warnings")):
        warnings.append(item)
    for item in _failed_rubrics(review):
        origin = str(item.get("origin") or "").strip()
        if origin != "source_input":
            continue
        finding = _finding_text(item)
        if finding:
            warnings.append(f"{_source_label(source_kind)}本身可能存在问题：{_rubric_label(item)} - {finding}")
    return _dedupe(warnings)


def _failed_rubrics(review: dict[str, Any]) -> list[dict[str, Any]]:
    results = review.get("rubric_results")
    if not isinstance(results, list):
        return []
    return [item for item in results if isinstance(item, dict) and not bool(item.get("passed"))]


def _reflection_history_item(review: dict[str, Any]) -> dict[str, Any]:
    return {
        "passed": bool(review.get("passed")),
        "summary": str(review.get("summary") or ""),
        "failed_rubrics": [
            {
                "name": str(item.get("name") or ""),
                "finding": _finding_text(item),
                "origin": str(item.get("origin") or ""),
            }
            for item in _failed_rubrics(review)
        ],
    }


def _source_label(source_kind: str) -> str:
    if source_kind == "rewrite":
        return "原始技能"
    return "原始文档"


def _rubric_label(item: dict[str, Any]) -> str:
    name = str(item.get("name") or "")
    return RUBRIC_LABELS.get(name, name or "未知 Rubric")


def _finding_text(item: dict[str, Any]) -> str:
    return str(item.get("finding") or item.get("issue") or "").strip()


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _dedupe(values: list[str]) -> list[str]:
    deduped: list[str] = []
    for value in values:
        text = value.strip()
        if text and text not in deduped:
            deduped.append(text)
    return deduped


def _emit(status_callback: StatusCallback | None, text: str) -> None:
    if status_callback is not None:
        status_callback(text)


def _status_event(text: str) -> dict[str, object]:
    return {"event": "status", "data": {"text": text}}


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
