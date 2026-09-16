from __future__ import annotations

import hashlib
import time
from types import SimpleNamespace

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.channels.adapters.dingtalk import DingTalkPermanentError, DingTalkTransientError
from app.channels.dingtalk_trace import DingTalkTraceStreamer, is_dingtalk_trace_enabled
from app.db.models import Skill, Tenant, Tool


def _binding(channel: str = "dingtalk", config: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id="chan_dingtalk",
        tenant_id="tenant_a",
        channel=channel,
        config_json=config if config is not None else {},
    )


def _target() -> dict:
    return {
        "to_user_id": "staff-1",
        "conversation_id": "cid-1",
        "conversation_type": "1",
        "session_webhook": "https://oapi.dingtalk.com/robot/sendBySession?session=x",
        "message_id": "msg-1",
    }


class FakeAdapter:
    def __init__(
        self,
        *,
        fail_create: bool = False,
        fail_update: bool = False,
        fail_stream: bool = False,
        transient_create_failures: int = 0,
    ) -> None:
        self.create_calls: list[dict] = []
        self.update_calls: list[dict] = []
        self.stream_calls: list[dict] = []
        self.fail_create = fail_create
        self.fail_update = fail_update
        self.fail_stream = fail_stream
        self.transient_create_failures = transient_create_failures

    def create_card(self, binding, target, card_data, *, idempotency_key) -> str:
        self.create_calls.append(
            {"binding": binding, "target": target, "card": card_data, "key": idempotency_key}
        )
        if self.transient_create_failures > 0:
            self.transient_create_failures -= 1
            raise DingTalkTransientError("network down")
        if self.fail_create:
            raise DingTalkPermanentError("create failed")
        return "ot_" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]

    def update_card(self, binding, card_id, card_data) -> None:
        self.update_calls.append({"binding": binding, "card_id": card_id, "card": card_data})
        if self.fail_update:
            raise DingTalkPermanentError("update failed")

    def stream_card(
        self,
        binding,
        card_id,
        key,
        content,
        *,
        finalize: bool = False,
        failed: bool = False,
    ) -> None:
        self.stream_calls.append(
            {
                "binding": binding,
                "card_id": card_id,
                "key": key,
                "content": content,
                "finalize": finalize,
                "failed": failed,
            }
        )
        if self.fail_stream:
            raise DingTalkPermanentError("stream failed")


def _make_streamer(
    *,
    adapter: FakeAdapter | None = None,
    min_update_interval: float = 0.0,
    compact_sop: bool = False,
    skill_names: dict | None = None,
    step_names: dict | None = None,
    tool_names: dict | None = None,
    binding=None,
    card_retry_delay: float = 0.0,
    user_message: str | None = None,
) -> DingTalkTraceStreamer:
    return DingTalkTraceStreamer(
        binding or _binding(),
        _target(),
        "turn_1",
        adapter=adapter or FakeAdapter(),
        skill_names=skill_names,
        step_names=step_names,
        tool_names=tool_names,
        min_update_interval=min_update_interval,
        compact_sop=compact_sop,
        card_retry_delay=card_retry_delay,
        user_message=user_message,
    )


