"""Task-layer ACP capability registration and dispatch (U5).

The four internal capabilities (acp_compress / acp_decompress /
acp_search_context / acp_status) are only advertised when the tenant
preference resolves to ACP; under legacy the names stay out of
``allowed_names()`` and a model call hits the illegal-tool error path.
"""

import json
from copy import deepcopy

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.core.capability_manifest import (
    CapabilityManifestBuilder,
    _acp_capability_descriptors,
)
from app.core.harness_agent import HarnessTaskAgent, _transcript_for_model
from app.core.task_request_compiler import (
    CapabilityDescriptor,
    CapabilityManifest,
    TaskRequirement,
)
from app.db.models import ModelConfig, Tenant
from app.core import harness_agent as harness_agent_module

ACP_NAMES = {"acp_compress", "acp_decompress", "acp_search_context", "acp_status"}


def _model_config() -> ModelConfig:
    return ModelConfig(
        id="model-test",
        tenant_id="tenant-demo",
        name="测试模型",
        api_key_encrypted="test",
        model="test-model",
    )


def _acp_requirement(*, include_acp: bool = True) -> TaskRequirement:
    available: list[CapabilityDescriptor] = [
        CapabilityDescriptor(
            capability_id="allowed",
            name="allowed.tool",
            kind="tool",
        )
    ]
    if include_acp:
        available.extend(_acp_capability_descriptors())
    return TaskRequirement(
        task_frame_id="task-1",
        kind="conversation",
        goal="查询物流",
        requirements=["查询 ORDER-1 的物流"],
        capability_manifest=CapabilityManifest(available=available),
    )


def _fake_llm(monkeypatch, actions, payloads: list | None = None):
    class FakeLLMClient:
        def __init__(self, _model_config: ModelConfig, session_id: str | None = None):
            pass

        def generate_json(
            self, system_prompt: str, payload: dict[str, object]
        ) -> dict[str, object]:
            if payloads is not None:
                payloads.append(deepcopy(payload))
            return next(actions)

    monkeypatch.setattr(harness_agent_module, "LLMClient", FakeLLMClient)


def _tool_invoke(invoked: list | None = None):
    def invoke_tool(name: str, arguments: dict[str, object]) -> dict[str, object]:
        if invoked is not None:
            invoked.append((name, arguments))
        return {
            "success": True,
            "data": {"status": "in_transit", "note": "ORDER-1 已发货"},
        }

    return invoke_tool


def _test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def test_acp_capabilities_only_injected_when_mode_is_acp() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        db.add(Tenant(id="tenant-demo", name="Demo"))
        db.commit()
        acp_manifest = CapabilityManifestBuilder(db).build(
            "tenant-demo", None, None, None, context_compression_mode="acp"
        )
        legacy_manifest = CapabilityManifestBuilder(db).build(
            "tenant-demo", None, None, None, context_compression_mode="legacy"
        )
        default_manifest = CapabilityManifestBuilder(db).build(
            "tenant-demo", None, None, None
        )

    assert ACP_NAMES <= acp_manifest.allowed_names()
    assert not (ACP_NAMES & legacy_manifest.allowed_names())
    assert not (ACP_NAMES & default_manifest.allowed_names())
    descriptors = {
        item.name: item
        for item in acp_manifest.available
        if item.name in ACP_NAMES
    }
    assert len(descriptors) == 4
    assert all(item.kind == "internal" for item in descriptors.values())
    assert all(item.available for item in descriptors.values())
    assert all(
        item.capability_id.startswith("builtin.acp.") for item in descriptors.values()
    )


