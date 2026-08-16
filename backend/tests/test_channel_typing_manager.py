"""typing_manager 周期 typing 指示器测试(功能6)。"""

import time

from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

from app.channels.adapters.base import ChannelCapability
from app.channels.typing_manager import TypingManager, begin_typing, end_typing
from app.db.models import ChannelBinding


class _TypingAdapter:
    """声明 TYPING 能力的假适配器:记录 send_typing 调用。"""

    def __init__(self) -> None:
        self.typing_calls: list[tuple[str, dict, int]] = []

    def channel_capabilities(self) -> set[ChannelCapability]:
        return {ChannelCapability.TYPING}

    def send_typing(self, binding, target, status: int) -> None:
        self.typing_calls.append((binding.id, dict(target), status))


class _NoTypingAdapter:
    """有 send_typing 但未声明 TYPING 能力(模拟微信):不应被 typing_manager 接管。"""

    def channel_capabilities(self) -> set[ChannelCapability]:
        return set()

    def send_typing(self, binding, target, status: int) -> None:
        raise AssertionError("未声明 TYPING 能力的渠道不应收到周期 typing")


class _PlainAdapter:
    """完全没有 send_typing 的存量渠道(飞书/钉钉):begin 应 no-op。"""

    def send(self, *args, **kwargs) -> None:
        return None


def _engine_with_binding(channel: str = "typing_test") -> object:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(
            ChannelBinding(
                id="binding_typing",
                tenant_id="tenant_demo",
                agent_id="agent_1",
                channel=channel,
                status="active",
            )
        )
        db.commit()
    return engine


def _manager(engine, monkeypatch, adapter, channel: str = "typing_test") -> TypingManager:
    import app.channels.adapters.base as base_module

    monkeypatch.setitem(base_module._adapters, channel, adapter)
    manager = TypingManager(interval_seconds=0.05, db_engine=engine)
    return manager


def _binding() -> ChannelBinding:
    return ChannelBinding(
        id="binding_typing",
        tenant_id="tenant_demo",
        agent_id="agent_1",
        channel="typing_test",
        status="active",
    )


def test_begin_sends_immediate_pulse_and_registers_timer(monkeypatch) -> None:
    engine = _engine_with_binding()
    adapter = _TypingAdapter()
    manager = _manager(engine, monkeypatch, adapter)
    binding = _binding()

    manager.begin(binding, {"channel_id": "chan_1"})

    # begin 立即触发一次 + 定时器已注册
    assert len(adapter.typing_calls) == 1
    assert adapter.typing_calls[0][0] == "binding_typing"
    assert adapter.typing_calls[0][2] == 1
    assert len(manager.active_keys()) == 1
    manager.end(binding, {"channel_id": "chan_1"})


def test_begin_repeats_periodically_until_end(monkeypatch) -> None:
    engine = _engine_with_binding()
    adapter = _TypingAdapter()
    manager = _manager(engine, monkeypatch, adapter)
    binding = _binding()
    target = {"channel_id": "chan_1"}

    manager.begin(binding, target)
    time.sleep(0.16)
    manager.end(binding, target)
    # 立即 1 次 + 0.05s 周期在 0.16s 内至少再触发 2 次
    assert len(adapter.typing_calls) >= 3
    assert manager.active_keys() == []


def test_end_stops_periodic_typing(monkeypatch) -> None:
    engine = _engine_with_binding()
    adapter = _TypingAdapter()
    manager = _manager(engine, monkeypatch, adapter)
    binding = _binding()
    target = {"channel_id": "chan_1"}

    manager.begin(binding, target)
    manager.end(binding, target)
    calls_after_end = len(adapter.typing_calls)
    time.sleep(0.12)
    assert len(adapter.typing_calls) == calls_after_end


def test_end_without_begin_is_noop(monkeypatch) -> None:
    manager = _manager(_engine_with_binding(), monkeypatch, _TypingAdapter())
    manager.end(_binding(), {"channel_id": "chan_1"})
    assert manager.active_keys() == []


def test_begin_ignores_adapter_without_typing_capability(monkeypatch) -> None:
    """微信模拟:有 send_typing 但无 TYPING 能力声明,begin 必须 no-op。"""
    adapter = _NoTypingAdapter()
    engine = _engine_with_binding()
    manager = _manager(engine, monkeypatch, adapter)
    manager.begin(_binding(), {"to_user_id": "u1", "context_token": "tok"})
    assert manager.active_keys() == []


def test_begin_respects_features_typing_disabled(monkeypatch) -> None:
    """§3.1 features.typing=false:即使适配器声明 TYPING,begin/end 也全部 no-op。"""
    engine = _engine_with_binding()
    adapter = _TypingAdapter()
    manager = _manager(engine, monkeypatch, adapter)
    binding = _binding()
    binding.config_json = {"features": {"typing": False}}

    manager.begin(binding, {"channel_id": "chan_1"})
    assert adapter.typing_calls == []
    assert manager.active_keys() == []
    manager.end(binding, {"channel_id": "chan_1"})
    assert manager.active_keys() == []


def test_begin_typing_enabled_by_default_without_features(monkeypatch) -> None:
    """无 features 配置的存量绑定:typing 默认开启,行为不变。"""
    engine = _engine_with_binding()
    adapter = _TypingAdapter()
    manager = _manager(engine, monkeypatch, adapter)
    binding = _binding()
    binding.config_json = {"bot_id": "bot_1"}

    manager.begin(binding, {"channel_id": "chan_1"})
    assert len(adapter.typing_calls) == 1
    manager.end(binding, {"channel_id": "chan_1"})
    assert manager.active_keys() == []


def test_begin_ignores_adapter_without_send_typing(monkeypatch) -> None:
    """飞书/钉钉模拟:无 send_typing 方法,begin 必须 no-op。"""
    adapter = _PlainAdapter()
    engine = _engine_with_binding()
    manager = _manager(engine, monkeypatch, adapter)
    manager.begin(_binding(), {"channel_id": "chan_1"})
    assert manager.active_keys() == []


def test_begin_is_idempotent_for_same_target(monkeypatch) -> None:
    engine = _engine_with_binding()
    adapter = _TypingAdapter()
    manager = _manager(engine, monkeypatch, adapter)
    binding = _binding()
    target = {"channel_id": "chan_1"}

    manager.begin(binding, target)
    manager.begin(binding, target)
    assert len(manager.active_keys()) == 1
    assert len(adapter.typing_calls) == 1
    manager.end(binding, target)


def test_convenience_functions_route_to_singleton(monkeypatch) -> None:
    """模块级便捷函数复用同一单例(默认 TypingManager 实例)。"""
    from app.channels import typing_manager as tm_module

    assert tm_module.typing_manager is tm_module.begin_typing.__globals__["typing_manager"]
    begin_typing(_binding(), {"channel_id": "chan_1"})
    try:
        # 单例默认 db_engine 可能没有该 binding,仅验证不会抛异常且能停止
        end_typing(_binding(), {"channel_id": "chan_1"})
    finally:
        tm_module.typing_manager.end(_binding(), {"channel_id": "chan_1"})
