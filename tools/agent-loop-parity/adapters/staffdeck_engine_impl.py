"""Run one parity scenario through StaffDeck's complete HarnessV2Engine path."""
from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
import threading
import time
import urllib.request
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

SOURCE_ROOT = Path(os.environ["PARITY_SOURCE_ROOT"]).resolve()
BACKEND_ROOT = (SOURCE_ROOT / "backend").resolve()
MODE = os.environ["PARITY_MODE"]
PILOTDECK_ROOT = Path(os.environ.get("PARITY_PILOTDECK_ROOT", "")).resolve()
RUNTIME_DIR = tempfile.TemporaryDirectory(prefix=f"staffdeck-parity-{MODE}-")
RUN_KEY = os.environ.get("PARITY_RUN_KEY", f"{MODE}-parity")
# StaffDeck's host task runner intentionally serializes capability actions. The
# mock's concurrency barrier uses the historical "pilotdeck" run-key marker;
# keep this adapter out of that probe so it does not manufacture a deadlock.
if MODE == "pilotdeck":
    RUN_KEY = RUN_KEY.replace("pilotdeck", "staffserial")

os.environ["APP_SECRET"] = "agent-loop-parity-test-secret"
os.environ["ULTRARAG_DATA_DIR"] = RUNTIME_DIR.name
os.environ["PILOTDECK_AGENT_LOOP_ENABLED"] = "true" if MODE == "pilotdeck" else "false"
if MODE == "pilotdeck":
    sidecar_path = PILOTDECK_ROOT / "dist/src/cli/pilotdeck-agent-loop-sidecar.js"
    os.environ["PILOTDECK_AGENT_LOOP_COMMAND"] = shlex.join(["node", str(sidecar_path)])
    os.environ["PILOTDECK_AGENT_LOOP_CWD"] = str(PILOTDECK_ROOT)
    os.environ["PILOTDECK_AGENT_LOOP_TIMEOUT_SECONDS"] = "20"

sys.path.insert(0, str(BACKEND_ROOT))

import app
from app.config import get_settings
from app.core import harness_agent as harness_agent_module
from app.core import harness_v2_engine as harness_v2_engine_module
from app.core.agent_loop import AgentLoop
from app.core.cancellation import (
    cancel_chat_turn,
    clear_chat_turn_cancelled,
)
from app.core.capability_manifest import CapabilityManifestBuilder
from app.core.harness_capability_invoker import HarnessCapabilityInvoker
from app.core.task_frame_store import TaskFrameStore
from app.core.task_request_compiler import (
    CapabilityDescriptor,
    CapabilityManifest,
)
from app.db.models import (
    ChatSession,
    HarnessAgentLoopRecord,
    HarnessRunRecord,
    HarnessTaskFrameRecord,
    HarnessTurnRecord,
    ModelConfig,
    Skill,
    Tenant,
    UIConfig,
)
from app.security.encryption import encrypt_secret
from app.session.session_schema import (
    ChatAttachmentRead,
    ChatTurnRequest,
    PlannedTaskFrame,
    TurnPlan,
)
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select