def test_acp_compress_in_task_produces_summary_block_and_checkpoint(
    monkeypatch,
) -> None:
    actions = iter(
        [
            {
                "action": "tool",
                "tool_name": "allowed.tool",
                "arguments": {"query": "ORDER-1"},
            },
            {
                "action": "tool",
                "tool_name": "acp_compress",
                "arguments": {
                    "seq_start": 0,
                    "seq_end": 1,
                    "summary": "已查询 ORDER-1 物流状态。",
                },
            },
            {
                "action": "finish",
                "status": "completed",
                "reply_fragment": "完成。",
                "task_summary": "完成。",
            },
        ]
    )
    _fake_llm(monkeypatch, actions)
    invoked: list[tuple[str, dict[str, object]]] = []

    result = HarnessTaskAgent().run(
        _acp_requirement(),
        _model_config(),
        _tool_invoke(invoked),
        max_actions=3,
        context_compression_mode="acp",
    )

    assert result.status == "completed"
    assert invoked == [("allowed.tool", {"query": "ORDER-1"})]
    transcript = result.loop_checkpoint["transcript"]
    assert len(transcript) == 1
    summary_entry = transcript[0]
    assert summary_entry["role"] == "tool"
    assert summary_entry["tool_name"] == "acp_compress"
    data = summary_entry["result"]["data"]
    assert data["summary"] == "已查询 ORDER-1 物流状态。"
    assert data["checkpoint_id"] == 1
    assert data["summary_block_id"] == 3
    assert data["removed_block_ids"] == [1, 2]
    acp_state = result.loop_checkpoint["acp_state"]
    assert acp_state["blocks"][0]["is_summary"] is True
    assert len(acp_state["checkpoints"]) == 1
    originals = acp_state["checkpoints"][0]["original_blocks"]
    assert len(originals) == 2
    assert "ORDER-1" in originals[1]["content"]
    projected = _transcript_for_model(transcript, acp_mode=True)
    assert projected[0]["tool_name"] == "acp_compress"


def test_acp_decompress_restores_original_entries(monkeypatch) -> None:
    actions = iter(
        [
            {
                "action": "tool",
                "tool_name": "allowed.tool",
                "arguments": {"query": "ORDER-1"},
            },
            {
                "action": "tool",
                "tool_name": "acp_compress",
                "arguments": {
                    "seq_start": 0,
                    "seq_end": 1,
                    "summary": "已查询 ORDER-1 物流状态。",
                },
            },
            {
                "action": "tool",
                "tool_name": "acp_decompress",
                "arguments": {"block_id": 3},
            },
            {
                "action": "finish",
                "status": "completed",
                "reply_fragment": "完成。",
                "task_summary": "完成。",
            },
        ]
    )
    _fake_llm(monkeypatch, actions)

    result = HarnessTaskAgent().run(
        _acp_requirement(),
        _model_config(),
        _tool_invoke(),
        max_actions=4,
        context_compression_mode="acp",
    )

    assert result.status == "completed"
    transcript = result.loop_checkpoint["transcript"]
    assert len(transcript) == 2
    assert transcript[0]["role"] == "assistant"
    assert transcript[0]["tool_name"] == "allowed.tool"
    assert transcript[1]["role"] == "tool"
    assert transcript[1]["tool_name"] == "allowed.tool"
    assert "ORDER-1 已发货" in json.dumps(transcript, ensure_ascii=False)
    acp_state = result.loop_checkpoint["acp_state"]
    assert acp_state["blocks"][0]["is_summary"] is False
    assert len(acp_state["checkpoints"]) == 1


def test_acp_status_returns_compression_stats(monkeypatch) -> None:
    actions = iter(
        [
            {
                "action": "tool",
                "tool_name": "allowed.tool",
                "arguments": {"query": "ORDER-1"},
            },
            {"action": "tool", "tool_name": "acp_status", "arguments": {}},
            {
                "action": "finish",
                "status": "completed",
                "reply_fragment": "完成。",
                "task_summary": "完成。",
            },
        ]
    )
    _fake_llm(monkeypatch, actions)

    result = HarnessTaskAgent().run(
        _acp_requirement(),
        _model_config(),
        _tool_invoke(),
        max_actions=3,
        context_compression_mode="acp",
    )

    assert result.status == "completed"
    status_entry = next(
        entry
        for entry in result.loop_checkpoint["transcript"]
        if entry.get("role") == "tool" and entry.get("tool_name") == "acp_status"
    )
    assert status_entry["result"]["success"] is True
    data = status_entry["result"]["data"]
    assert data["total_blocks"] == 2
    assert data["original_blocks"] == 2
    assert data["summary_blocks"] == 0
    assert data["ledger_balance"] >= 0
    assert len(data["blocks"]) == 2


