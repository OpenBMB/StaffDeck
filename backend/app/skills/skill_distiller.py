from __future__ import annotations

import hashlib
import json
from pathlib import Path
from time import sleep
from typing import Any

from app.db.models import ModelConfig
from app.llm import LLMClient, LLMError
from app.skills.skill_schema import SkillDistillRequest, SkillDistillResponse, SkillCard, SkillStep
from app.skills.step_ids import ensure_unique_step_ids, skill_card_with_unique_step_ids


PROMPT_PATH = Path(__file__).resolve().parents[1] / "llm" / "prompts" / "skill_distiller_prompt.md"
STREAM_INTERVAL_SECONDS = 0.035
CLOSED_LOOP_RESPONSE_RULE = (
    "流程必须形成闭环：不得把“请稍候/正在处理/稍后反馈”作为最终回复；"
    "需要查询、核实、创建或处理时必须调用已配置工具或转人工，并向用户给出明确结果。"
)
ADAPTIVE_FLOW_RESPONSE_RULE = (
    "步骤是可自适应推进的目标，不是固定问答脚本；已由当前用户消息、历史信息或路由意图满足的内容"
    "不得重复追问，应直接推进到下一缺失信息、工具调用或最终回复。"
)
CONFIRMATION_FLOW_RESPONSE_RULE = (
    "涉及购买、下单、创建订单、退款、退货、取消订单或提交申请等动作时，"
    "调用工具或执行处理前必须先让用户确认关键对象和操作内容。"
)
TOOL_PROCESS_KEYWORDS = (
    "查询",
    "核实",
    "生成",
    "创建",
    "购买",
    "下单",
    "提交",
    "办理",
    "处理",
)
CONFIRMATION_PROCESS_KEYWORDS = (
    "购买",
    "下单",
    "创建订单",
    "退款",
    "退货",
    "取消订单",
    "提交申请",
    "提交",
    "办理",
)
TOOL_STEP_INSTRUCTION_SUFFIX = (
    "工具参数满足时直接调用工具；工具成功后必须基于工具结果进入最终回复，"
    "不要停留在“请稍候”或“正在处理”。"
)
ADAPTIVE_STEP_INSTRUCTION_SUFFIX = (
    "将本步骤作为目标而不是固定话术；如果用户当前消息、历史 slots 或路由意图已满足本步骤，"
    "直接写入对应 slot 并继续到下一缺失信息、工具调用或最终回复，不要重复确认。"
)
NUMERIC_EXTRACTION_INSTRUCTION = (
    "数值字段需要理解口语数字和量词表达，例如“一个/一件/一台/一次”表示 1，"
    "“两个/两件”表示 2，“三份/3个”表示 3。"
)


