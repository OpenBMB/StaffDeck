"""决策 JSON 正文预览通道（A 方案）行为验证。

覆盖四层：
1. ``JsonStringFieldStream``：分片、转义、``\\uXXXX``、arm 守卫、结构异常时放弃；
2. ``_reply_preview_sink`` / ``_iteration_reply_preview``：只有「强制能力已满足」
   的决策调用才启用预览，避免把注定作废的文本推给用户；
3. ``TurnStreamSink.reconcile``：收尾对账——一致不产生事件，不一致原地替换；
4. ``LLMClient.generate_json(preview_sink=...)``：首次尝试走流式候选并把原始增量
   透传给预览通道，repair 重试回落到阻塞候选。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.engine.facade import TurnStreamSink
from app.core.engine.step_loop import (
    _generate_harness_action_json,
    _iteration_reply_preview,
    _reply_preview_sink,
)
from app.core.base.request_compiler import TaskRequirement
from app.llm.client import LLMClient, LLMError
from app.llm.json_field_stream import JsonStringFieldStream

# --------------------------------------------------------------------------- #
# 1. 增量字段提取器
# --------------------------------------------------------------------------- #


def _drain(stream: JsonStringFieldStream, chunks: list[str]) -> str:
    return "".join(stream.feed(chunk) for chunk in chunks)


def test_preview_extracts_reply_fragment_across_arbitrary_chunks():
    text = '{"action": "finish", "status": "completed", "reply_fragment": "报销标准是每人每天 500 元。", "task_summary": "t"}'
    stream = JsonStringFieldStream("reply_fragment", arm_marker="finish")

    # 逐字符喂：任何分片边界都不能影响结果。
    assert _drain(stream, list(text)) == "报销标准是每人每天 500 元。"
    assert stream.finished is True


def test_preview_decodes_escapes_and_unicode_escape():
    text = (
        r'{"action":"finish","reply_fragment":"第一行\n第二行\t\"引用\"\\反斜杠\u4e2d\u6587"}'
    )
    stream = JsonStringFieldStream("reply_fragment", arm_marker="finish")

    assert _drain(stream, [text]) == '第一行\n第二行\t"引用"\\反斜杠中文'


def test_preview_holds_back_split_escape_until_next_chunk():
    stream = JsonStringFieldStream("reply_fragment", arm_marker="finish")
    chunks = ['{"action":"finish","reply_fragment":"正文\\', 'n继续"}']

    assert stream.feed(chunks[0]) == "正文"
    assert stream.feed(chunks[1]) == "\n继续"


def test_preview_holds_back_lone_high_surrogate():
    stream = JsonStringFieldStream("reply_fragment", arm_marker="finish")

    assert stream.feed(r'{"action":"finish","reply_fragment":"\ud83d') == ""
    # 低位代理到达后两个半体一起下发，前端不会拿到非法字符。
    assert stream.feed(r'\ude00 结尾"}') == "\U0001f600 结尾"


def test_preview_requires_arm_marker_so_tool_actions_never_leak_text():
    # 工具动作里偶然出现同名字段：没有 finish 标记就不该吐任何字。
    text = '{"action": "tool", "arguments": {"reply_fragment": "不该出现的文本"}}'
    stream = JsonStringFieldStream("reply_fragment", arm_marker="finish")

    assert _drain(stream, [text]) == ""


def test_preview_waits_for_arm_marker_split_across_chunks():
    stream = JsonStringFieldStream("reply_fragment", arm_marker="finish")
    chunks = ['{"action": "fin', 'ish", "reply_fragment": "答案"}']
    assert _drain(stream, chunks) == "答案"


def test_preview_stops_at_closing_quote_and_ignores_later_fields():
    stream = JsonStringFieldStream("reply_fragment", arm_marker="finish")
    text = (
        '{"action":"finish","reply_fragment":"答案",'
        '"task_summary":"reply_fragment 之后的内容不能混进来"}'
    )

    assert _drain(stream, [text]) == "答案"


def test_preview_gives_up_quietly_on_malformed_structure():
    stream = JsonStringFieldStream("reply_fragment", arm_marker="finish")

    assert _drain(stream, ['{"action":"finish","reply_fragment": 123}']) == ""
    assert stream.dead is True
    # 放弃之后不再有任何输出（也不会抛异常）。
    assert stream.feed('"后续垃圾"') == ""


# --------------------------------------------------------------------------- #
# 2. 预览通道的启用条件
# --------------------------------------------------------------------------- #


def _requirement(*, capabilities: list[str], knowledge: list[str] | None = None):
    return TaskRequirement(
        task_frame_id="frame-1",
        kind="conversation",
        goal="回答报销标准",
        required_capability_names=capabilities,
        required_knowledge_base_ids=knowledge or [],
    )


def test_iteration_preview_is_disabled_while_required_capability_missing():
    emitted: list[str] = []
    preview = _iteration_reply_preview(
        _requirement(capabilities=["knowledge_search"]),
        [],
        set(),
        emitted.append,
    )

    assert preview is None


def test_iteration_preview_is_enabled_once_required_capability_satisfied():
    emitted: list[str] = []
    preview = _iteration_reply_preview(
        _requirement(capabilities=["knowledge_search"]),
        [{"tool_name": "knowledge_search", "success": True}],
        set(),
        emitted.append,
    )

    assert preview is not None
    preview('{"action":"finish","reply_fragment":"答案"}')
    assert "".join(emitted) == "答案"


def test_iteration_preview_requires_required_knowledge_base():
    requirement = _requirement(capabilities=[], knowledge=["kb-1"])
    emitted: list[str] = []

    assert _iteration_reply_preview(requirement, [], set(), emitted.append) is None
    assert (
        _iteration_reply_preview(requirement, [], {"kb-1"}, emitted.append) is not None
    )


def test_iteration_preview_is_none_without_stream_consumer():
    assert (
        _iteration_reply_preview(
            _requirement(capabilities=[]),
            [],
            set(),
            None,
        )
        is None
    )


def test_harness_action_json_forwards_preview_sink_when_supported():
    seen: list[str] = []

    class _SequenceClient:
        def generate_json_sequence(
            self, system_prompt, payload, cancellation=None, *, preview_sink=None
        ):
            for chunk in ['{"action":"fin', 'ish","reply_fragment":"答案"}']:
                if preview_sink is not None:
                    preview_sink(chunk)
            return {"action": "finish", "reply_fragment": "答案"}

    raw = _generate_harness_action_json(
        _SequenceClient(),
        "sys",
        {},
        preview_sink=_reply_preview_sink(seen.append),
    )

    assert raw == {"action": "finish", "reply_fragment": "答案"}
    assert "".join(seen) == "答案"


def test_harness_action_json_omits_preview_kwarg_for_legacy_clients():
    class _LegacyClient:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def generate_json(self, system_prompt, payload):
            self.calls.append((system_prompt, payload))
            return {"action": "finish"}

    client = _LegacyClient()
    assert _generate_harness_action_json(client, "sys", {}, preview_sink=None) == {
        "action": "finish"
    }
    assert client.calls == [("sys", {})]


# --------------------------------------------------------------------------- #
# 3. 收尾对账
# --------------------------------------------------------------------------- #


def _bound_sink():
    recorded: list[tuple[str, dict]] = []
    owner = SimpleNamespace(
        events=SimpleNamespace(
            record=lambda tenant_id, session_id, event_type, payload: recorded.append(
                (event_type, dict(payload))
            )
        ),
        db=SimpleNamespace(commit=lambda: None),
    )
    sink = TurnStreamSink(owner)
    sink.bind("tenant-1", "session-1")
    return sink, recorded


def test_reconcile_is_silent_when_final_text_matches():
    sink, recorded = _bound_sink()
    sink.delta("预览")
    sink.delta("正文")
    recorded.clear()

    sink.reconcile("预览正文")

    assert recorded == []


def test_reconcile_counts_buffered_fragments_as_streamed():
    sink, recorded = _bound_sink()

    # 首片立即下发，后续碎片还压在缓冲里——对账必须把它们算进来，
    # 否则会在收尾时把一模一样的正文再 replace 一遍。
    sink.delta("甲")
    sink.delta("乙")
    recorded.clear()

    sink.reconcile("甲乙")

    assert recorded == []


def test_reconcile_replaces_when_final_text_differs():
    sink, recorded = _bound_sink()
    sink.delta("预览里的旧正文")
    recorded.clear()

    sink.reconcile("最终正文（引用编号已重排）")

    assert recorded == [
        ("stream_replace", {"content": "最终正文（引用编号已重排）"})
    ]
    assert sink.streamed is True


def test_reconcile_is_noop_when_nothing_was_streamed():
    sink, recorded = _bound_sink()

    sink.reconcile("最终正文")

    assert recorded == []


def test_reset_on_empty_sink_emits_nothing():
    sink, recorded = _bound_sink()

    sink.reset()

    assert recorded == []


# --------------------------------------------------------------------------- #
# 4. LLMClient 侧接线
# --------------------------------------------------------------------------- #


def _client_with_scripts(monkeypatch, script: list[str]):
    """不构造 SDL 客户端的 LLMClient：只保留本用例要验证的分支。"""

    client = object.__new__(LLMClient)
    calls: dict[str, object] = {"stream": 0, "text": 0}

    def fake_stream(system_prompt, payload, cancellation=None, *, response_format=None):
        calls["stream"] = int(calls["stream"]) + 1
        calls["stream_response_format"] = response_format
        yield from script

    def fake_text(system_prompt, payload, response_format=None, cancellation=None):
        calls["text"] = int(calls["text"]) + 1
        calls["text_response_format"] = response_format
        return "".join(script)

    monkeypatch.setattr(client, "generate_text_stream", fake_stream)
    monkeypatch.setattr(client, "generate_text", fake_text)
    return client, calls


def test_generate_json_preview_streams_raw_deltas_and_parses_same_result(monkeypatch):
    script = [
        '{"action": "finish", "status": "completed", ',
        '"reply_fragment": "第一行\\n第二行", "task_summary": "t"}',
    ]
    client, calls = _client_with_scripts(monkeypatch, script)
    seen: list[str] = []

    parsed = client.generate_json(
        "sys", {"conversation_context": {}}, preview_sink=seen.append
    )

    assert parsed["reply_fragment"] == "第一行\n第二行"
    assert calls["stream"] == 1
    assert calls["text"] == 0
    # 请求体必须与阻塞候选同构：JSON 模式参数要透传，否则预览读到的是另一种输出。
    assert calls["stream_response_format"] == {"type": "json_object"}
    # 原始增量（而非提取结果）逐段透传，由预览通道决定怎么用。
    assert "".join(seen) == "".join(script)


def test_generate_json_without_preview_sink_uses_blocking_candidate(monkeypatch):
    client, calls = _client_with_scripts(monkeypatch, ['{"action": "finish"}'])

    parsed = client.generate_json("sys", {"conversation_context": {}})

    assert parsed == {"action": "finish"}
    assert calls["stream"] == 0
    assert calls["text"] == 1


def test_generate_json_repair_attempt_falls_back_to_blocking_candidate(monkeypatch):
    # 首次输出不是合法 JSON：repair 重试属于「作废那一版」，不能再喂预览通道。
    client, calls = _client_with_scripts(monkeypatch, ["不是 JSON"])
    seen: list[str] = []

    with pytest.raises(LLMError):
        client.generate_json(
            "sys", {"conversation_context": {}}, preview_sink=seen.append
        )

    assert calls["stream"] == 1
    assert int(calls["text"]) >= 1