def _wait_for_card(streamer: DingTalkTraceStreamer, timeout: float = 1.0) -> None:
    """等待后台 worker 完成卡片创建（无论成功或失败）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if streamer._card_created:
            return
        time.sleep(0.005)


def _wait_for_updates(adapter: FakeAdapter, count: int, timeout: float = 2.0) -> None:
    """等待流式内容推送（stream_card）达到指定次数。"""
    deadline = time.monotonic() + timeout
    while len(adapter.stream_calls) < count and time.monotonic() < deadline:
        time.sleep(0.005)


def _wait_for_worker_done(streamer: DingTalkTraceStreamer, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        worker = streamer._worker
        if worker is None or not worker.is_alive():
            return
        time.sleep(0.005)


def _card_markdown(adapter: FakeAdapter) -> str:
    return adapter.update_calls[-1]["card"]["msgContent"]


def test_start_creates_card_with_processing_state() -> None:
    adapter = FakeAdapter()
    streamer = _make_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    streamer.finish()
    _wait_for_worker_done(streamer)
    assert streamer._message_id is not None
    assert len(adapter.create_calls) == 1
    call = adapter.create_calls[0]
    # 幂等键与飞书同构：渠道前缀 + binding + turn
    assert call["key"] == "dingtalk-trace:chan_dingtalk:turn_1"
    assert call["target"]["to_user_id"] == "staff-1"
    card = call["card"]
    assert "正在" in card["msgTitle"]
    assert card["flowStatus"] == "1"
    assert card["msgContent"] == "等待执行步骤…"
    assert '"msgTitle"' in card["sys_full_json_obj"]
    # 未经流式（无中间事件）时 finish 直接定格：无 stream 调用
    assert adapter.stream_calls == []
    assert adapter.update_calls[-1]["card"]["flowStatus"] == "3"


def test_start_failure_does_not_raise_and_disables_updates() -> None:
    adapter = FakeAdapter(fail_create=True)
    streamer = _make_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    streamer.on_event("step_result", {"turn_id": "turn_1", "reply": "ok"})
    streamer.finish()
    _wait_for_worker_done(streamer)
    assert streamer._message_id is None
    # 永久错误不重试：单次尝试后放弃
    assert len(adapter.create_calls) == 1
    assert len(adapter.update_calls) == 0


def test_create_card_retries_transient_network_failure() -> None:
    """回归：建卡瞬时失败（网络抖动）时按 retryable 重试，而不是整轮放弃。

    生产环境曾出现"有正文回复（outbox 有重试）但无卡片（建卡一次失败
    即放弃）"的不对称；重试后同一轮仍能出卡片。
    """
    adapter = FakeAdapter(transient_create_failures=2)
    streamer = _make_streamer(adapter=adapter, card_retry_delay=0.0)
    streamer.start()
    _wait_for_card(streamer, timeout=2.0)
    streamer.on_event("router_decision_created", {"turn_id": "t1", "user_intent": "退款"})
    streamer.finish()
    _wait_for_worker_done(streamer, timeout=3.0)
    assert len(adapter.create_calls) == 3
    assert streamer._message_id is not None
    # 重试成功后正常走完流式与定格
    assert adapter.update_calls[-1]["card"]["flowStatus"] == "3"


def test_no_sop_router_line_replaces_internal_english_terms() -> None:
    """回归：无 SOP 匹配的闲聊轮次，卡片不得出现 conversation 等内部英文枚举。"""
    adapter = FakeAdapter()
    streamer = _make_streamer(adapter=adapter, user_message="你好")
    streamer.start()
    _wait_for_card(streamer)

    streamer.on_event(
        "router_decision_created",
        {
            "turn_id": "t1",
            "decision": "answer_only",
            "user_intent": "闲聊",
            "reason": "用户输入你好，输入闲聊，无SOP匹配，使用conversation。",
        },
    )
    streamer.finish()
    _wait_for_worker_done(streamer)

    markdown = _card_markdown(adapter)
    assert "conversation" not in markdown
    assert "判断意图 闲聊" in markdown
    assert "无SOP匹配，使用普通对话" in markdown


def test_final_state_update_retries_transient_failure() -> None:
    """回归：最终定格瞬时失败时重试，避免卡片永远停在"输入中"。"""

    class _FlakyFinalUpdateAdapter(FakeAdapter):
        def __init__(self, failures: int) -> None:
            super().__init__()
            self.failures = failures

        def update_card(self, binding, card_id, card_data) -> None:
            if card_data.get("flowStatus") in ("3", "5") and self.failures > 0:
                self.failures -= 1
                raise DingTalkTransientError("network down")
            super().update_card(binding, card_id, card_data)

    adapter = _FlakyFinalUpdateAdapter(failures=2)
    streamer = _make_streamer(adapter=adapter, card_retry_delay=0.0)
    streamer.start()
    _wait_for_card(streamer)
    streamer.on_event("tool_call_started", {"turn_id": "t1", "name": "lookup"})
    streamer.finish()
    _wait_for_worker_done(streamer, timeout=3.0)
    # 两次瞬时失败后第三次成功，卡片最终定格完成态
    assert adapter.update_calls[-1]["card"]["flowStatus"] == "3"


def test_on_event_renders_line_and_updates_card() -> None:
    adapter = FakeAdapter()
    streamer = _make_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)

    streamer.on_event(
        "router_decision_created",
        {"turn_id": "turn_1", "user_intent": "退款", "reason": "匹配退款SOP"},
    )
    streamer.finish()
    _wait_for_worker_done(streamer)

    # 首次内容更新：先切换 INPUTING（输入态），再走流式接口推送内容
    assert adapter.update_calls[0]["card"]["flowStatus"] == "2"
    assert "判断意图 退款" in adapter.update_calls[0]["card"]["msgContent"]
    assert len(adapter.stream_calls) >= 1
    first_stream = adapter.stream_calls[0]
    assert first_stream["key"] == "msgContent"
    assert first_stream["finalize"] is False
    assert "判断意图 退款" in first_stream["content"]
    assert "匹配退款SOP" in first_stream["content"]
    # 结束序列：流式 finalize 关闭通道 + 定格完成态
    last_stream = adapter.stream_calls[-1]
    assert last_stream["finalize"] is True
    assert last_stream["failed"] is False
    last_card = adapter.update_calls[-1]["card"]
    assert last_card["msgTitle"] == "执行完成"
    assert last_card["flowStatus"] == "3"
    assert "判断意图 退款" in last_card["msgContent"]


def test_throttle_merges_rapid_events() -> None:
    adapter = FakeAdapter()
    streamer = _make_streamer(adapter=adapter, min_update_interval=10.0)
    streamer.start()
    _wait_for_card(streamer)

    streamer.on_event("router_decision_created", {"turn_id": "t1"})
    streamer.on_event("step_result", {"turn_id": "t1", "next_step_id": "s2"})
    streamer.on_event("tool_call_started", {"turn_id": "t1", "name": "lookup"})

    # 节流合并：三个事件至多产生一次 INPUTING 转换 + 一次流式推送
    throttled_updates = len(adapter.update_calls)
    throttled_streams = len(adapter.stream_calls)
    assert throttled_updates <= 1
    assert throttled_streams <= 1

    streamer.finish()
    _wait_for_worker_done(streamer)
    # 最终内容包含节流窗口内合并的全部行
    assert len(adapter.stream_calls) > throttled_streams
    final_card = adapter.update_calls[-1]["card"]
    assert final_card["msgContent"].count("\n\n") >= 1


def test_update_failure_does_not_raise() -> None:
    adapter = FakeAdapter(fail_update=True)
    streamer = _make_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    streamer.on_event("router_decision_created", {"turn_id": "t1"})
    streamer.finish()
    _wait_for_worker_done(streamer)


def test_stream_failure_does_not_raise() -> None:
    """流式接口失败仅记日志：INPUTING 已切换，后续仍尝试流式与定格。"""
    adapter = FakeAdapter(fail_stream=True)
    streamer = _make_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    streamer.on_event("router_decision_created", {"turn_id": "t1"})
    streamer.finish()
    _wait_for_worker_done(streamer)
    # INPUTING 转换成功，流式尝试过，最终仍定格完成态
    assert adapter.update_calls[0]["card"]["flowStatus"] == "2"
    assert len(adapter.stream_calls) >= 2
    assert adapter.update_calls[-1]["card"]["flowStatus"] == "3"


def test_finish_marks_running_lines_completed() -> None:
    adapter = FakeAdapter()
    streamer = _make_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    streamer.on_event("tool_call_started", {"turn_id": "t1", "name": "lookup"})
    streamer.finish()
    _wait_for_worker_done(streamer)

    last_card = adapter.update_calls[-1]["card"]
    assert last_card["flowStatus"] == "3"
    assert last_card["msgTitle"] == "执行完成"
    # running 行定格为完成
    assert "⏳" not in last_card["msgContent"]
    assert "✅" in last_card["msgContent"]


def test_abort_marks_failed_state() -> None:
    adapter = FakeAdapter()
    streamer = _make_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    streamer.on_event("tool_call_started", {"turn_id": "t1", "name": "lookup"})
    streamer.abort("boom")
    _wait_for_worker_done(streamer)

    # 结束序列：流式以 isError 定格 + 卡片切失败态
    last_stream = adapter.stream_calls[-1]
    assert last_stream["finalize"] is True
    assert last_stream["failed"] is True
    last_card = adapter.update_calls[-1]["card"]
    assert last_card["flowStatus"] == "5"
    assert last_card["msgTitle"] == "执行失败"
    assert "❌" in last_card["msgContent"]


def test_on_event_after_finish_is_ignored() -> None:
    adapter = FakeAdapter()
    streamer = _make_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    streamer.finish()
    _wait_for_worker_done(streamer)
    updates_before = len(adapter.update_calls) + len(adapter.stream_calls)
    streamer.on_event("step_result", {"turn_id": "t1"})
    assert len(adapter.update_calls) + len(adapter.stream_calls) == updates_before


def test_is_dingtalk_trace_enabled() -> None:
    assert is_dingtalk_trace_enabled(_binding(channel="dingtalk")) is True
    assert is_dingtalk_trace_enabled(_binding(channel="feishu")) is False
    assert is_dingtalk_trace_enabled(_binding(channel="dingtalk", config={"trace_enabled": False})) is False
    assert is_dingtalk_trace_enabled(None) is False


def test_on_event_does_not_block_when_card_not_created() -> None:
    """卡片尚未创建时 on_event 应立即返回，不阻塞 AgentLoop。"""
    adapter = FakeAdapter()
    streamer = _make_streamer(adapter=adapter, min_update_interval=0.0)
    streamer.start()
    # 不等待卡片创建——立即发送事件
    streamer.on_event("router_decision_created", {"turn_id": "t1"})
    streamer.on_event("step_result", {"turn_id": "t1"})
    streamer.finish()
    _wait_for_worker_done(streamer)
    assert len(adapter.create_calls) == 1
    # 卡片创建后应有一次最终更新；建卡前的事件已合并进最终内容
    assert len(adapter.update_calls) >= 1
    last_card = adapter.update_calls[-1]["card"]
    assert last_card["flowStatus"] == "3"


def test_finish_before_card_created_still_sends_final_state() -> None:
    """finish 在卡片创建前调用：worker 先建卡再发最终状态。"""
    adapter = FakeAdapter()
    streamer = _make_streamer(adapter=adapter)
    streamer.start()
    streamer.on_event(
        "router_decision_created", {"turn_id": "t1", "user_intent": "退款"}
    )
    # 立即 finish，不等卡片创建
    streamer.finish()
    _wait_for_worker_done(streamer)
    assert len(adapter.create_calls) == 1
    # 从未进入流式，直接整卡定格（finalize 语义渲染最终内容）
    assert adapter.stream_calls == []
    assert len(adapter.update_calls) >= 1
    last_card = adapter.update_calls[-1]["card"]
    assert last_card["flowStatus"] == "3"
    assert "判断意图 退款" in last_card["msgContent"]


class _SlowAdapter(FakeAdapter):
    """模拟钉钉网络慢：create_card/update_card/stream_card 阻塞指定秒数。"""

    def __init__(self, *, delay: float) -> None:
        super().__init__()
        self.delay = delay

    def create_card(self, binding, target, card_data, *, idempotency_key) -> str:
        time.sleep(self.delay)
        return super().create_card(binding, target, card_data, idempotency_key=idempotency_key)

    def update_card(self, binding, card_id, card_data) -> None:
        time.sleep(self.delay)
        super().update_card(binding, card_id, card_data)

    def stream_card(self, binding, card_id, key, content, *, finalize=False, failed=False) -> None:
        time.sleep(self.delay)
        super().stream_card(
            binding, card_id, key, content, finalize=finalize, failed=failed
        )


def test_finish_does_not_block_on_slow_network() -> None:
    """回归：finish 不应因钉钉网络慢而阻塞主线程。"""
    adapter = _SlowAdapter(delay=2.0)
    streamer = _make_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    start = time.monotonic()
    streamer.finish()
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, f"finish blocked for {elapsed:.2f}s"
    _wait_for_worker_done(streamer, timeout=5.0)


def test_abort_does_not_block_on_slow_network() -> None:
    """回归：abort 不应因钉钉网络慢而阻塞主线程。"""
    adapter = _SlowAdapter(delay=2.0)
    streamer = _make_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    start = time.monotonic()
    streamer.abort("boom")
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, f"abort blocked for {elapsed:.2f}s"
    _wait_for_worker_done(streamer, timeout=5.0)


def test_card_renders_readable_step_and_tool_labels() -> None:
    """钉钉卡片与控制台共用行渲染：应展示可读步骤/能力名而不是裸 id。"""
    adapter = FakeAdapter()
    streamer = _make_streamer(
        adapter=adapter,
        skill_names={"skill_refund": "售后退款流程"},
        step_names={"skill_refund": {"handoff_to_repair_specialist": "转交维修专员"}},
        tool_names={"hr.balance_query": "假期考勤查询"},
    )
    streamer.start()
    _wait_for_card(streamer)
    streamer.on_event(
        "skill_step_changed",
        {"turn_id": "t1", "to_skill_id": "skill_refund", "to_step_id": "handoff_to_repair_specialist"},
    )
    streamer.on_event(
        "harness_action_created",
        {"turn_id": "t1", "task_frame_id": "f1", "iteration": 1, "action": "tool", "tool_name": "hr.balance_query"},
    )
    streamer.on_event(
        "harness_tool_completed",
        {"turn_id": "t1", "task_frame_id": "f1", "iteration": 1, "tool_name": "hr.balance_query", "success": True},
    )
    streamer.finish()
    _wait_for_worker_done(streamer)

    joined = _card_markdown(adapter)
    assert "当前步骤 转交维修专员" in joined
    assert "handoff_to_repair_specialist" not in joined
    assert "能力调用完成 假期考勤查询" in joined
    assert "hr.balance_query" not in joined


def test_streamer_loads_step_names_from_db() -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_a", name="Demo"))
        db.add(
            Skill(
                tenant_id="tenant_a",
                skill_id="skill_refund",
                name="售后退款流程",
                content_json={
                    "nodes": [
                        {"node_id": "collect_order_info", "name": "收集订单信息"},
                    ]
                },
            )
        )
        db.add(
            Tool(
                tenant_id="tenant_a",
                name="hr.balance_query",
                display_name="假期考勤查询",
                description="查询假期余额与考勤记录",
                tool_type="http",
                method="POST",
                url="https://example.com/api/hr/balance_query",
            )
        )
        db.commit()

        adapter = FakeAdapter()
        streamer = DingTalkTraceStreamer(
            _binding(),
            _target(),
            "turn_1",
            adapter=adapter,
            db=db,
            compact_sop=False,
        )
        streamer.start()
        _wait_for_card(streamer)
        streamer.on_event(
            "skill_step_changed",
            {"turn_id": "t1", "to_skill_id": "skill_refund", "to_step_id": "collect_order_info"},
        )
        streamer.finish()
        _wait_for_worker_done(streamer)

        joined = _card_markdown(adapter)
        assert "当前步骤 收集订单信息" in joined
        assert "collect_order_info" not in joined


# ---- SOP 紧凑展示（翻书动画 + 正在推进SOP）----


def _run_sop_lifecycle_running(streamer: DingTalkTraceStreamer) -> None:
    streamer.on_event("stream_status", {"turn_id": "t1", "phase": "routing"})
    streamer.on_event(
        "router_decision_created",
        {
            "turn_id": "t1",
            "user_intent": "电脑无法开机",
            "reason": "匹配\"电脑故障处理\"SOP",
            "target_skill_id": "skill_pc",
        },
    )
    streamer.on_event(
        "skill_started",
        {"turn_id": "t1", "to_skill_id": "skill_pc", "to_step_id": "collect_info"},
    )
    streamer.on_event(
        "skill_step_changed",
        {"turn_id": "t1", "from_skill_id": "skill_pc", "from_step_id": "collect_info",
         "to_skill_id": "skill_pc", "to_step_id": "reply"},
    )
    streamer.on_event(
        "task_frame_started",
        {"turn_id": "t1", "kind": "sop", "step_id": "collect_info"},
    )


def _finish_sop_lifecycle(streamer: DingTalkTraceStreamer, *, frame_status: str = "awaiting_user") -> None:
    streamer.on_event(
        "task_frame_finished",
        {"turn_id": "t1", "task_frame_id": "f1", "status": frame_status, "action_count": 1},
    )


def _wait_for_card_containing(adapter: FakeAdapter, *needles: str, timeout: float = 2.0) -> str:
    """等待出现一版包含全部关键字的卡片内容（流式或定格，节流合并下避免取早期快照）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        candidates = [call["content"] for call in adapter.stream_calls]
        candidates += [call["card"]["msgContent"] for call in adapter.update_calls]
        for content in reversed(candidates):
            if all(needle in content for needle in needles):
                return content
        time.sleep(0.005)
    raise AssertionError(f"card content containing {needles} not found within {timeout}s")