class SkillDistiller:
    def distill(self, request: SkillDistillRequest, model_config: ModelConfig) -> SkillDistillResponse:
        payload = self._payload(request)
        raw = LLMClient(model_config).generate_json(PROMPT_PATH.read_text(encoding="utf-8"), payload)
        return self._normalize_response(raw, request)

    def distill_stream(self, request: SkillDistillRequest, model_config: ModelConfig) -> SkillDistillResponse:
        payload = {
            "title": request.title,
            "business_domain": request.business_domain,
            "raw_content": request.raw_content,
            "available_tools": request.available_tools,
        }
        text = "".join(LLMClient(model_config).generate_text_stream(PROMPT_PATH.read_text(encoding="utf-8"), payload))
        return self._normalize_response(json.loads(_extract_json(text)), request)

    def stream_text(self, request: SkillDistillRequest, model_config: ModelConfig):
        payload = self._payload(request)
        chunks: list[str] = []
        try:
            yield {"event": "status", "data": {"text": "模型正在规划技能结构"}}
            for chunk in LLMClient(model_config).generate_text_stream(PROMPT_PATH.read_text(encoding="utf-8"), payload):
                chunks.append(chunk)
                yield {"event": "chunk", "data": {"content": chunk}}
            yield {"event": "status", "data": {"text": "正在校验模型输出结构"}}
            response = self._normalize_response(json.loads(_extract_json("".join(chunks))), request)
        except (LLMError, json.JSONDecodeError, ValueError) as exc:
            yield {"event": "status", "data": {"text": "模型输出需修复，正在生成可用草稿"}}
            response = self._fallback_response(request, f"模型输出未能直接解析，已使用规则兜底生成：{exc}")
            for chunk in _chunk_text(json.dumps(response.draft_skill.model_dump(), ensure_ascii=False, indent=2)):
                yield {"event": "chunk", "data": {"content": chunk}}
                sleep(STREAM_INTERVAL_SECONDS)
        yield {"event": "status", "data": {"text": "已完成 Skill Card 结构化"}}
        yield {"event": "complete", "data": response.model_dump(mode="json")}

    def _payload(self, request: SkillDistillRequest) -> dict[str, Any]:
        return {
            "title": request.title,
            "business_domain": request.business_domain,
            "raw_content": request.raw_content,
            "available_tools": request.available_tools,
        }

    def _normalize_response(self, raw: dict[str, Any], request: SkillDistillRequest) -> SkillDistillResponse:
        draft = raw.get("draft_skill") if isinstance(raw.get("draft_skill"), dict) else raw
        warnings = list(raw.get("warnings") or [])
        fallback = self._fallback_card(request)

        required_info = _string_list(draft.get("required_info"), fallback.required_info)
        steps = self._normalize_steps(draft.get("steps"), fallback.steps)
        steps, step_warnings = self._ensure_closed_loop_steps(steps, request)
        warnings.extend(step_warnings)
        steps, unique_step_warnings = ensure_unique_step_ids(steps)
        warnings.extend(unique_step_warnings)
        response_rules = _string_list(draft.get("response_rules"), fallback.response_rules)
        if CLOSED_LOOP_RESPONSE_RULE not in response_rules:
            response_rules.append(CLOSED_LOOP_RESPONSE_RULE)
        if ADAPTIVE_FLOW_RESPONSE_RULE not in response_rules:
            response_rules.append(ADAPTIVE_FLOW_RESPONSE_RULE)
        if _needs_confirmation(_request_text(request)) and CONFIRMATION_FLOW_RESPONSE_RULE not in response_rules:
            response_rules.append(CONFIRMATION_FLOW_RESPONSE_RULE)
        normalized = {
            "skill_id": _string(draft.get("skill_id"), fallback.skill_id),
            "name": _string(draft.get("name"), fallback.name),
            "version": _string(draft.get("version"), "1.0.0"),
            "business_domain": _string(draft.get("business_domain"), fallback.business_domain or "general"),
            "description": _string(draft.get("description"), fallback.description),
            "trigger_intents": _string_list(draft.get("trigger_intents"), fallback.trigger_intents),
            "user_utterance_examples": _string_list(
                draft.get("user_utterance_examples"), fallback.user_utterance_examples
            ),
            "goal": _string_list(draft.get("goal"), fallback.goal),
            "required_info": required_info,
            "slot_filling_policy": _slot_filling_policy(
                draft.get("slot_filling_policy"),
                required_info,
                steps,
                fallback.slot_filling_policy,
            ),
            "steps": steps,
            "interruption_policy": _string_dict(draft.get("interruption_policy"), fallback.interruption_policy),
            "response_rules": response_rules,
        }
        draft_skill, card_warnings = skill_card_with_unique_step_ids(SkillCard.model_validate(normalized))
        warnings.extend(card_warnings)
        response = SkillDistillResponse(draft_skill=draft_skill, warnings=_unique_warnings(warnings))
        if not response.draft_skill.steps:
            response.draft_skill.steps = fallback.steps
            response.warnings.append("模型未生成步骤，已使用规则生成默认步骤。")
        return response

    def _ensure_closed_loop_steps(
        self, steps: list[dict[str, Any]], request: SkillDistillRequest
    ) -> tuple[list[dict[str, Any]], list[str]]:
        normalized_steps = [dict(step) for step in steps]
        warnings: list[str] = []
        tool_actions = _tool_actions(request.available_tools)
        has_tool_action = _steps_have_tool_action(normalized_steps)
        needs_tool = any(keyword in request.raw_content for keyword in TOOL_PROCESS_KEYWORDS)

        if tool_actions and needs_tool and not has_tool_action:
            insert_at = max(len(normalized_steps) - 1, 0)
            normalized_steps.insert(
                insert_at,
                {
                    "step_id": _unique_step_id(normalized_steps, "execute_with_tools"),
                    "name": "执行工具处理",
                    "instruction": (
                        "根据技能目标、已收集信息和工具 input_schema 选择合适工具处理；"
                        f"{TOOL_STEP_INSTRUCTION_SUFFIX}"
                    ),
                    "expected_user_info": [],
                    "allowed_actions": ["continue_flow", *tool_actions],
                },
            )
            warnings.append("原始改写未包含工具步骤，已按可用工具补充闭环执行步骤。")

        if tool_actions and _needs_confirmation(_request_text(request)):
            inserted = _ensure_confirmation_before_tool(normalized_steps)
            if inserted:
                warnings.append("原始改写缺少执行前确认步骤，已补充确认步骤。")

        for step in normalized_steps:
            _ensure_adaptive_step_instruction(step)
            actions = [str(action) for action in step.get("allowed_actions", [])]
            if not any(action.startswith("call_tool:") for action in actions):
                continue
            if "continue_flow" not in actions:
                actions.append("continue_flow")
                step["allowed_actions"] = actions
            instruction = str(step.get("instruction") or "")
            if "工具成功后" not in instruction:
                step["instruction"] = f"{instruction}{TOOL_STEP_INSTRUCTION_SUFFIX}"

        if not _last_step_allows_answer(normalized_steps):
            normalized_steps.append(
                {
                    "step_id": _unique_step_id(normalized_steps, "reply_final_result"),
                    "name": "反馈最终结果",
                    "instruction": (
                        "基于已收集信息和工具结果给用户明确最终回复；"
                        "信息不足时追问缺失信息，无法闭环时转人工，不要只说请稍候；"
                        f"{ADAPTIVE_STEP_INSTRUCTION_SUFFIX}"
                    ),
                    "expected_user_info": [],
                    "allowed_actions": ["answer_user", "handoff_human"],
                }
            )
            warnings.append("原始改写缺少最终回复步骤，已补充闭环反馈步骤。")
        else:
            last_step = normalized_steps[-1]
            instruction = str(last_step.get("instruction") or "")
            if "明确" not in instruction or "请稍候" in instruction:
                last_step["instruction"] = (
                    f"{instruction}给用户明确最终回复；无法闭环时转人工，不要只说请稍候。"
                )

        return normalized_steps, warnings

    def _normalize_steps(self, value: Any, fallback_steps: list[SkillStep]) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return [step.model_dump() for step in fallback_steps]
        steps: list[dict[str, Any]] = []
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                continue
            fallback = fallback_steps[min(index, len(fallback_steps) - 1)]
            steps.append(
                {
                    "step_id": _string(item.get("step_id"), fallback.step_id),
                    "name": _string(item.get("name"), fallback.name),
                    "instruction": _string(item.get("instruction"), fallback.instruction),
                    "expected_user_info": _string_list(
                        item.get("expected_user_info"), fallback.expected_user_info
                    ),
                    "allowed_actions": _normalize_actions(
                        _string_list(item.get("allowed_actions"), fallback.allowed_actions)
                    ),
                }
            )
        return steps or [step.model_dump() for step in fallback_steps]

    def _fallback_response(self, request: SkillDistillRequest, warning: str) -> SkillDistillResponse:
        return SkillDistillResponse(draft_skill=self._fallback_card(request), warnings=[warning])

    def _fallback_card(self, request: SkillDistillRequest) -> SkillCard:
        title = request.title.strip() or "新技能"
        raw = request.raw_content
        inferred_fields = _infer_required_fields(raw)
        required_info = [field for field, _label in inferred_fields]
        required_labels = [label for _field, label in inferred_fields]
        tool_actions = _tool_actions(request.available_tools)
        needs_confirmation = bool(tool_actions and _needs_confirmation(_request_text(request)))
        steps: list[SkillStep] = []
        if required_info:
            labels = "、".join(required_labels)
            steps.append(
                SkillStep(
                    step_id="collect_required_info",
                    name="收集必要信息",
                    instruction=(
                        f"询问并记录完成该流程所需的信息：{labels}。如果用户一次提供多个信息，"
                        "需要同时提取并写入对应 slot，不要重复追问已提供的信息；"
                        "如果信息已经满足，直接推进到下一缺失信息、工具调用或最终回复；"
                        f"{NUMERIC_EXTRACTION_INSTRUCTION}"
                        f"{ADAPTIVE_STEP_INSTRUCTION_SUFFIX}"
                    ),
                    expected_user_info=required_info,
                    allowed_actions=["ask_user", "continue_flow"],
                )
            )
        if needs_confirmation:
            steps.append(
                SkillStep(
                    step_id="confirm_operation",
                    name="确认操作信息",
                    instruction=(
                        "调用工具或执行处理前，向用户确认关键对象、数量、订单号、诉求类型等信息；"
                        "只有用户明确确认后，才能写入 operation_confirmed=true 并继续。"
                    ),
                    expected_user_info=["operation_confirmed"],
                    allowed_actions=["ask_user", "continue_flow"],
                )
            )
        if tool_actions:
            steps.append(
                SkillStep(
                    step_id="execute_with_tools",
                    name="执行工具处理",
                    instruction=(
                        "根据技能目标、已收集信息和工具 input_schema 选择合适工具处理；"
                        "只能使用 available_tools 中存在且参数已满足的工具；"
                        f"{ADAPTIVE_STEP_INSTRUCTION_SUFFIX}"
                    ),
                    expected_user_info=[],
                    allowed_actions=["continue_flow", *tool_actions],
                )
            )
        steps.append(
            SkillStep(
                step_id="reply_result",
                name="反馈结果",
                instruction=(
                    "根据已收集的信息和工具结果给用户明确回复；信息不足时继续追问，不要编造事实；"
                    f"{ADAPTIVE_STEP_INSTRUCTION_SUFFIX}"
                ),
                expected_user_info=[],
                allowed_actions=["answer_user", "handoff_human"],
            )
        )
        return SkillCard(
            skill_id=_slugify(title, raw),
            name=title,
            version="1.0.0",
            business_domain=request.business_domain or "general",
            description=raw[:120] or "根据原始技能文本生成的流程。",
            trigger_intents=[title],
            user_utterance_examples=[title],
            goal=_infer_goals(raw),
            required_info=required_info,
            slot_filling_policy=_default_slot_filling_policy(
                [*required_info, *(["operation_confirmed"] if needs_confirmation else [])]
            ),
            steps=steps,
            interruption_policy={
                "related_question": "回答相关问题后回到当前流程。",
                "unrelated_business": "可切换新流程并保留当前进度。",
                "chitchat": "简短回应后引导用户继续当前流程。",
                "user_wants_human": "直接转人工。",
            },
            response_rules=[
                "信息不足时先追问，不要编造事实。",
                ADAPTIVE_FLOW_RESPONSE_RULE,
                *([CONFIRMATION_FLOW_RESPONSE_RULE] if needs_confirmation else []),
            ],
        )