def test_legacy_preference_excludes_acp_capabilities_and_illegal_call_is_structured(
    monkeypatch,
) -> None:
    actions = iter(
        [
            {
                "action": "tool",
                "tool_name": "acp_compress",
                "arguments": {"seq_start": 0, "seq_end": 1, "summary": "x"},
            },
            {
                "action": "finish",
                "status": "completed",
                "reply_fragment": "完成。",
                "task_summary": "完成。",
            },
        ]
    )
    _fake_llm(monkeypatch, actions)

    result = HarnessTaskAgent().run(
        _acp_requirement(include_acp=False),
        _model_config(),
        _tool_invoke(),
        max_actions=2,
    )

    assert result.status == "completed"
    transcript = result.loop_checkpoint["transcript"]
    assert transcript[0]["tool_name"] == "acp_compress"
    assert transcript[0]["result"]["success"] is False
    assert transcript[0]["result"]["error"]["code"] == "TOOL_NOT_AVAILABLE"
    assert "acp_state" not in result.loop_checkpoint


def test_unknown_tool_name_yields_structured_error(monkeypatch) -> None:
    actions = iter(
        [
            {"action": "tool", "tool_name": "acp_unknown", "arguments": {}},
            {
                "action": "finish",
                "status": "completed",
                "reply_fragment": "完成。",
                "task_summary": "完成。",
            },
        ]
    )
    _fake_llm(monkeypatch, actions)

    result = HarnessTaskAgent().run(
        _acp_requirement(),
        _model_config(),
        _tool_invoke(),
        max_actions=2,
        context_compression_mode="acp",
    )

    assert result.status == "completed"
    transcript = result.loop_checkpoint["transcript"]
    assert transcript[0]["tool_name"] == "acp_unknown"
    assert transcript[0]["result"]["success"] is False
    assert transcript[0]["result"]["error"]["code"] == "TOOL_NOT_AVAILABLE"


def test_acp_search_context_hits_compressed_content_round_trip(monkeypatch) -> None:
    actions = iter(
        [
            {
                "action": "tool",
                "tool_name": "allowed.tool",
                "arguments": {"query": "ORDER-1"},
            },
            {
                "action": "tool",
                "tool_name": "acp_compress",
                "arguments": {
                    "seq_start": 0,
                    "seq_end": 1,
                    "summary": "已查询物流状态。",
                },
            },
            {
                "action": "tool",
                "tool_name": "acp_search_context",
                "arguments": {"query": "ORDER-1"},
            },
            {
                "action": "finish",
                "status": "completed",
                "reply_fragment": "完成。",
                "task_summary": "完成。",
            },
        ]
    )
    _fake_llm(monkeypatch, actions)

    result = HarnessTaskAgent().run(
        _acp_requirement(),
        _model_config(),
        _tool_invoke(),
        max_actions=4,
        context_compression_mode="acp",
    )

    assert result.status == "completed"
    search_entry = next(
        entry
        for entry in result.loop_checkpoint["transcript"]
        if entry.get("role") == "tool" and entry.get("tool_name") == "acp_search_context"
    )
    assert search_entry["result"]["success"] is True
    data = search_entry["result"]["data"]
    assert data["matched"] is True
    assert data["total"] >= 1
    assert data["hits"][0]["source"] == "hidden"
    assert data["hits"][0]["block_id"] == 3


def test_acp_state_round_trips_across_turns(monkeypatch) -> None:
    first_actions = iter(
        [
            {
                "action": "tool",
                "tool_name": "allowed.tool",
                "arguments": {"query": "ORDER-1"},
            },
            {
                "action": "tool",
                "tool_name": "acp_compress",
                "arguments": {
                    "seq_start": 0,
                    "seq_end": 1,
                    "summary": "已查询 ORDER-1 物流状态。",
                },
            },
            {
                "action": "finish",
                "status": "completed",
                "reply_fragment": "完成。",
                "task_summary": "完成。",
            },
        ]
    )
    _fake_llm(monkeypatch, first_actions)
    first = HarnessTaskAgent().run(
        _acp_requirement(),
        _model_config(),
        _tool_invoke(),
        max_actions=3,
        context_compression_mode="acp",
    )
    assert first.loop_checkpoint["transcript"][0]["tool_name"] == "acp_compress"

    second_actions = iter(
        [
            {
                "action": "tool",
                "tool_name": "acp_decompress",
                "arguments": {"block_id": 3},
            },
            {
                "action": "finish",
                "status": "completed",
                "reply_fragment": "完成。",
                "task_summary": "完成。",
            },
        ]
    )
    _fake_llm(monkeypatch, second_actions)
    second = HarnessTaskAgent().run(
        _acp_requirement(),
        _model_config(),
        _tool_invoke(),
        max_actions=2,
        context_compression_mode="acp",
        checkpoint=first.loop_checkpoint,
    )

    assert second.status == "completed"
    transcript = second.loop_checkpoint["transcript"]
    assert len(transcript) == 2
    assert transcript[0]["role"] == "assistant"
    assert transcript[0]["tool_name"] == "allowed.tool"
    assert "ORDER-1 已发货" in json.dumps(transcript, ensure_ascii=False)