def _make_compact_streamer(
    *,
    adapter: FakeAdapter | None = None,
    min_update_interval: float = 0.0,
    binding=None,
) -> DingTalkTraceStreamer:
    return DingTalkTraceStreamer(
        binding or _binding(),
        _target(),
        "turn_1",
        adapter=adapter or FakeAdapter(),
        skill_names={"skill_pc": "电脑故障处理"},
        step_names={"skill_pc": {"collect_info": "收集电脑故障信息", "reply": "反馈处理结果"}},
        min_update_interval=min_update_interval,
        compact_sop=True,
    )


def test_compact_sop_hides_intermediate_steps_while_running() -> None:
    """紧凑模式：匹配 SOP 后中间步骤不展示，仅显示翻书动画 + 正在推进SOP。"""
    adapter = FakeAdapter()
    streamer = _make_compact_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    _run_sop_lifecycle_running(streamer)
    joined = _wait_for_card_containing(adapter, "判断意图 电脑无法开机", "正在推进SOP")

    assert "判断意图 电脑无法开机" in joined
    assert "正在推进SOP" in joined
    # 中间步骤全部隐藏
    assert "进入流程" not in joined
    assert "推进流程" not in joined
    assert "当前步骤" not in joined
    assert "开始执行任务" not in joined
    assert "调用能力" not in joined

    _finish_sop_lifecycle(streamer)
    streamer.finish()
    _wait_for_worker_done(streamer)