def _steps_have_tool_action(steps: list[dict[str, Any]]) -> bool:
    for step in steps:
        actions = step.get("allowed_actions", [])
        if isinstance(actions, list) and any(str(action).startswith("call_tool:") for action in actions):
            return True
    return False


def _ensure_adaptive_step_instruction(step: dict[str, Any]) -> None:
    instruction = str(step.get("instruction") or "")
    if "目标而不是固定话术" in instruction or "不是固定问答脚本" in instruction:
        return
    step["instruction"] = f"{instruction}{ADAPTIVE_STEP_INSTRUCTION_SUFFIX}"


def _ensure_confirmation_before_tool(steps: list[dict[str, Any]]) -> bool:
    tool_index = next(
        (
            index
            for index, step in enumerate(steps)
            if any(str(action).startswith("call_tool:") for action in step.get("allowed_actions", []))
        ),
        -1,
    )
    if tool_index < 0:
        return False

    prior_confirmation_fields = _confirmation_fields(steps[:tool_index])
    if prior_confirmation_fields:
        _append_tool_confirmation_instruction(steps[tool_index], prior_confirmation_fields)
        return False

    confirmation_field = "operation_confirmed"
    steps.insert(
        tool_index,
        {
            "step_id": _unique_step_id(steps, "confirm_operation"),
            "name": "确认操作信息",
            "instruction": (
                "调用工具或执行处理前，向用户确认关键对象、数量、订单号、诉求类型等信息；"
                f"只有用户明确确认后，才能写入 {confirmation_field}=true 并继续。"
            ),
            "expected_user_info": [confirmation_field],
            "allowed_actions": ["ask_user", "continue_flow"],
        },
    )
    _append_tool_confirmation_instruction(steps[tool_index + 1], [confirmation_field])
    return True