def test_acp_compress_invalid_range_returns_structured_error(monkeypatch) -> None:
    actions = iter(
        [
            {
                "action": "tool",
                "tool_name": "acp_compress",
                "arguments": {"seq_start": 0, "seq_end": 99, "summary": "x"},
            },
            {
                "action": "finish",
                "status": "completed",
                "reply_fragment": "完成。",
                "task_summary": "完成。",
            },
        ]
    )
    _fake_llm(monkeypatch, actions)

    result = HarnessTaskAgent().run(
        _acp_requirement(),
        _model_config(),
        _tool_invoke(),
        max_actions=2,
        context_compression_mode="acp",
    )

    assert result.status == "completed"
    transcript = result.loop_checkpoint["transcript"]
    assert transcript[0]["tool_name"] == "acp_compress"
    assert transcript[1]["result"]["success"] is False
    assert transcript[1]["result"]["error"]["code"] == "invalid_range"


def test_acp_compress_invalid_arguments_returns_structured_error(monkeypatch) -> None:
    actions = iter(
        [
            {
                "action": "tool",
                "tool_name": "acp_compress",
                "arguments": {"seq_start": 0, "seq_end": 1},
            },
            {
                "action": "finish",
                "status": "completed",
                "reply_fragment": "完成。",
                "task_summary": "完成。",
            },
        ]
    )
    _fake_llm(monkeypatch, actions)

    result = HarnessTaskAgent().run(
        _acp_requirement(),
        _model_config(),
        _tool_invoke(),
        max_actions=2,
        context_compression_mode="acp",
    )

    assert result.status == "completed"
    transcript = result.loop_checkpoint["transcript"]
    assert transcript[0]["tool_name"] == "acp_compress"
    assert transcript[1]["result"]["success"] is False
    assert transcript[1]["result"]["error"]["code"] == "INVALID_ARGUMENTS"


def test_acp_decompress_unknown_block_returns_structured_error(monkeypatch) -> None:
    actions = iter(
        [
            {
                "action": "tool",
                "tool_name": "acp_decompress",
                "arguments": {"block_id": 999},
            },
            {
                "action": "finish",
                "status": "completed",
                "reply_fragment": "完成。",
                "task_summary": "完成。",
            },
        ]
    )
    _fake_llm(monkeypatch, actions)

    result = HarnessTaskAgent().run(
        _acp_requirement(),
        _model_config(),
        _tool_invoke(),
        max_actions=2,
        context_compression_mode="acp",
    )

    assert result.status == "completed"
    transcript = result.loop_checkpoint["transcript"]
    assert transcript[0]["tool_name"] == "acp_decompress"
    assert transcript[1]["result"]["success"] is False
    assert transcript[1]["result"]["error"]["code"] == "BLOCK_NOT_FOUND"


def test_legacy_transcript_projection_keeps_receipt_path_untouched() -> None:
    transcript = [
        {
            "role": "assistant",
            "action": "tool",
            "tool_name": "allowed.tool",
            "arguments": {"query": "ORDER-1"},
        },
        {
            "role": "tool",
            "tool_name": "allowed.tool",
            "result": {"success": True, "data": {"note": "x" * 500}},
        },
    ]
    # 8 more entries push the first pair beyond the recent-6 window.
    for index in range(4):
        transcript.extend(
            [
                {
                    "role": "assistant",
                    "action": "tool",
                    "tool_name": "allowed.tool",
                    "arguments": {"query": f"ORDER-{index}"},
                },
                {
                    "role": "tool",
                    "tool_name": "allowed.tool",
                    "result": {"success": True, "data": {"note": "y" * 500}},
                },
            ]
        )
    legacy = _transcript_for_model(transcript)
    assert legacy[0]["tool_name"] == "allowed.tool"
    assert "history_receipt" in legacy[1]["result"]
    acp = _transcript_for_model(transcript, acp_mode=True)
    assert acp == transcript
    assert "history_receipt" not in acp[1]["result"]