def test_compact_sop_shows_finished_after_completion() -> None:
    """紧凑模式：SOP 结束后显示"流程已结束"，卡片定格完成态。"""
    adapter = FakeAdapter()
    streamer = _make_compact_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    _run_sop_lifecycle_running(streamer)
    _finish_sop_lifecycle(streamer, frame_status="completed")
    streamer.on_event("skill_completed", {"turn_id": "t1", "skill_id": "skill_pc", "reason": "step_completed"})
    streamer.finish()
    _wait_for_worker_done(streamer)

    last_card = adapter.update_calls[-1]["card"]
    joined = last_card["msgContent"]
    assert "✅ 流程已结束" in joined
    assert "正在推进SOP" not in joined
    assert "判断意图 电脑无法开机" in joined
    assert last_card["flowStatus"] == "3"


def test_compact_sop_awaiting_user_shows_paused() -> None:
    """紧凑模式：等待用户补充信息时定格为"📖 流程已暂停"。"""
    adapter = FakeAdapter()
    streamer = _make_compact_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    _run_sop_lifecycle_running(streamer)
    _finish_sop_lifecycle(streamer)  # awaiting_user → 暂停
    streamer.finish()
    _wait_for_worker_done(streamer)

    last_card = adapter.update_calls[-1]["card"]
    joined = last_card["msgContent"]
    assert "📖 流程已暂停" in joined
    assert "等待用户补充信息后继续" in joined
    assert "流程已结束" not in joined
    # 本轮 turn 正常结束，卡片仍为完成态
    assert last_card["flowStatus"] == "3"