def _confirmation_fields(steps: list[dict[str, Any]]) -> list[str]:
    fields: list[str] = []
    for step in steps:
        expected = [str(field) for field in step.get("expected_user_info", [])]
        for field in expected:
            if field.endswith("_confirmed") and field not in fields:
                fields.append(field)
    return fields


def _append_tool_confirmation_instruction(step: dict[str, Any], confirmation_fields: list[str]) -> None:
    if not confirmation_fields:
        return
    instruction = str(step.get("instruction") or "")
    if "确认字段" in instruction or "confirmed=true" in instruction:
        return
    field_text = "、".join(f"{field}=true" for field in confirmation_fields)
    step["instruction"] = f"{instruction}调用工具前必须确认字段已满足：{field_text}。"


def _last_step_allows_answer(steps: list[dict[str, Any]]) -> bool:
    if not steps:
        return False
    actions = [str(action) for action in steps[-1].get("allowed_actions", [])]
    return "answer_user" in actions or "reply" in actions


def _unique_step_id(steps: list[dict[str, Any]], base: str) -> str:
    existing = {str(step.get("step_id") or "") for step in steps}
    if base not in existing:
        return base
    index = 2
    while f"{base}_{index}" in existing:
        index += 1
    return f"{base}_{index}"


def _unique_warnings(warnings: list[str]) -> list[str]:
    deduped: list[str] = []
    for warning in warnings:
        text = str(warning).strip()
        if text and text not in deduped:
            deduped.append(text)
    return deduped


