import json
from types import SimpleNamespace as NS

import pytest

from app.core.reply_stream import PublicTextProjection, ReplyStream, reconcile_reply
from staffdeck_harness.bridge.phases import EnginePhaseRunner
from staffdeck_harness.bridge.task_agent import HarnessV3TaskAgent
from staffdeck_harness.runtime.completion import native_result


@pytest.mark.parametrize("phase,authorized", [("plan", False), ("plan", True), ("reply", False), ("reply", True)])
def test_phase_text_never_uses_the_audit_public_reply_route(monkeypatch, phase, authorized):
    from staffdeck_harness.bridge import session_runner

    audit, public = [], []

    def run(*args, **kwargs):
        trace = args[4]
        trace("model_text_delta", {"content": "first"})
        trace("harness_v3_turn_ended", {"reason": "stop"})
        trace("model_text_delta", {"content": "second"})
        assert public == (["first", "second"] if authorized and phase == "reply" else [])
        return [], "firstsecond", "stop"

    monkeypatch.setattr(session_runner, "run_session", run)
    runner = EnginePhaseRunner(
        NS(registry=NS(rebind=lambda *a: None)), NS(token="t", process=None),
        tenant_id="t1", session_id="s1", trace=lambda e, p: audit.append(e), cancelled=lambda: False,
    )
    assert runner.prompt(
        phase=phase, model_config=None, system_text="system", user_text="input",
        engine_session="engine", on_text=public.append if authorized else None,
    ) == "firstsecond"
    assert "model_text_delta" not in audit and "stream_delta" not in audit


@pytest.mark.parametrize("status", ["completed", "awaiting_user", "handoff", "failed"])
@pytest.mark.parametrize("width", [1, 7, 1000])
def test_native_control_is_never_partially_published(status, width):
    events = []
    sink = ReplyStream(lambda e, p: events.append((e, p)))
    agent = HarnessV3TaskAgent.__new__(HarnessV3TaskAgent)
    agent.turn = NS(live_stream=True, stream_sink=sink)
    trace = agent._client_stream_trace(lambda *a: None, NS(kind="conversation"))
    text = json.dumps({"action": "finish", "status": status, "reply_fragment": '请补充“来源”。\n第二行', "slot_updates": {}}, ensure_ascii=False)
    for offset in range(0, len(text), width):
        trace("model_text_delta", {"content": text[offset:offset + width]})
        assert events == []
    final = native_result(text)["reply_fragment"]
    reconcile_reply(sink, final)
    reconcile_reply(sink, final)
    assert events == [("stream_delta", {"content": final})]
    assert agent._streamed_parts == []


def test_plain_text_streams_before_finish_and_rewrite_replaces_once():
    events = []
    sink = ReplyStream(lambda e, p: events.append((e, p)))
    projection = PublicTextProjection(sink.on_delta)
    projection.feed(" ")
    assert not events
    projection.feed("你好")
    assert sink.text == " 你好"
    projection.feed("你好")  # repeated tokens are meaningful, not snapshots
    assert sink.text == " 你好你好"
    reconcile_reply(sink, "修正后的回复")
    reconcile_reply(sink, "修正后的回复")
    assert events[-1] == ("stream_replace", {"content": "修正后的回复"})
    assert len(events) == 3


@pytest.mark.parametrize("text", ['{"example": 1}', '```json\n{"example": 1}\n```'])
def test_user_requested_json_and_code_are_preserved(text):
    events = []
    sink = ReplyStream(lambda e, p: events.append((e, p)))
    projection = PublicTextProjection(sink.on_delta)
    for char in text:
        projection.feed(char)
    assert not events
    assert native_result(text) is None
    reconcile_reply(sink, text)
    assert sink.text == text


@pytest.mark.parametrize("authorized", [False, True])
def test_synthesis_streaming_is_explicit_and_precedes_final_normalization(monkeypatch, authorized):
    from staffdeck_harness.runtime import model_phases

    public = []
    monkeypatch.setattr(model_phases, "stage_prompt_text", lambda p: "stage")

    def prompt(**kwargs):
        callback = kwargs.get("on_text")
        if callback:
            callback("初始")
            callback("回复")
        assert public == (["初始", "回复"] if authorized else [])
        return "初始回复"

    v2 = NS(direct_reply=lambda *a: None, prepare_payload=lambda *a: {},
            normalize_reply=lambda text, *a: "监管前规范化结果")
    generator = model_phases.EngineResponseGenerator(v2, NS(prompt=prompt), engine_session="s")
    final = generator.generate_with_stream(*([None] * 11), on_delta=public.append if authorized else None)
    assert final == "监管前规范化结果"
