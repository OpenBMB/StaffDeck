from __future__ import annotations

import time
from types import SimpleNamespace

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.channels.adapters.feishu import FeishuPermanentError
from app.channels.feishu_trace import FeishuTraceStreamer, _SinkEvent, is_feishu_trace_enabled
from app.db.models import Skill, Tenant, Tool


def _binding(channel: str = "feishu", config: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id="chan_feishu",
        tenant_id="tenant_a",
        channel=channel,
        config_json=config if config is not None else {},
    )


class FakeAdapter:
    def __init__(self, *, fail_create: bool = False, fail_update: bool = False) -> None:
        self.create_calls: list[dict] = []
        self.update_calls: list[dict] = []
        self.fail_create = fail_create
        self.fail_update = fail_update

    def create_card(self, binding, target, card_json, *, idempotency_key) -> str:
        self.create_calls.append(
            {"binding": binding, "target": target, "card": card_json, "key": idempotency_key}
        )
        if self.fail_create:
            raise FeishuPermanentError("create failed")
        return "om_card_123"

    def update_card(self, binding, message_id, card_json) -> None:
        self.update_calls.append(
            {"binding": binding, "message_id": message_id, "card": card_json}
        )
        if self.fail_update:
            raise FeishuPermanentError("update failed")


def _make_streamer(
    *,
    adapter: FakeAdapter | None = None,
    min_update_interval: float = 0.0,
    compact_sop: bool = False,
) -> FeishuTraceStreamer:
    return FeishuTraceStreamer(
        _binding(),
        {"message_id": "om_source"},
        "turn_1",
        adapter=adapter or FakeAdapter(),
        min_update_interval=min_update_interval,
        compact_sop=compact_sop,
    )


def _wait_for_card(streamer: FeishuTraceStreamer, timeout: float = 1.0) -> None:
    """等待后台 worker 完成卡片创建（无论成功或失败）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if streamer._card_created:
            return
        time.sleep(0.005)


def _wait_for_updates(adapter: FakeAdapter, count: int, timeout: float = 2.0) -> None:
    """等待 adapter 收到指定数量的 update 调用。"""
    deadline = time.monotonic() + timeout
    while len(adapter.update_calls) < count and time.monotonic() < deadline:
        time.sleep(0.005)


def _wait_for_worker_done(streamer: FeishuTraceStreamer, timeout: float = 2.0) -> None:
    """等待 worker 线程退出（draining 完成）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        worker = streamer._worker
        if worker is None or not worker.is_alive():
            return
        time.sleep(0.005)