def test_compact_sop_abort_shows_unfinished() -> None:
    """紧凑模式：异常中止显示"流程未完成"，失败行仍透出。"""
    adapter = FakeAdapter()
    streamer = _make_compact_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    _run_sop_lifecycle_running(streamer)
    streamer.on_event(
        "error_occurred",
        {"turn_id": "t1", "code": "tool_failed", "message": "能力调用失败"},
    )
    streamer.abort("boom")
    _wait_for_worker_done(streamer)

    last_card = adapter.update_calls[-1]["card"]
    assert "❌ 流程未完成" in last_card["msgContent"]
    assert last_card["flowStatus"] == "5"


def test_compact_sop_animation_rotates_frames() -> None:
    """紧凑模式：无事件期间 worker 周期性流式更新，翻书 emoji 帧应轮换。"""
    adapter = FakeAdapter()
    streamer = _make_compact_streamer(adapter=adapter, min_update_interval=0.1)
    streamer.start()
    _wait_for_card(streamer)
    streamer.on_event(
        "skill_started",
        {"turn_id": "t1", "to_skill_id": "skill_pc", "to_step_id": "collect_info"},
    )
    _wait_for_updates(adapter, 3, timeout=3.0)
    streamer.finish()
    _wait_for_worker_done(streamer)

    frames = set()
    for call in adapter.stream_calls:
        if "正在推进SOP" in call["content"]:
            frames.add(call["content"].split(" ")[0])
    assert len(frames) >= 2, f"expected rotating flip frames, got {frames}"
