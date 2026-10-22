"""B 方案（半流式）行为验证：执行期状态 + 生成期真流式。

覆盖四件事：
1. ``TurnStreamSink`` 的投递语义（绑定前静默、60ms/400 字节流、reset/replace）；
2. ``_stream_status_for_trace`` 把 harness trace 事件翻译成等待期状态的映射；
3. ``_direct_reply_answer`` 与 ``_direct_reply_answer_stream`` 请求体一致；
4. ``ResponseGenerator.generate_stream`` 在「已下发部分正文后失败」不再追加报错，
   由 ``HarnessV2Engine._synthesize_reply`` 原地换成报错文案（不会拼出脏文本）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.llm as llm_package
from app.core.engine import turn as engine_module
from app.core.converse import response as response_generator_module
from app.core.engine.facade import AgentLoop, TurnStreamSink
from app.core.engine.turn import (
    HarnessV2Engine,
    _direct_reply_answer,
    _direct_reply_answer_stream,
    _stream_status_for_trace,
)
from app.core.converse.response import ResponseGenerator
from app.db.models import ChatSession, ModelConfig
from app.llm import client as llm_client_module
from app.session.session_schema import (
    ChatTurnRequest,
    ChatTurnResponse,
    RouterDecision,
    SessionPublic,
    StepAgentResult,
)


class _RecordedSink:
    """HarnessV2Engine 侧的最小 sink 替身：只记录调用。"""

    def __init__(self) -> None:
        self.statuses: list[tuple[str, str, dict]] = []
        self.deltas: list[str] = []
        self.replaced: list[str] = []
        self.resets = 0

    def status(self, phase: str, text: str, **extra: object) -> None:
        self.statuses.append((phase, text, dict(extra)))

    def delta(self, chunk: str) -> None:
        self.deltas.append(chunk)

    def replace(self, content: str) -> None:
        self.replaced.append(content)

    def reset(self) -> None:
        self.resets += 1


class _StubLLMClient:
    """替换 LLMClient：按脚本产出流式增量。"""

    script: ClassVar[list[str]] = []
    error: ClassVar[Exception | None] = None
    captured: ClassVar[list[dict]] = []

    def __init__(self, model_config: object) -> None:
        self.model_config = model_config

    def generate_text(self, system_prompt: str, payload: object) -> str:
        _StubLLMClient.captured.append({"system": system_prompt, "payload": payload})
        if _StubLLMClient.error is not None:
            raise _StubLLMClient.error
        return "".join(_StubLLMClient.script)

    def generate_text_stream(self, system_prompt: str, payload: object):
        _StubLLMClient.captured.append({"system": system_prompt, "payload": payload})
        yield from _StubLLMClient.script
        if _StubLLMClient.error is not None:
            raise _StubLLMClient.error


@pytest.fixture(autouse=True)
def _reset_stub_llm():
    _StubLLMClient.script = []
    _StubLLMClient.error = None
    _StubLLMClient.captured = []
    yield


@pytest.fixture
def stub_llm(monkeypatch):
    # 三个引用点都要换：`app.llm.LLMClient`（harness 内的局部 import）、
    # `app.llm.client.LLMClient`（同类引用）、`app.core.converse.response.LLMClient`
    # （模块级 import 已把名字绑进该模块命名空间）。
    monkeypatch.setattr(llm_package, "LLMClient", _StubLLMClient)
    monkeypatch.setattr(llm_client_module, "LLMClient", _StubLLMClient)
    monkeypatch.setattr(response_generator_module, "LLMClient", _StubLLMClient)
    return _StubLLMClient


@pytest.fixture
def loop_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _fake_owner():
    recorded: list[tuple[str, str, str, dict]] = []
    commits: list[int] = []
    owner = SimpleNamespace(
        events=SimpleNamespace(
            record=lambda tenant_id, session_id, event_type, payload: recorded.append(
                (tenant_id, session_id, event_type, dict(payload))
            )
        ),
        db=SimpleNamespace(commit=lambda: commits.append(1)),
    )
    return owner, recorded, commits


# --------------------------------------------------------------------------- #
# 1. TurnStreamSink
# --------------------------------------------------------------------------- #


def test_sink_is_silent_until_bound():
    owner, recorded, commits = _fake_owner()
    sink = TurnStreamSink(owner)

    sink.status("planning", "正在规划本轮任务")
    sink.delta("你好")
    sink.flush()
    sink.reset()

    assert recorded == []
    assert commits == []
    assert sink.active is False
    assert sink.streamed is False


def test_sink_emits_status_and_first_delta_immediately():
    owner, recorded, commits = _fake_owner()
    sink = TurnStreamSink(owner)
    sink.bind("tenant-1", "session-1")

    sink.status("preparing", "正在生成回复")
    sink.delta("你")

    assert [item[2] for item in recorded] == ["stream_status", "stream_delta"]
    assert recorded[0][0] == "tenant-1"
    assert recorded[0][3] == {"phase": "preparing", "text": "正在生成回复"}
    assert recorded[1][3] == {"content": "你"}
    assert len(commits) == len(recorded)
    assert sink.streamed is True


def test_sink_batches_fragments_arriving_within_flush_interval():
    owner, recorded, commits = _fake_owner()
    sink = TurnStreamSink(owner)
    sink.bind("tenant-1", "session-1")

    # 首片立即下发，随后的碎片落在 60ms 节流窗口内 → 先攒着不写库。
    sink.delta("第一片")
    for _ in range(20):
        sink.delta("碎")
    assert len(recorded) == 1

    sink.flush()
    # 攒下的碎片合成一条增量，而不是 20 条。
    assert [item[2] for item in recorded] == ["stream_delta", "stream_delta"]
    assert recorded[1][3] == {"content": "碎" * 20}
    assert commits == [1, 1]


def test_sink_forces_flush_when_pending_exceeds_max_chars():
    owner, recorded, _ = _fake_owner()
    sink = TurnStreamSink(owner)
    sink.bind("tenant-1", "session-1")

    sink.delta("x" * 10)
    assert len(recorded) == 1
    # 超过合并上限必须立刻落库，避免长回复长时间不出现在界面上。
    sink.delta("y" * 500)
    assert len(recorded) == 2
    assert recorded[1][3] == {"content": "y" * 500}


def test_sink_reset_clears_streamed_flag_so_caller_may_republish():
    owner, recorded, _ = _fake_owner()
    sink = TurnStreamSink(owner)
    sink.bind("tenant-1", "session-1")
    sink.delta("半截正文")
    assert sink.streamed is True

    sink.reset()

    assert sink.streamed is False
    assert recorded[-1][2] == "stream_replace"
    assert recorded[-1][3] == {"content": ""}
    # reset 后重新生成的内容仍会正常下发。
    sink.delta("新的正文")
    assert sink.streamed is True


def test_sink_replace_keeps_streamed_flag_and_rewrites_content():
    owner, recorded, _ = _fake_owner()
    sink = TurnStreamSink(owner)
    sink.bind("tenant-1", "session-1")
    sink.delta("半截正文")

    sink.replace("模型调用失败：已中断。")

    assert recorded[-1][2] == "stream_replace"
    assert recorded[-1][3] == {"content": "模型调用失败：已中断。"}
    assert sink.streamed is True


# --------------------------------------------------------------------------- #
# 2. trace → stream_status 映射
# --------------------------------------------------------------------------- #


def test_trace_status_maps_tool_call():
    status = _stream_status_for_trace(
        "harness_action_created",
        {"iteration": 1, "action": "tool", "tool_name": "query_order"},
    )
    assert status == {
        "phase": "preparing",
        "text": "正在调用工具 query_order",
        "tool_name": "query_order",
    }


def test_trace_status_skips_finish_action():
    # finish 动作没有等待意义，不该多出一条滚动状态。
    assert (
        _stream_status_for_trace(
            "harness_action_created",
            {"iteration": 2, "action": "finish", "tool_name": None},
        )
        is None
    )


@pytest.mark.parametrize(
    "event_type",
    [
        "knowledge_source_read",
        "knowledge_payload_compacted",
        "capability_search_completed",
        "harness_action_model_retry",
    ],
)
def test_trace_status_maps_known_waiting_phases(event_type):
    status = _stream_status_for_trace(event_type, {})
    assert status is not None
    # phase 必须落在前端既有词表里，否则前端渲染不出可读文案。
    assert status["phase"] == "preparing"
    assert status["text"]


def test_trace_status_ignores_unrelated_events():
    assert _stream_status_for_trace("task_frame_started", {}) is None
    assert _stream_status_for_trace("harness_tool_completed", {"tool_name": "x"}) is None


# --------------------------------------------------------------------------- #
# 3. direct_reply 的流式/非流式同源
# --------------------------------------------------------------------------- #


def test_direct_reply_stream_uses_same_payload_as_blocking_variant(stub_llm):
    stub_llm.script = ["你好", "，很高兴", "见到你"]

    streamed = "".join(
        _direct_reply_answer_stream(
            "hi",
            "你是客服助手",
            [{"id": "m1", "content": "用户偏好中文"}],
            {"history": []},
            ModelConfig(name="m", model="gpt-x"),
            capability_summary="能力清单",
        )
    )
    blocking = _direct_reply_answer(
        "hi",
        "你是客服助手",
        [{"id": "m1", "content": "用户偏好中文"}],
        {"history": []},
        ModelConfig(name="m", model="gpt-x"),
        capability_summary="能力清单",
    )

    assert streamed == "你好，很高兴见到你"
    assert blocking == streamed
    # 两次调用的请求体必须完全一致：流式只是换了交付方式，没有换语义。
    assert len(stub_llm.captured) == 2
    assert stub_llm.captured[0]["payload"] == stub_llm.captured[1]["payload"]


# --------------------------------------------------------------------------- #
# 4. 生成中途失败的收尾口径
# --------------------------------------------------------------------------- #


def _response_generator_args() -> tuple:
    session = ChatSession(tenant_id="t1", id="s1")
    return (
        "你好",
        session,
        None,
        RouterDecision(decision="answer_only"),
        StepAgentResult(action="reply"),
        None,
        ModelConfig(name="m", model="gpt-x"),
    )


def test_generate_stream_reraises_instead_of_appending_error_text(stub_llm):
    stub_llm.script = ["已经吐出一段正文"]
    stub_llm.error = RuntimeError("连接中断")

    generator = ResponseGenerator().generate_stream(*_response_generator_args())

    assert next(generator) == "已经吐出一段正文"
    with pytest.raises(RuntimeError):
        next(generator)


def test_generate_stream_still_reports_error_when_nothing_streamed(stub_llm):
    stub_llm.script = []
    stub_llm.error = RuntimeError("连接中断")

    chunks = list(ResponseGenerator().generate_stream(*_response_generator_args()))

    assert chunks
    assert "模型调用失败" in "".join(chunks)


def _engine_with_stub(loop_db, sink) -> HarnessV2Engine:
    owner = SimpleNamespace(
        db=loop_db,
        events=SimpleNamespace(record=lambda *args, **kwargs: None),
        response_generator=ResponseGenerator(),
        _get_persona_prompt=lambda *args, **kwargs: None,
    )
    engine = HarnessV2Engine(owner, turn_stream=sink)
    return engine


def _synthesize(engine: HarnessV2Engine, session: ChatSession) -> str:
    request = SimpleNamespace(tenant_id="t1", user_id="u1")
    execution_request = SimpleNamespace(message="你好")
    return engine._synthesize_reply(
        request,
        execution_request,
        session,
        None,
        RouterDecision(decision="answer_only"),
        StepAgentResult(action="reply"),
        ModelConfig(name="m", model="gpt-x"),
        [],
        {"history": []},
        [],
    )


def test_synthesize_reply_uses_blocking_generate_without_live_deltas(loop_db, stub_llm):
    stub_llm.script = ["甲", "乙", "丙"]
    sink = _RecordedSink()
    engine = _engine_with_stub(loop_db, sink)
    session = ChatSession(tenant_id="t1", id="s1")

    reply = _synthesize(engine, session)

    assert reply == "甲乙丙"
    # 2026-10-19：综合生成不再走预览通道下发增量，正文由终局 8字/45ms 匀速回放。
    assert sink.deltas == []
    assert sink.statuses == [("preparing", "正在生成回复", {})]
    assert sink.resets == 0


def test_synthesize_reply_returns_failure_without_streaming_partial(loop_db, stub_llm):
    stub_llm.script = ["半截正文"]
    stub_llm.error = RuntimeError("连接中断")
    sink = _RecordedSink()
    engine = _engine_with_stub(loop_db, sink)
    session = ChatSession(tenant_id="t1", id="s1")

    reply = _synthesize(engine, session)

    assert "模型调用失败" in reply
    assert "半截正文" not in reply
    # 非流式生成：没有任何增量下发，也就不需要原地替换。
    assert sink.deltas == []
    assert sink.replaced == []


def test_synthesize_reply_uses_blocking_generate_without_sink(loop_db, stub_llm):
    stub_llm.script = ["整段直出"]
    engine = _engine_with_stub(loop_db, None)

    reply = _synthesize(engine, ChatSession(tenant_id="t1", id="s1"))

    assert reply == "整段直出"


def test_engine_trace_status_is_noop_without_sink(loop_db):
    engine = _engine_with_stub(loop_db, None)
    # 非流式轮次：任何等待状态都不该产生副作用。
    engine._emit_stream_status("preparing", "正在生成回复")
    engine._emit_stream_delta("x")
    engine._trace_stream_status("harness_action_created", {"tool_name": "t"})


def test_engine_translates_trace_events_into_waiting_status(loop_db):
    sink = _RecordedSink()
    engine = _engine_with_stub(loop_db, sink)

    engine._trace_stream_status(
        "harness_action_created", {"action": "tool", "tool_name": "query_order"}
    )
    engine._trace_stream_status("knowledge_source_read", {"requested": 3})
    engine._trace_stream_status("task_frame_started", {})

    assert [item[0] for item in sink.statuses] == ["preparing", "preparing"]
    assert sink.statuses[0][1] == "正在调用工具 query_order"
    assert sink.statuses[0][2] == {"tool_name": "query_order"}
    assert sink.statuses[1][1] == "读取知识来源"


def test_trace_status_does_not_leak_phase_keys_into_extra():
    # 回归：phase/text 是 status 的定位字段，必须从 extra 里摘掉，
    # 否则前端 publicStreamPhase 会拿到重复键。
    status = _stream_status_for_trace(
        "knowledge_payload_compacted", {"query": "报销标准"}
    )
    assert status is not None
    sink = _RecordedSink()
    phase = str(status.pop("phase"))
    text = str(status.pop("text"))
    sink.status(phase, text, **status)
    assert sink.statuses == [("preparing", "整理知识证据", {"query": "报销标准"})]
    assert engine_module._stream_status_for_trace is _stream_status_for_trace


# --------------------------------------------------------------------------- #
# 5. 流式轮次的编排：sink 生命周期 + 不重复下发
# --------------------------------------------------------------------------- #


def _stream_events(loop_db, response, *, streamed: bool):
    """驱动 _handle_turn_stream_v2 的事件序列（handle_turn 已被替换）。"""

    def fake_handle_turn(self, request):
        if streamed:
            self.turn_stream.status("preparing", "正在生成回复")
            self.turn_stream.delta(response.reply)
        return response

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(AgentLoop, "handle_turn", fake_handle_turn)
    try:
        session = ChatSession(tenant_id="t1", id="s1", user_id="u1")
        loop_db.add(session)
        loop_db.commit()
        loop = AgentLoop(loop_db)
        request = ChatTurnRequest(
            tenant_id="t1",
            session_id="s1",
            user_id="u1",
            message="你好",
            client_turn_id="ctid-1",
        )
        events = list(loop.handle_turn_stream(request))
        # 事件循环结束后 sink 必须摘掉，避免后续复用 AgentLoop 时误投递。
        assert loop.turn_stream is None
        return events
    finally:
        monkeypatch.undo()


def test_stream_path_does_not_republish_already_streamed_reply(loop_db):
    response = ChatTurnResponse(
        reply="真流式的正文",
        session_id="s1",
        session_state=SessionPublic(session_id="s1", tenant_id="t1"),
    )
    events = _stream_events(loop_db, response, streamed=True)

    assert [item["event"] for item in events] == [
        "user_message_received",
        "status",
        "stream_end",
        "complete",
    ]
    # 正文已随生成实时下发：终局路径不该再补一遍 stream_delta。
    assert response.reply not in {item["data"].get("content") for item in events}


def test_stream_path_reconciles_previewed_text_with_final_reply(loop_db):
    """预览通道先吐出的可能是未重排引用编号的草稿：收尾必须换成最终正文。"""

    response = ChatTurnResponse(
        reply="报销标准是每天 500 元 [1]。",
        session_id="s1",
        session_state=SessionPublic(session_id="s1", tenant_id="t1"),
    )
    relayed: list[tuple[str, dict]] = []

    def fake_handle_turn(self, request):
        self.turn_stream.delta("报销标准是每天 500 元。")
        return response

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(AgentLoop, "handle_turn", fake_handle_turn)
    try:
        session = ChatSession(tenant_id="t1", id="s1", user_id="u1")
        loop_db.add(session)
        loop_db.commit()
        loop = AgentLoop(
            loop_db,
            event_sink=lambda kind, payload: relayed.append((kind, dict(payload))),
        )
        request = ChatTurnRequest(
            tenant_id="t1",
            session_id="s1",
            user_id="u1",
            message="你好",
            client_turn_id="ctid-1",
        )
        events = list(loop.handle_turn_stream(request))
    finally:
        monkeypatch.undo()

    assert [item["event"] for item in events] == [
        "user_message_received",
        "status",
        "stream_end",
        "complete",
    ]
    # 已下发的草稿与最终正文不一致 → relay 里必须补一条 stream_replace 校准，
    # 否则前端会停在缺引用编号的版本上。
    replaced = [payload for kind, payload in relayed if kind == "stream_replace"]
    assert replaced == [{"content": response.reply}]
    assert [
        payload["content"] for kind, payload in relayed if kind == "stream_delta"
    ] == ["报销标准是每天 500 元。"]


def test_stream_path_chunks_reply_when_nothing_was_streamed(loop_db):
    response = ChatTurnResponse(
        reply="x" * 40,
        session_id="s1",
        session_state=SessionPublic(session_id="s1", tenant_id="t1"),
    )
    events = _stream_events(loop_db, response, streamed=False)

    assert [item["event"] for item in events] == [
        "user_message_received",
        "status",
        "stream_delta",
        "stream_delta",
        "stream_delta",
        "stream_delta",
        "stream_delta",
        "stream_end",
        "complete",
    ]
    assert "".join(
        str(item["data"]["content"])
        for item in events
        if item["event"] == "stream_delta"
    ) == response.reply