def _needs_confirmation(raw: str) -> bool:
    return any(keyword in raw for keyword in CONFIRMATION_PROCESS_KEYWORDS) or (
        "订单" in raw and any(keyword in raw for keyword in ("生成", "新增", "添加"))
    )


def _request_text(request: SkillDistillRequest) -> str:
    return f"{request.title}\n{request.raw_content}"


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


def _chunk_text(text: str, size: int = 18):
    for index in range(0, len(text), size):
        yield text[index : index + size]


def _string(value: Any, fallback: str | None = "") -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback or ""


def _string_list(value: Any, fallback: list[str]) -> list[str]:
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
        if items:
            return items
    return fallback


def _string_dict(value: Any, fallback: dict[str, str]) -> dict[str, str]:
    if isinstance(value, dict):
        items = {str(key): str(item) for key, item in value.items() if str(key)}
        if items:
            return items
    return fallback


def _slot_filling_policy(
    value: Any,
    required_info: list[str],
    steps: list[dict[str, Any]],
    fallback_policy: dict[str, Any],
) -> dict[str, Any]:
    has_explicit_policy = isinstance(value, dict)
    if has_explicit_policy:
        policy = dict(value)
    else:
        policy = dict(fallback_policy or {})
    expected_infos = set(required_info)
    for step in steps:
        expected_infos.update(str(field) for field in step.get("expected_user_info", []))
    if has_explicit_policy and isinstance(policy.get("target_info"), list):
        expected_infos.update(str(field) for field in policy["target_info"] if str(field).strip())
    default_policy = _default_slot_filling_policy(sorted(expected_infos))
    return {
        **default_policy,
        **policy,
        "enabled": True,
        "multi_slot_per_turn": True,
        "extract_scope": "all_skill_expected_user_info",
        "skip_satisfied_steps": True,
        "target_info": sorted(expected_infos),
    }


