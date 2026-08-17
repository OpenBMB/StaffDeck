from __future__ import annotations

import time
from types import SimpleNamespace

from app.channels.adapters.feishu import FeishuPermanentError
from app.channels.feishu_trace import FeishuTraceStreamer, _SinkEvent, is_feishu_trace_enabled


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
) -> FeishuTraceStreamer:
    return FeishuTraceStreamer(
        _binding(),
        {"message_id": "om_source"},
        "turn_1",
        adapter=adapter or FakeAdapter(),
        min_update_interval=min_update_interval,
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