def _wait_for_card_containing(adapter: FakeAdapter, *needles: str, timeout: float = 2.0) -> dict:
    """等待出现一张包含全部关键字的最新卡片（节流合并下避免取到早期快照）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for call in reversed(adapter.update_calls):
            card = call["card"]
            joined = "\n".join(el["text"]["content"] for el in card["elements"])
            if all(needle in joined for needle in needles):
                return card
        time.sleep(0.005)
    raise AssertionError(f"card containing {needles} not found within {timeout}s")


def test_start_creates_card_and_saves_message_id() -> None:
    adapter = FakeAdapter()
    streamer = _make_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    streamer.finish()
    _wait_for_worker_done(streamer)
    assert streamer._message_id == "om_card_123"
    assert len(adapter.create_calls) == 1
    call = adapter.create_calls[0]
    assert call["key"] == "feishu-trace:chan_feishu:turn_1"
    assert call["target"] == {"message_id": "om_source"}
    header = call["card"]["header"]
    assert "正在" in header["title"]["content"]


def test_start_failure_does_not_raise_and_disables_updates() -> None:
    adapter = FakeAdapter(fail_create=True)
    streamer = _make_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    streamer.on_event("step_result", {"turn_id": "turn_1", "reply": "ok"})
    streamer.finish()
    _wait_for_worker_done(streamer)
    assert len(adapter.update_calls) == 0


def test_on_event_renders_line_and_patches_card() -> None:
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

    assert len(adapter.update_calls) >= 1
    last_card = adapter.update_calls[-1]["card"]
    elements = last_card["elements"]
    texts = [el["text"]["content"] for el in elements]
    assert any("判断意图" in t for t in texts)
    assert last_card["header"]["template"] == "green"


def test_throttle_merges_rapid_events() -> None:
    adapter = FakeAdapter()
    streamer = _make_streamer(adapter=adapter, min_update_interval=10.0)
    streamer.start()
    _wait_for_card(streamer)

    streamer.on_event("router_decision_created", {"turn_id": "t1"})
    streamer.on_event("step_result", {"turn_id": "t1", "next_step_id": "s2"})
    streamer.on_event("tool_call_started", {"turn_id": "t1", "name": "lookup"})

    throttled_updates = len(adapter.update_calls)
    assert throttled_updates <= 1

    streamer.finish()
    _wait_for_worker_done(streamer)
    final_updates = len(adapter.update_calls)
    assert final_updates > throttled_updates
    last_card = adapter.update_calls[-1]["card"]
    elements = last_card["elements"]
    assert len(elements) >= 2


def test_update_failure_does_not_raise() -> None:
    adapter = FakeAdapter(fail_update=True)
    streamer = _make_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    streamer.on_event("router_decision_created", {"turn_id": "t1"})
    streamer.finish()
    _wait_for_worker_done(streamer)


def test_finish_marks_running_lines_completed() -> None:
    adapter = FakeAdapter()
    streamer = _make_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    streamer.on_event("tool_call_started", {"turn_id": "t1", "name": "lookup"})
    streamer.finish()
    _wait_for_worker_done(streamer)

    last_card = adapter.update_calls[-1]["card"]
    assert last_card["header"]["template"] == "green"


def test_abort_marks_failed_state() -> None:
    adapter = FakeAdapter()
    streamer = _make_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    streamer.on_event("tool_call_started", {"turn_id": "t1", "name": "lookup"})
    streamer.abort("boom")
    _wait_for_worker_done(streamer)

    last_card = adapter.update_calls[-1]["card"]
    assert last_card["header"]["template"] == "red"


def test_on_event_after_finish_is_ignored() -> None:
    adapter = FakeAdapter()
    streamer = _make_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    streamer.finish()
    _wait_for_worker_done(streamer)
    updates_before = len(adapter.update_calls)
    streamer.on_event("step_result", {"turn_id": "t1"})
    assert len(adapter.update_calls) == updates_before


def test_sink_event_construction() -> None:
    event = _SinkEvent("step_result", {"turn_id": "t1", "reply": "ok"})
    assert event.event_type == "step_result"
    assert event.payload_json["turn_id"] == "t1"
    assert event.id == "t1"


def test_is_feishu_trace_enabled() -> None:
    assert is_feishu_trace_enabled(_binding(channel="feishu")) is True
    assert is_feishu_trace_enabled(_binding(channel="wechat")) is False
    assert is_feishu_trace_enabled(_binding(channel="feishu", config={"trace_enabled": False})) is False
    assert is_feishu_trace_enabled(None) is False


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
    # 卡片创建后应有一次最终更新
    assert len(adapter.update_calls) >= 1
    last_card = adapter.update_calls[-1]["card"]
    assert last_card["header"]["template"] == "green"


def test_finish_before_card_created_still_sends_final_state() -> None:
    """finish 在卡片创建前调用：worker 先建卡再发最终状态。"""
    adapter = FakeAdapter()
    streamer = _make_streamer(adapter=adapter)
    streamer.start()
    streamer.on_event("router_decision_created", {"turn_id": "t1"})
    # 立即 finish，不等卡片创建
    streamer.finish()
    _wait_for_worker_done(streamer)
    assert len(adapter.create_calls) == 1
    assert len(adapter.update_calls) >= 1
    last_card = adapter.update_calls[-1]["card"]
    assert last_card["header"]["template"] == "green"


class _SlowAdapter(FakeAdapter):
    """模拟飞书网络慢：create_card/update_card 阻塞指定秒数。"""

    def __init__(self, *, delay: float, fail_create: bool = False, fail_update: bool = False) -> None:
        super().__init__(fail_create=fail_create, fail_update=fail_update)
        self.delay = delay

    def create_card(self, binding, target, card_json, *, idempotency_key) -> str:
        time.sleep(self.delay)
        return super().create_card(binding, target, card_json, idempotency_key=idempotency_key)

    def update_card(self, binding, message_id, card_json) -> None:
        time.sleep(self.delay)
        super().update_card(binding, message_id, card_json)


def test_finish_does_not_block_on_slow_network() -> None:
    """回归：finish 不应因飞书网络慢而阻塞主线程。

    之前的实现 finish 会 join worker 最长 8 秒，飞书网络异常时
    会拖住会话锁。改为非阻塞后 finish 应在远小于网络延迟的时间内返回。
    """
    adapter = _SlowAdapter(delay=2.0)
    streamer = _make_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    # 卡片已创建，finish 会入队最终状态 patch（worker 执行时 sleep 2s）
    start = time.monotonic()
    streamer.finish()
    elapsed = time.monotonic() - start
    # finish 应立即返回，不应等待 2s 的 update_card 完成
    assert elapsed < 0.5, f"finish blocked for {elapsed:.2f}s"
    _wait_for_worker_done(streamer, timeout=5.0)


def test_abort_does_not_block_on_slow_network() -> None:
    """回归：abort 不应因飞书网络慢而阻塞主线程。"""
    adapter = _SlowAdapter(delay=2.0)
    streamer = _make_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    start = time.monotonic()
    streamer.abort("boom")
    elapsed = time.monotonic() - start
    assert elapsed < 0.5, f"abort blocked for {elapsed:.2f}s"
    _wait_for_worker_done(streamer, timeout=5.0)


def test_finish_then_worker_delivers_final_state() -> None:
    """finish 非阻塞返回后，worker 仍应异步送达最终状态。"""
    adapter = FakeAdapter()
    streamer = _make_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    streamer.on_event("tool_call_started", {"turn_id": "t1", "name": "lookup"})
    streamer.finish()
    # finish 立即返回，此时最终状态可能尚未送达
    _wait_for_worker_done(streamer)
    assert len(adapter.update_calls) >= 1
    last_card = adapter.update_calls[-1]["card"]
    assert last_card["header"]["template"] == "green"


def _card_texts(adapter: FakeAdapter) -> str:
    card = adapter.update_calls[-1]["card"]
    return "\n".join(el["text"]["content"] for el in card["elements"])


def test_card_renders_readable_step_and_status_labels() -> None:
    """飞书卡片应展示用户可读的步骤名与状态，而不是 step id / 状态码。"""
    adapter = FakeAdapter()
    streamer = FeishuTraceStreamer(
        _binding(),
        {"message_id": "om_source"},
        "turn_1",
        adapter=adapter,
        skill_names={"skill_refund": "售后退款流程"},
        step_names={
            "skill_refund": {
                "handoff_to_repair_specialist": "转交维修专员",
                "reply_final_result": "反馈最终结果",
            }
        },
        compact_sop=False,
    )
    streamer.start()
    _wait_for_card(streamer)
    streamer.on_event(
        "skill_step_changed",
        {"turn_id": "t1", "to_skill_id": "skill_refund", "to_step_id": "handoff_to_repair_specialist"},
    )
    streamer.on_event(
        "task_frame_finished",
        {"turn_id": "t1", "status": "handoff", "action_count": 3},
    )
    streamer.finish()
    _wait_for_worker_done(streamer)

    joined = _card_texts(adapter)
    assert "step handoff_to_repair_specialist" not in joined
    assert "当前步骤 转交维修专员" in joined
    assert "已转人工处理" in joined
    assert "状态 handoff" not in joined


def test_card_falls_back_to_readable_labels_for_unknown_step_ids() -> None:
    """技能卡片查不到步骤名时，也应对常见 step id 给出可读文案。"""
    adapter = FakeAdapter()
    streamer = FeishuTraceStreamer(
        _binding(),
        {"message_id": "om_source"},
        "turn_1",
        adapter=adapter,
        skill_names={"skill_refund": "售后退款流程"},
        compact_sop=False,
    )
    streamer.start()
    _wait_for_card(streamer)
    streamer.on_event(
        "skill_step_changed",
        {"turn_id": "t1", "to_skill_id": "skill_refund", "to_step_id": "handoff_to_repair_specialist"},
    )
    streamer.on_event(
        "task_frame_started",
        {"turn_id": "t1", "kind": "sop", "step_id": "reply_final_result"},
    )
    streamer.finish()
    _wait_for_worker_done(streamer)

    joined = _card_texts(adapter)
    assert "handoff_to_repair_specialist" not in joined
    assert "当前步骤 转人工处理" in joined
    assert "当前环节 反馈最终结果" in joined


def test_card_renders_tool_display_names_and_readable_completion_reason() -> None:
    """能力调用应展示中文能力名；skill_completed 的 reason 不应裸露英文码。"""
    adapter = FakeAdapter()
    streamer = FeishuTraceStreamer(
        _binding(),
        {"message_id": "om_source"},
        "turn_1",
        adapter=adapter,
        skill_names={"skill_refund": "售后退款流程"},
        step_names={"skill_refund": {}},
        tool_names={"hr.balance_query": "假期考勤查询"},
        compact_sop=False,
    )
    streamer.start()
    _wait_for_card(streamer)
    streamer.on_event(
        "harness_action_created",
        {"turn_id": "t1", "task_frame_id": "f1", "iteration": 1, "action": "tool", "tool_name": "hr.balance_query"},
    )
    streamer.on_event(
        "harness_tool_completed",
        {
            "turn_id": "t1",
            "task_frame_id": "f1",
            "iteration": 1,
            "tool_name": "hr.balance_query",
            "success": True,
        },
    )
    streamer.on_event(
        "harness_tool_completed",
        {
            "turn_id": "t1",
            "task_frame_id": "f1",
            "iteration": 2,
            "tool_name": "capability_describe",
            "success": True,
        },
    )
    streamer.on_event(
        "skill_completed",
        {"turn_id": "t1", "skill_id": "skill_refund", "reason": "step_completed"},
    )
    streamer.finish()
    _wait_for_worker_done(streamer)

    joined = _card_texts(adapter)
    assert "能力调用完成 假期考勤查询" in joined
    assert "hr.balance_query" not in joined
    assert "能力调用完成 查看能力详情" in joined
    assert "capability_describe" not in joined
    assert "完成流程 售后退款流程" in joined
    assert "step_completed" not in joined
    assert "全部步骤已完成" in joined


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
                        {"node_id": "reply_final_result", "name": "反馈最终结果"},
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
        streamer = FeishuTraceStreamer(
            _binding(),
            {"message_id": "om_source"},
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
        streamer.on_event(
            "task_frame_finished",
            {"turn_id": "t1", "status": "completed", "action_count": 2},
        )
        streamer.on_event(
            "harness_tool_completed",
            {
                "turn_id": "t1",
                "task_frame_id": "f1",
                "iteration": 1,
                "tool_name": "hr.balance_query",
                "success": True,
            },
        )
        streamer.finish()
        _wait_for_worker_done(streamer)

        joined = _card_texts(adapter)
        assert "当前步骤 收集订单信息" in joined
        assert "collect_order_info" not in joined
        assert "任务执行完成" in joined
        assert "能力调用完成 假期考勤查询" in joined
        assert "hr.balance_query" not in joined


# ---- SOP 紧凑展示（翻书动画 + 正在推进SOP）----


def _make_compact_streamer(
    *,
    adapter: FakeAdapter | None = None,
    min_update_interval: float = 0.0,
    binding=None,
    compact_sop: bool | None = True,
) -> FeishuTraceStreamer:
    return FeishuTraceStreamer(
        binding or _binding(),
        {"message_id": "om_source"},
        "turn_1",
        adapter=adapter or FakeAdapter(),
        skill_names={"skill_pc": "电脑故障处理"},
        step_names={"skill_pc": {"collect_info": "收集电脑故障信息", "reply": "反馈处理结果"}},
        min_update_interval=min_update_interval,
        compact_sop=compact_sop,
    )


def _run_sop_lifecycle(
    streamer: FeishuTraceStreamer, *, frame_status: str = "awaiting_user"
) -> None:
    """模拟一次完整 SOP 回合：判断意图 → 进入流程 → 中间步骤 → 结束。

    frame_status 默认 awaiting_user（等待用户补充信息 → 暂停）。
    """
    _run_sop_lifecycle_running(streamer)
    _finish_sop_lifecycle(streamer, frame_status=frame_status)


def _run_sop_lifecycle_running(streamer: FeishuTraceStreamer) -> None:
    """SOP 推进阶段：判断意图 → 进入流程 → 中间步骤。"""
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
    streamer.on_event(
        "harness_action_created",
        {"turn_id": "t1", "task_frame_id": "f1", "iteration": 1, "action": "tool",
         "tool_name": "capability_describe"},
    )


def _finish_sop_lifecycle(
    streamer: FeishuTraceStreamer, *, frame_status: str = "awaiting_user"
) -> None:
    """SOP 收尾：frame 结束（awaiting_user → 暂停）+ 整理结果动作。"""
    streamer.on_event(
        "task_frame_finished",
        {"turn_id": "t1", "task_frame_id": "f1", "status": frame_status, "action_count": 1},
    )
    streamer.on_event(
        "harness_action_created",
        {"turn_id": "t1", "task_frame_id": "f1", "iteration": 2, "action": "finish"},
    )


def test_compact_sop_hides_intermediate_steps_while_running() -> None:
    """紧凑模式：匹配 SOP 后中间步骤不展示，仅显示翻书动画 + 正在推进SOP。"""
    adapter = FakeAdapter()
    streamer = _make_compact_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    _run_sop_lifecycle_running(streamer)
    running_card = _wait_for_card_containing(adapter, "判断意图 电脑无法开机", "正在推进SOP")

    texts = [el["text"]["content"] for el in running_card["elements"]]
    joined = "\n".join(texts)
    # 仅保留判断意图（含匹配 SOP 理由），其余中间步骤全部隐藏
    assert "判断意图 电脑无法开机" in joined
    assert "匹配\"电脑故障处理\"SOP" in joined
    # 翻书动画 + 正在推进SOP 合成行
    assert any("正在推进SOP" in t for t in texts)
    # 中间步骤全部隐藏
    assert "进入流程" not in joined
    assert "推进流程" not in joined
    assert "当前步骤" not in joined
    assert "当前环节" not in joined
    assert "开始执行任务" not in joined
    assert "调用能力" not in joined
    assert "等待用户补充信息" not in joined
    assert "共执行 1 个操作" not in joined
    assert "整理任务结果" not in joined

    _finish_sop_lifecycle(streamer)
    streamer.finish()
    _wait_for_worker_done(streamer)


def test_compact_sop_shows_finished_after_skill_completed() -> None:
    """紧凑模式：SOP 结束后显示"流程已结束"，最终卡片定格绿色。"""
    adapter = FakeAdapter()
    streamer = _make_compact_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    _run_sop_lifecycle(streamer)
    streamer.on_event("skill_completed", {"turn_id": "t1", "skill_id": "skill_pc", "reason": "step_completed"})
    streamer.finish()
    _wait_for_worker_done(streamer)

    last_card = adapter.update_calls[-1]["card"]
    texts = [el["text"]["content"] for el in last_card["elements"]]
    joined = "\n".join(texts)
    assert "✅ 流程已结束" in joined
    assert "正在推进SOP" not in joined
    assert "流程已暂停" not in joined
    assert "完成流程" not in joined  # skill_completed 行由合成行替代
    # 判断意图保留，进入流程之后全部隐藏
    assert "判断意图 电脑无法开机" in joined
    assert "进入流程" not in joined
    assert last_card["header"]["template"] == "green"


def test_compact_sop_awaiting_user_shows_paused_with_book_icon() -> None:
    """紧凑模式：等待用户补充信息时定格为"📖 流程已暂停"，跟书不跟对号。"""
    adapter = FakeAdapter()
    streamer = _make_compact_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    _run_sop_lifecycle(streamer)  # frame 以 awaiting_user 结束
    streamer.finish()
    _wait_for_worker_done(streamer)

    last_card = adapter.update_calls[-1]["card"]
    texts = [el["text"]["content"] for el in last_card["elements"]]
    joined = "\n".join(texts)
    assert "📖 流程已暂停" in joined
    assert "等待用户补充信息后继续" in joined
    # 不显示"已结束"，也不带对号
    assert "流程已结束" not in joined
    assert "✅ 流程已暂停" not in joined
    assert "正在推进SOP" not in joined
    # 判断意图保留，中间步骤隐藏
    assert "判断意图 电脑无法开机" in joined
    assert "共执行 1 个操作" not in joined
    # 本轮 turn 正常结束，卡片头仍为绿色
    assert last_card["header"]["template"] == "green"


def test_compact_sop_finish_without_completion_still_shows_finished() -> None:
    """紧凑模式：SOP 未收到 skill_completed 就 finish 且未挂起时，仍定格为已结束。"""
    adapter = FakeAdapter()
    streamer = _make_compact_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    _run_sop_lifecycle(streamer, frame_status="completed")
    streamer.finish()
    _wait_for_worker_done(streamer)

    joined = _card_texts(adapter)
    assert "✅ 流程已结束" in joined
    assert "流程已暂停" not in joined
    assert "正在推进SOP" not in joined


def test_compact_sop_suspended_skill_state_shows_paused() -> None:
    """紧凑模式：skill_state 报 suspended（SOP 挂起）同样定格为已暂停。"""
    adapter = FakeAdapter()
    streamer = _make_compact_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    streamer.on_event(
        "skill_started",
        {"turn_id": "t1", "to_skill_id": "skill_pc", "to_step_id": "collect_info"},
    )
    streamer.on_event(
        "skill_state",
        {
            "turn_id": "t1",
            "currentSkills": [
                {"skillId": "skill_pc", "name": "电脑故障处理", "state": "suspended",
                 "stepId": "collect_info"},
            ],
        },
    )
    streamer.finish()
    _wait_for_worker_done(streamer)

    joined = _card_texts(adapter)
    assert "📖 流程已暂停" in joined
    assert "流程已结束" not in joined


def test_compact_sop_resumes_animation_after_new_frame() -> None:
    """紧凑模式：暂停后 frame 重新开始即恢复翻书动画行。"""
    adapter = FakeAdapter()
    streamer = _make_compact_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    _run_sop_lifecycle(streamer)  # 以暂停结束
    assert streamer._sop_paused is True
    assert streamer._should_animate() is False

    streamer.on_event(
        "task_frame_started",
        {"turn_id": "t1", "kind": "sop", "step_id": "reply"},
    )
    assert streamer._sop_paused is False
    assert streamer._should_animate() is True
    resumed_card = _wait_for_card_containing(adapter, "正在推进SOP")
    resumed_joined = "\n".join(el["text"]["content"] for el in resumed_card["elements"])
    assert "流程已暂停" not in resumed_joined

    streamer.finish()
    _wait_for_worker_done(streamer)
    joined = _card_texts(adapter)
    assert "✅ 流程已结束" in joined


def test_compact_sop_abort_shows_unfinished() -> None:
    """紧凑模式：异常中止显示"流程未完成"，失败行仍透出。"""
    adapter = FakeAdapter()
    streamer = _make_compact_streamer(adapter=adapter)
    streamer.start()
    _wait_for_card(streamer)
    _run_sop_lifecycle(streamer)
    streamer.on_event(
        "error_occurred",
        {"turn_id": "t1", "code": "tool_failed", "message": "能力调用失败"},
    )
    streamer.abort("boom")
    _wait_for_worker_done(streamer)

    joined = _card_texts(adapter)
    assert "❌ 流程未完成" in joined
    last_card = adapter.update_calls[-1]["card"]
    assert last_card["header"]["template"] == "red"


def test_compact_sop_animation_rotates_frames() -> None:
    """紧凑模式：无事件期间 worker 周期性 PATCH，翻书 emoji 帧应轮换。"""
    adapter = FakeAdapter()
    streamer = _make_compact_streamer(adapter=adapter, min_update_interval=0.1)
    streamer.start()
    _wait_for_card(streamer)
    streamer.on_event(
        "skill_started",
        {"turn_id": "t1", "to_skill_id": "skill_pc", "to_step_id": "collect_info"},
    )
    # 等待动画 tick 产生多帧
    _wait_for_updates(adapter, 3, timeout=3.0)
    streamer.finish()
    _wait_for_worker_done(streamer)

    frames = set()
    for call in adapter.update_calls:
        for el in call["card"]["elements"]:
            content = el["text"]["content"]
            if "正在推进SOP" in content:
                frames.add(content.split(" ")[0])
    assert len(frames) >= 2, f"expected rotating flip frames, got {frames}"


def test_compact_sop_animation_stops_after_finish() -> None:
    """紧凑模式：finish 后 worker 退出，不再产生动画 PATCH。"""
    adapter = FakeAdapter()
    streamer = _make_compact_streamer(adapter=adapter, min_update_interval=0.1)
    streamer.start()
    _wait_for_card(streamer)
    streamer.on_event(
        "skill_started",
        {"turn_id": "t1", "to_skill_id": "skill_pc", "to_step_id": "collect_info"},
    )
    _wait_for_updates(adapter, 1, timeout=2.0)
    streamer.finish()
    _wait_for_worker_done(streamer)
    updates_after_finish = len(adapter.update_calls)
    time.sleep(0.3)
    assert len(adapter.update_calls) == updates_after_finish


def test_compact_sop_disabled_keeps_legacy_step_lines() -> None:
    """回滚开关：compact_sop=False 时保留原有逐行样式。"""
    adapter = FakeAdapter()
    streamer = _make_compact_streamer(adapter=adapter, compact_sop=False)
    streamer.start()
    _wait_for_card(streamer)
    _run_sop_lifecycle(streamer)
    streamer.finish()
    _wait_for_worker_done(streamer)

    joined = _card_texts(adapter)
    assert "进入流程 电脑故障处理" in joined
    assert "推进流程 电脑故障处理" in joined
    assert "当前步骤 反馈处理结果" in joined
    assert "开始执行任务" in joined
    assert "等待用户补充信息" in joined
    assert "整理任务结果" in joined
    assert "正在推进SOP" not in joined


def test_compact_sop_disabled_by_binding_config() -> None:
    """回滚开关：binding config_json.compact_trace=False 时走旧样式。"""
    adapter = FakeAdapter()
    streamer = _make_compact_streamer(
        adapter=adapter,
        binding=_binding(config={"compact_trace": False}),
        compact_sop=None,
    )
    assert streamer._compact_sop is False
    streamer.start()
    _wait_for_card(streamer)
    _run_sop_lifecycle(streamer)
    streamer.finish()
    _wait_for_worker_done(streamer)

    joined = _card_texts(adapter)
    assert "推进流程 电脑故障处理" in joined
    assert "正在推进SOP" not in joined