def _default_slot_filling_policy(expected_infos: list[str]) -> dict[str, Any]:
    return {
        "enabled": True,
        "multi_slot_per_turn": True,
        "extract_scope": "all_skill_expected_user_info",
        "skip_satisfied_steps": True,
        "description": "每轮用户消息都应同时抽取所有可识别的信息；如果用户一次提供多个字段，必须一次性写入 slot_updates，不要按步骤重复追问。",
        "target_info": expected_infos,
    }


def _normalize_actions(actions: list[str]) -> list[str]:
    aliases = {
        "ask_for_info": "ask_user",
        "ask": "ask_user",
        "reply": "answer_user",
        "respond": "answer_user",
    }
    normalized: list[str] = []
    for action in actions:
        value = aliases.get(action, action)
        if value not in normalized:
            normalized.append(value)
    return normalized


def _tool_actions(available_tools: list[dict[str, Any]]) -> list[str]:
    actions: list[str] = []
    for tool in available_tools:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or "").strip()
        if name:
            actions.append(f"call_tool:{name}")
    return actions


def _infer_goals(raw: str) -> list[str]:
    clauses = [clause.strip() for clause in _split_clauses(raw) if clause.strip()]
    return clauses or ["理解用户诉求", "收集必要信息", "完成流程处理", "向用户反馈结果"]


def _infer_required_fields(raw: str) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    seen: set[str] = set()
    for clause in _split_clauses(raw):
        label = _extract_info_label(clause)
        if not label:
            continue
        field = _field_id(label, len(fields) + 1)
        if field in seen:
            continue
        fields.append((field, label))
        seen.add(field)
    return fields


def _split_clauses(text: str) -> list[str]:
    normalized = text.replace("\n", "，").replace("；", "，").replace(";", "，").replace(",", "，").replace("。", "，")
    return [part.strip() for part in normalized.split("，")]


def _extract_info_label(clause: str) -> str | None:
    text = clause.strip()
    for verb in ("获取", "收集", "询问", "确认", "记录", "填写", "提供", "输入"):
        if text.startswith(verb):
            label = text[len(verb) :].strip(" ：:，。")
            label = label.removeprefix("用户").removeprefix("客户").removeprefix("您的").strip(" 的")
            if label and not any(word in label for word in ("是否", "结果", "流程", "状态")):
                return label[:24]
    return None


def _field_id(label: str, index: int) -> str:
    common = {
        "姓名": "user_name",
        "名字": "user_name",
        "联系方式": "contact",
        "手机号": "phone",
        "电话": "phone",
    }
    for key, value in common.items():
        if key in label:
            return value
    ascii_slug = "".join(char.lower() if char.isalnum() else "_" for char in label if ord(char) < 128)
    ascii_slug = "_".join(part for part in ascii_slug.split("_") if part)
    return ascii_slug[:48] if ascii_slug else f"info_{index}"


def _slugify(title: str, raw: str) -> str:
    ascii_slug = "".join(char.lower() if char.isalnum() else "_" for char in title if ord(char) < 128)
    ascii_slug = "_".join(part for part in ascii_slug.split("_") if part)
    if ascii_slug:
        return ascii_slug[:48]
    digest = hashlib.md5(f"{title}:{raw}".encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
    return f"skill_{digest}"