def post(url: str, body: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


class TraceRecorder:
    def __init__(self, scenario: dict[str, Any]) -> None:
        self.scenario = scenario
        self.records: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def add(self, kind: str, **payload: Any) -> None:
        with self._lock:
            self.records.append(
                {
                    "kind": kind,
                    "scenarioId": self.scenario["scenarioId"],
                    "q": self.scenario["q"],
                    "sequence": len(self.records),
                    **payload,
                }
            )


def _assert_source_checkout() -> None:
    module_path = Path(app.__file__ or "").resolve()
    if not module_path.is_relative_to(BACKEND_ROOT):
        raise RuntimeError(
            f"StaffDeck adapter imported app from {module_path}, expected {BACKEND_ROOT}"
        )
    if MODE == "pilotdeck":
        sidecar_path = PILOTDECK_ROOT / "dist/src/cli/pilotdeck-agent-loop-sidecar.js"
        if not sidecar_path.is_file():
            raise RuntimeError(f"PilotDeck sidecar build is missing: {sidecar_path}")


def _manifest(scenario: dict[str, Any]) -> CapabilityManifest:
    return CapabilityManifest(
        available=[
            CapabilityDescriptor(
                capability_id=f"parity-{name}",
                name=name,
                kind="tool",
                capability_scope="sop_specific",
                description=name,
                input_schema={"type": "object"},
                metadata={
                    "readOnly": name not in {"restricted", "loop"},
                    "concurrencySafe": name in {"lookup", "summarize"},
                },
            )
            for name in scenario.get("tools", [])
        ]
    )


def _skill(scenario: dict[str, Any]) -> Skill | None:
    sop = scenario.get("sop") or scenario.get("forcedSopSnapshot")
    if not isinstance(sop, dict) and scenario["scenarioId"] not in {"deadline", "deadline_during_tool"}:
        return None
    if isinstance(sop, dict):
        nodes = []
        for raw in sop.get("steps") or []:
            if not isinstance(raw, dict):
                continue
            node = {
                "node_id": raw.get("id") or raw.get("node_id"),
                "type": raw.get("type", "task"),
                "node_type": raw.get("type", "task"),
                "instruction": raw.get("instruction") or "Execute the deterministic SOP step.",
            }
            required_slots = raw.get("requiredSlots") or raw.get("expectedUserInfo")
            if required_slots:
                node["expected_user_info"] = list(required_slots)
            capabilities = raw.get("requiredCapabilities") or []
            knowledge = raw.get("requiredKnowledgeBaseIds") or []
            if capabilities or knowledge:
                node["capability_refs"] = {
                    "required_tool_ids": list(capabilities),
                    "required_knowledge_base_ids": list(knowledge),
                }
            nodes.append(node)
        edges = []
        steps = sop.get("steps") or []
        for raw in steps:
            if not isinstance(raw, dict):
                continue
            source = raw.get("id") or raw.get("node_id")
            target = raw.get("nextStepId")
            if source and target:
                edges.append({"source_node_id": source, "next_node_id": target})
            for transition in raw.get("transitions") or []:
                if isinstance(transition, dict) and transition.get("to"):
                    edges.append({
                        "source_node_id": source,
                        "next_node_id": transition["to"],
                        "condition": transition.get("when"),
                    })
        content = {
            "start_node_id": sop.get("startStepId") or sop.get("start_node_id"),
            "nodes": nodes,
            "edges": edges,
            "goal": [str(scenario.get("q") or "")],
        }
        step_timeout_seconds = (scenario.get("limits") or {}).get(
            "stepTimeoutSeconds"
        )
        if step_timeout_seconds is not None:
            content["step_timeout_seconds"] = int(step_timeout_seconds)
        return Skill(
            id=f"skill-parity-{scenario['scenarioId']}",
            tenant_id="tenant-parity",
            skill_id=str(sop.get("id") or f"parity-{scenario['scenarioId']}"),
            version=str(sop.get("version") or "1"),
            name=f"Parity {scenario['scenarioId']}",
            status="published",
            content_json=content,
        )
    
    return Skill(
        id="skill-parity-deadline",
        tenant_id="tenant-parity",
        skill_id="parity-deadline",
        version="1.0.0",
        name="Parity deadline",
        status="published",
        content_json={
            "start_node_id": "wait",
            "step_timeout_seconds": int(
                (scenario.get("limits") or {}).get("stepTimeoutSeconds") or 1
            ),
            "nodes": [
                {
                    "node_id": "wait",
                    "name": "Wait for deterministic model",
                    "type": "task",
                }
            ],
            "edges": [],
        },
    )


def _plan(scenario: dict[str, Any], skill: Skill | None) -> TurnPlan:
    content = skill.content_json if skill is not None else {}
    start_step_id = (
        str(content.get("start_node_id") or "").strip()
        if isinstance(content, dict)
        else ""
    )
    if scenario["scenarioId"] == "sop_task_dependency" and skill is not None:
        return TurnPlan(
            decision="start_new_task",
            selected_task_id="a",
            user_intent=str(scenario["q"]),
            task_frames=[
                PlannedTaskFrame(
                    task_id="a",
                    kind="sop",
                    decision="start_new_task",
                    target_skill_id=skill.skill_id,
                    target_step_id="a",
                    user_intent=str(scenario["q"]),
                    requirements=[str(scenario["q"])],
                    source_message=str(scenario["q"]),
                ),
                PlannedTaskFrame(
                    task_id="b",
                    kind="sop",
                    decision="start_new_task",
                    target_skill_id=skill.skill_id,
                    target_step_id="b",
                    user_intent=str(scenario["q"]),
                    requirements=[str(scenario["q"])],
                    depends_on_task_ids=["a"],
                    source_message=str(scenario["q"]),
                ),
            ],
        )
    return TurnPlan(
        decision="start_new_task" if skill else "answer_only",
        selected_task_id=f"task-{scenario['scenarioId']}",
        user_intent=str(scenario["q"]),
        task_frames=[
            PlannedTaskFrame(
                task_id=f"task-{scenario['scenarioId']}",
                kind="sop" if skill else "conversation",
                decision="start_new_task" if skill else "answer_only",
                target_skill_id=skill.skill_id if skill else None,
                target_step_id=start_step_id or None,
                user_intent=str(scenario["q"]),
                requirements=[str(scenario["q"])],
                slot_hints=dict(scenario.get("knownSlots") or {}),
                execution_target=(
                    "team_member"
                    if scenario.get("executionTarget") == "team_member"
                    else "self"
                ),
                source_message=str(scenario["q"]),
            )
        ],
    )


def _forced_sop_snapshot(scenario: dict[str, Any], skill: Skill | None) -> dict[str, Any] | None:
    raw = scenario.get("forcedSopSnapshot")
    if not isinstance(raw, dict) or skill is None:
        return None
    return {
        "skill_id": skill.skill_id,
        "version": skill.version,
        "name": skill.name,
        "content_json": dict(skill.content_json),
    }


def _attachments(scenario: dict[str, Any]) -> list[ChatAttachmentRead]:
    attachments: list[ChatAttachmentRead] = []
    for message in scenario.get("messages") or []:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for index, block in enumerate(content):
            if not isinstance(block, dict) or block.get("type") != "image_url":
                continue
            image_url = block.get("image_url")
            data_url = image_url.get("url") if isinstance(image_url, dict) else None
            if not isinstance(data_url, str) or "," not in data_url:
                continue
            header, encoded = data_url.split(",", 1)
            import base64

            raw = base64.b64decode(encoded, validate=True)
            content_type = header.removeprefix("data:").split(";", 1)[0]
            attachments.append(
                ChatAttachmentRead(
                    id=f"parity-image-{index}",
                    filename=f"image-{index}.png",
                    content_type=content_type,
                    size=len(raw),
                    kind="image",
                    data_url=data_url,
                )
            )
    return attachments


def _model_messages(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    context = payload.get("conversation_context")
    context_messages = (
        context.get("messages", []) if isinstance(context, dict) else []
    )
    transcript = payload.get("harness_transcript")
    transcript_messages = transcript if isinstance(transcript, list) else []
    return [
        dict(message)
        for message in [*context_messages, *transcript_messages]
        if isinstance(message, dict)
    ]


def _json_object_from_message(message: dict[str, Any]) -> dict[str, Any] | None:
    content = message.get("content")
    candidates: list[str] = []
    if isinstance(content, str):
        candidates.append(content)
    elif isinstance(content, list):
        candidates.extend(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and ("goal" in value or "task_frame_id" in value):
            return value
    return None


def _semantic_model_view(payload: Any, messages: list[dict[str, Any]]) -> dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    requirement = payload.get("task_requirement")
    if not isinstance(requirement, dict):
        requirement = next(
            (
                parsed
                for message in messages
                for parsed in [_json_object_from_message(message)]
                if parsed is not None
            ),
            {},
        )
    task_fields = (
        "goal",
        "requirements",
        "required_slots",
        "known_slots",
        "completion_criteria",
        "required_capability_names",
        "required_knowledge_base_ids",
        "allowed_transitions",
        "prior_task_results",
        "sop_context",
    )
    task = {key: requirement.get(key) for key in task_fields if key in requirement}
    manifest = requirement.get("capability_manifest") if isinstance(requirement, dict) else {}
    available = manifest.get("available") if isinstance(manifest, dict) else None
    payload_tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
    tools = [
        item.get("name")
        for item in (available if isinstance(available, list) else payload_tools)
        if isinstance(item, dict) and item.get("name")
    ]
    images = [
        node
        for message in messages
        for node in _walk(message)
        if isinstance(node, dict)
        and (
            node.get("type") == "image_url"
            or (node.get("type") == "image" and node.get("source") == "base64")
        )
    ]
    tool_results: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "tool":
            result = message.get("result") if isinstance(message.get("result"), dict) else {}
            tool_results.append({
                "toolName": message.get("tool_name") or result.get("toolName"),
                "success": result.get("success"),
                "data": result.get("data"),
                "error": result.get("error"),
            })
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            raw = block.get("raw") if isinstance(block.get("raw"), dict) else {}
            tool_results.append({
                "toolName": raw.get("toolName"),
                "success": raw.get("type") == "success",
                "data": raw.get("data") if raw.get("type") == "success" else None,
                "error": raw.get("error") if isinstance(raw.get("error"), dict) else (
                    {"code": raw.get("code"), "message": raw.get("data"), "retryable": raw.get("retryable")}
                    if raw.get("type") == "error"
                    else None
                ),
            })
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    return {
        "task": task,
        "tools": tools,
        "images": images,
        "toolResults": tool_results,
        "remainingActions": payload.get("remaining_actions", metadata.get("remainingActions")),
    }


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _action_from_message(message: dict[str, Any]) -> dict[str, Any]:
    calls = message.get("tool_calls") or []
    if calls:
        call = calls[0]
        return {
            "action": "tool",
            "tool_name": call["function"]["name"],
            "tool_call_id": call["id"],
            "arguments": json.loads(call["function"]["arguments"]),
        }
    content = message.get("content")
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict) and parsed.get("action") in {"finish", "tool"}:
            return parsed
    return {
        "action": "finish",
        "status": "completed",
        "reply_fragment": message.get("content") or "",
    }


def _actions_from_message(message: dict[str, Any]) -> dict[str, Any]:
    calls = message.get("tool_calls") or []
    if not calls:
        return _action_from_message(message)
    return {
        "actions": [
            {
                "action": "tool",
                "tool_name": call["function"]["name"],
                "arguments": json.loads(call["function"]["arguments"]),
            }
            for call in calls
        ]
    }


def _seed_checkpoint(
    db: Session,
    scenario: dict[str, Any],
    session: ChatSession,
    plan: TurnPlan,
) -> None:
    if scenario["scenarioId"] != "checkpoint_resume":
        return
    store = TaskFrameStore(db)
    row = store.persist_plan(session, "message-before-resume", plan)[0]
    loop = store.ensure_agent_loop(row)
    messages = [
        dict(message)
        for message in scenario.get("messages") or []
        if isinstance(message, dict)
    ]
    checkpoint = {
        "version": 1,
        "task_frame_id": row.task_id,
        "step_id": row.step_id or "",
        "transcript": messages,
        "agentLoopMessages": messages,
        "agentLoopSeedState": dict(scenario.get("seedState") or {}),
    }
    store.save_agent_loop_checkpoint(loop, checkpoint, status="active")
    db.commit()


def _terminal_payload(db: Session, response: Any) -> dict[str, Any]:
    receipt = db.exec(
        select(HarnessTurnRecord).order_by(HarnessTurnRecord.created_at.desc())
    ).first()
    run = db.exec(
        select(HarnessRunRecord).order_by(HarnessRunRecord.created_at.desc())
    ).first()
    frame = db.exec(
        select(HarnessTaskFrameRecord).order_by(
            HarnessTaskFrameRecord.created_at.desc()
        )
    ).first()
    loop = db.exec(
        select(HarnessAgentLoopRecord).order_by(
            HarnessAgentLoopRecord.created_at.desc()
        )
    ).first()
    session = db.exec(select(ChatSession)).first()
    result = dict(run.result_json or {}) if run is not None else {}
    if not result and frame is not None:
        result = dict(frame.result_json or {})
    result_error = (
        result.get("error") if isinstance(result.get("error"), dict) else {}
    )
    receipt_error = (
        dict(receipt.error_json or {}) if receipt is not None else {}
    )
    status = str(result.get("status") or "")
    checkpoint = dict(loop.checkpoint_json or {}) if loop is not None else {}
    successful_knowledge_searches = int(
        result.get("successful_knowledge_searches")
        or checkpoint.get("successful_knowledge_searches")
        or 0
    )
    if receipt_error.get("code") == "RESULT_UNKNOWN":
        status = "result_unknown"
    elif receipt is not None and receipt.status in {"failed", "cancelled"}:
        status = receipt.status
    elif not status:
        status = "failed" if response.runtime_error_code else "completed"
    return {
        "outcome": status,
        "code": (
            receipt_error.get("code")
            or result_error.get("code")
            or response.runtime_error_code
        ),
        "stopReason": status,
        "structuredResult": result.get("structured_result"),
        "output": result.get("reply_fragment") or response.reply,
        "turnStatus": receipt.status if receipt is not None else None,
        "frameStatus": frame.status if frame is not None else None,
        "runStatus": run.status if run is not None else None,
        "taskFrame": {
            "status": frame.status if frame is not None else None,
            "stepId": frame.step_id if frame is not None else None,
            "nextStepId": result.get("next_step_id"),
            "slots": dict(frame.slots_json or {}) if frame is not None else {},
            "requiredCapabilities": list((frame.task_requirement_json or {}).get("required_capability_names") or []) if frame is not None else [],
            "knowledgeBudget": {
                "successfulCalls": successful_knowledge_searches,
                "remaining": max(0, 2 - successful_knowledge_searches),
            },
            "priorTaskResults": list((frame.task_requirement_json or {}).get("prior_task_results") or []) if frame is not None else [],
        },
        "session": {
            "activeSkillId": session.active_skill_id if session is not None else None,
            "activeStepId": session.active_step_id if session is not None else None,
            "slots": dict(session.slots_json or {}) if session is not None else {},
            "pendingTasks": list(session.pending_tasks_json or []) if session is not None else [],
            "awaitingInput": session.awaiting_input_json if session is not None else None,
            "handoff": session.status == "handoff" if session is not None else False,
            "priorTaskResults": list(session.context_state_json.get("prior_task_results") or []) if session is not None else [],
        },
    }


def main(expected_mode: str | None = None) -> int:
    _assert_source_checkout()
    if expected_mode is not None and MODE != expected_mode:
        raise RuntimeError(f"adapter mode is {MODE}, expected {expected_mode}")
    get_settings.cache_clear()
    scenario = json.loads(os.environ["PARITY_SCENARIO_JSON"])
    mock_url = os.environ["PARITY_MOCK_BASE_URL"]
    output = Path(os.environ["PARITY_TRACE_OUT"])
    recorder = TraceRecorder(scenario)
    manifest = _manifest(scenario)
    skill = _skill(scenario)
    plan = _plan(scenario, skill)
    session_id = "session-parity"
    client_turn_id = f"turn-{scenario['scenarioId']}"
    limits = scenario.get("limits") if isinstance(scenario.get("limits"), dict) else {}
    tool_started = threading.Event()
    applied_forced_sop_version: list[str] = []

    class ParityModelClient:
        def __init__(self, _config: Any) -> None:
            pass

        def _generate(self, system_prompt: str, payload: Any) -> dict[str, Any]:
            messages = _model_messages(payload)
            attempt = 1 + sum(record["kind"] == "model.request" for record in recorder.records)
            recorder.add(
                "model.request",
                attempt=attempt,
                modelView=_semantic_model_view(payload, messages),
                request={
                    "systemPrompt": system_prompt,
                    "messages": messages,
                    "payload": payload,
                },
            )
            response = post(
                f"{mock_url}/v1/chat/completions",
                {
                    "scenarioId": scenario["scenarioId"],
                    "q": scenario["q"],
                    "messages": messages,
                    "delays": scenario.get("delays", {}),
                    "toolDelays": scenario.get("toolDelays", {}),
                    "faults": scenario.get("faults", {}),
                    "runKey": RUN_KEY,
                },
            )
            message = response["choices"][0]["message"]
            recorder.add("model.response", attempt=attempt, modelView=message, response=message)
            return message

        def generate_json(
            self,
            system_prompt: str,
            payload: Any,
            _cancellation: Any = None,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            return _action_from_message(self._generate(system_prompt, payload))

        def generate_json_sequence(
            self,
            system_prompt: str,
            payload: Any,
            _cancellation: Any = None,
        ) -> dict[str, Any]:
            return _actions_from_message(self._generate(system_prompt, payload))

    def deterministic_plan(_self: Any, *_args: Any, **_kwargs: Any) -> TurnPlan:
        return plan.model_copy(deep=True)

    def deterministic_manifest(_self: Any, *_args: Any, **_kwargs: Any) -> CapabilityManifest:
        return manifest.model_copy(deep=True)

    def deterministic_skills(_self: Any, *_args: Any, **_kwargs: Any) -> list[Skill]:
        return [skill] if skill is not None else []

    original_apply_forced_sop_snapshot = (
        harness_v2_engine_module._apply_forced_sop_snapshot
    )

    def observe_forced_sop_snapshot(
        source_skills: list[Skill],
        forced_sop_id: str | None,
        snapshot: dict[str, Any] | None,
    ) -> list[Skill]:
        applied = original_apply_forced_sop_snapshot(
            source_skills,
            forced_sop_id,
            snapshot,
        )
        target = str(forced_sop_id or "").strip()
        pinned = next((item for item in applied if item.skill_id == target), None)
        if pinned is not None:
            applied_forced_sop_version[:] = [str(pinned.version)]
        return applied

    def invoke_external_tool(
        _self: Any,
        _capability_id: str,
        _metadata: dict[str, Any],
        name: str,
        arguments: dict[str, Any],
        *,
        call_id: str,
    ) -> dict[str, Any]:
        sidecar_faults = (
            scenario.get("faults", {}).get("sidecar", [])
            if isinstance(scenario.get("faults"), dict)
            else []
        )
        fault_stages = {
            str(item.get("stage") or "")
            for item in sidecar_faults
            if isinstance(item, dict) and item.get("action") == "exit"
        }
        denied = name in scenario.get("permission", {}).get("deny", [])
        recorder.add("permission.request", toolName=name, mode=scenario.get("permission", {}).get("mode", "default"), canPrompt=scenario.get("permission", {}).get("canPrompt", False))
        recorder.add("permission.decision", toolName=name, allowed=not denied)
        if denied:
            return {
                "success": False,
                "error": {
                    "code": "PERMISSION_DENIED",
                    "message": "Deterministic permission denial.",
                    "retryable": False,
                },
            }
        recorder.add("tool.call", name=name, arguments=arguments)
        recorder.add("tool.start", name=name)
        tool_started.set()
        if "before_tool" in fault_stages:
            return _result_unknown(
                "Parity host dispatcher lost the operation before capability acknowledgement."
            )
        step_timeout_seconds = (scenario.get("limits") or {}).get(
            "stepTimeoutSeconds"
        )
        operation_deadline_epoch_ms = (
            int(time.time() * 1000) + int(step_timeout_seconds) * 1000
            if step_timeout_seconds is not None
            else None
        )
        result = post(
            f"{mock_url}/tools/execute",
            {
                "scenarioId": scenario["scenarioId"],
                "q": scenario["q"],
                "name": name,
                "arguments": arguments,
                "permissionAllowed": True,
                "delays": scenario.get("delays", {}),
                "toolDelays": scenario.get("toolDelays", {}),
                "faults": scenario.get("faults", {}),
                "runKey": RUN_KEY,
                # The real invoker receives a remaining-time override.  This
                # deterministic provider fault point needs the same deadline
                # so it can prove a timed-out write was never committed.
                "operationDeadlineEpochMs": operation_deadline_epoch_ms,
            },
        )
        if "after_tool" in fault_stages:
            return _result_unknown(
                "Parity host dispatcher lost the operation after capability acknowledgement."
            )
        recorder.add("tool.finish", name=name, success=result.get("type") == "success", error=result.get("error"), sideEffectCount=(result.get("data") or {}).get("sideEffectCount"))
        recorder.add("tool.result", result=result, sideEffectCount=(result.get("data") or {}).get("sideEffectCount"))
        response: dict[str, Any] = {
            "success": result["type"] == "success",
            "data": result.get("data"),
            "error": result.get("error"),
        }
        if name == "knowledge_search" and result.get("type") == "success":
            data = result.get("data")
            if isinstance(data, dict) and data.get("evidence"):
                evidence = {
                    "knowledge_base_id": next(
                        iter(data.get("knowledgeBaseIds") or ["handbook"]),
                        "handbook",
                    ),
                    "content": str(data["evidence"]),
                }
                response["data"] = {**data, "evidence_pack": [evidence]}
                response["citations"] = [evidence]
        return response

    database = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(database)
    cancellation_thread: threading.Thread | None = None
    with Session(database) as db:
        model = ModelConfig(
            id="model-parity",
            tenant_id="tenant-parity",
            name="Parity model",
            provider="openai_compatible",
            base_url=mock_url,
            api_key_encrypted=encrypt_secret("parity-key"),
            model="deterministic",
            is_default=True,
            enabled=True,
        )
        session = ChatSession(
            id=session_id,
            tenant_id="tenant-parity",
            user_id="user-parity",
        )
        db.add(Tenant(id="tenant-parity", name="Parity"))
        db.add(model)
        db.add(
            UIConfig(
                tenant_id="tenant-parity",
                agent_loop_max_actions=int(limits.get("remainingActions") or 6),
            )
        )
        db.add(session)
        db.commit()
        known_slots = scenario.get("knownSlots")
        if isinstance(known_slots, dict):
            session.slots_json = dict(known_slots)
            db.add(session)
            db.commit()
        _seed_checkpoint(db, scenario, session, plan)

        interaction_mode = "normal"
        if scenario.get("category") == "scheduled":
            interaction_mode = "scheduled_task"
        elif scenario.get("executionTarget") == "team_member":
            interaction_mode = "team_task"
        request = ChatTurnRequest(
            tenant_id="tenant-parity",
            session_id=session_id,
            user_id="user-parity",
            model_config_id=model.id,
            client_turn_id=client_turn_id,
            message=str(scenario["q"]),
            attachments=_attachments(scenario),
            interaction_mode=interaction_mode,
            forced_sop_id=(skill.skill_id if scenario.get("forcedSopSnapshot") and skill else None),
            forced_sop_snapshot=_forced_sop_snapshot(scenario, skill),
        )
        cancel_after_ms = int(limits.get("cancelAfterMs") or limits.get("cancelAfterToolStartMs") or 0)
        if cancel_after_ms:
            wait_for_tool = bool(limits.get("cancelAfterToolStartMs"))

            def request_cancel() -> None:
                if wait_for_tool:
                    tool_started.wait(timeout=5)
                time.sleep(cancel_after_ms / 1000)
                post(f"{mock_url}/control/cancel", {"runKey": RUN_KEY})
                cancel_chat_turn(session_id, client_turn_id)

            cancellation_thread = threading.Thread(
                target=request_cancel,
                daemon=True,
            )
            cancellation_thread.start()

        loop = AgentLoop(db)
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(harness_v2_engine_module.TurnPlanner, "plan", deterministic_plan)
            )
            stack.enter_context(
                patch.object(CapabilityManifestBuilder, "build", deterministic_manifest)
            )
            stack.enter_context(
                patch.object(AgentLoop, "_list_published_skills", deterministic_skills)
            )
            stack.enter_context(
                patch.object(
                    harness_v2_engine_module,
                    "_apply_forced_sop_snapshot",
                    observe_forced_sop_snapshot,
                )
            )
            stack.enter_context(
                patch.object(
                    HarnessCapabilityInvoker,
                    "_invoke_external_tool",
                    invoke_external_tool,
                )
            )
            stack.enter_context(
                patch.object(harness_agent_module, "LLMClient", ParityModelClient)
            )
            if hasattr(harness_v2_engine_module, "LLMClient"):
                stack.enter_context(
                    patch.object(
                        harness_v2_engine_module,
                        "LLMClient",
                        ParityModelClient,
                    )
                )
            response = loop.handle_turn(request)

        mock_state = post(f"{mock_url}/control/state", {"runKey": RUN_KEY})
        side_effect_counts = mock_state.get("sideEffects") if isinstance(mock_state, dict) else {}
        recorder.add(
            "side_effect.state",
            counts=side_effect_counts,
            sideEffectCount=sum(int(value) for value in (side_effect_counts or {}).values()),
        )

        checkpoint_row = db.exec(
            select(HarnessAgentLoopRecord).order_by(
                HarnessAgentLoopRecord.created_at.desc()
            )
        ).first()
        if checkpoint_row is not None:
            recorder.add("checkpoint", checkpoint=checkpoint_row.checkpoint_json)
        frame_snapshot = db.exec(
            select(HarnessTaskFrameRecord).order_by(HarnessTaskFrameRecord.created_at.desc())
        ).first()
        if frame_snapshot is not None:
            recorder.add(
                "taskframe",
                taskFrame={
                    "status": frame_snapshot.status,
                    "stepId": frame_snapshot.step_id,
                    "slots": dict(frame_snapshot.slots_json or {}),
                    "requiredCapabilities": list((frame_snapshot.task_requirement_json or {}).get("required_capability_names") or []),
                    "priorTaskResults": list((frame_snapshot.task_requirement_json or {}).get("prior_task_results") or []),
                },
            )
        recorder.add(
            "session.state",
            activeSkillId=session.active_skill_id,
            activeStepId=session.active_step_id,
            slots=dict(session.slots_json or {}),
            pendingTasks=list(session.pending_tasks_json or []),
            awaitingInput=session.awaiting_input_json,
            handoff=session.status == "handoff",
        )
        recorder.add(
            "terminal",
            **_terminal_payload(db, response),
            forcedSopVersion=(
                applied_forced_sop_version[-1]
                if applied_forced_sop_version
                else None
            ),
        )
        recorder.add("user.output", text=response.reply)

    if cancellation_thread is not None:
        cancellation_thread.join(timeout=2)
    clear_chat_turn_cancelled(session_id, client_turn_id)
    output.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in recorder.records)
        + "\n",
        encoding="utf-8",
    )
    RUNTIME_DIR.cleanup()
    return 0


def _result_unknown(message: str) -> dict[str, Any]:
    return {
        "success": False,
        "outcome": "result_unknown",
        "error": {
            "code": "RESULT_UNKNOWN",
            "message": message,
            "retryable": False,
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
