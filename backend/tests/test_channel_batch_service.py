"""批处理服务测试:token 桶限流整形、批量作业、回填作业(功能3/4)。"""

import json
import time

from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from app.channels.adapters.base import ChannelCapability
from app.channels.batch_service import (
    BACKFILL_DEFAULT_LIMIT,
    BatchService,
    TokenBucket,
    _batch_idempotency_key,
)
from app.db.models import ChannelBinding, ChannelInboundEvent


class _BatchAdapter:
    """记录 send 调用的假适配器,支持按文本触发失败与富媒体载荷记录。"""

    def __init__(self, fail_texts: set[str] | None = None) -> None:
        self.sent: list[tuple[str, str, str | None]] = []
        self.payloads: list[str | None] = []
        self.fail_texts = fail_texts or set()

    def channel_capabilities(self) -> set[ChannelCapability]:
        return {ChannelCapability.BATCH_SEND}

    def send(
        self,
        binding,
        target,
        text: str,
        *,
        idempotency_key: str | None = None,
        payload_json: str | None = None,
    ) -> None:
        if text in self.fail_texts:
            raise RuntimeError(f"模拟发送失败: {text}")
        self.sent.append((binding.id, text, idempotency_key))
        self.payloads.append(payload_json)


class _BackfillAdapter:
    def __init__(self, messages: list[dict]) -> None:
        self.messages = messages
        self.calls: list[tuple[str, str | None, str | None, int]] = []

    def channel_capabilities(self) -> set[ChannelCapability]:
        return {ChannelCapability.BACKFILL}

    def fetch_history(self, binding, target: dict, *, before=None, after=None, limit=None):
        channel_id = str(target.get("channel_id") or "")
        self.calls.append((channel_id, before, after, limit or BACKFILL_DEFAULT_LIMIT))
        return self.messages


def _engine(binding_channel: str = "batch_test") -> object:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(
            ChannelBinding(
                id="binding_batch",
                tenant_id="tenant_demo",
                agent_id="agent_1",
                channel=binding_channel,
                status="active",
                config_json={"bot_id": "bot_1"},
            )
        )
        db.commit()
    return engine


def _binding() -> ChannelBinding:
    return ChannelBinding(
        id="binding_batch",
        tenant_id="tenant_demo",
        agent_id="agent_1",
        channel="batch_test",
        status="active",
    )


def _register_adapter(monkeypatch, adapter, channel: str = "batch_test") -> None:
    import app.channels.adapters.base as base_module

    monkeypatch.setitem(base_module._adapters, channel, adapter)


# ---------- TokenBucket ----------


def test_token_bucket_acquire_exhausts_and_blocks() -> None:
    bucket = TokenBucket(capacity=2, refill_period_seconds=60.0)
    assert bucket.acquire() is True
    assert bucket.acquire() is True
    assert bucket.acquire() is False


def test_token_bucket_refills_over_time() -> None:
    bucket = TokenBucket(capacity=2, refill_period_seconds=0.05, refill_amount=1)
    assert bucket.acquire() and bucket.acquire()
    assert not bucket.acquire()
    time.sleep(0.12)
    assert bucket.acquire() is True


def test_token_bucket_wait_until_available_blocks_then_succeeds() -> None:
    bucket = TokenBucket(capacity=1, refill_period_seconds=0.05, refill_amount=1)
    assert bucket.acquire() is True
    start = time.monotonic()
    assert bucket.wait_until_available(timeout=2.0) is True
    assert time.monotonic() - start >= 0.04


def test_token_bucket_wait_timeout_returns_false() -> None:
    bucket = TokenBucket(capacity=1, refill_period_seconds=60.0)
    assert bucket.acquire() is True
    assert bucket.wait_until_available(timeout=0.05) is False


def test_bucket_is_per_binding() -> None:
    service = BatchService()
    assert service.bucket_for("b1") is service.bucket_for("b1")
    assert service.bucket_for("b1") is not service.bucket_for("b2")


# ---------- 批量作业 ----------


def test_run_batch_sends_all_items_with_idempotency_keys(monkeypatch) -> None:
    engine = _engine()
    adapter = _BatchAdapter()
    _register_adapter(monkeypatch, adapter)
    service = BatchService()
    binding = _binding()
    job_id = service.submit_batch(
        binding, "tenant_demo", {"channel_id": "chan_1"}, ["a", "b", "c"], db_engine=engine, autostart=False
    )
    job = service.get_batch(job_id)
    assert job is not None
    service.run_batch(job_id, db_engine=engine)

    assert job.status == "done"
    assert job.progress == 3
    assert job.succeeded == 3
    assert job.failed == 0
    assert [(text, key) for _, text, key in adapter.sent] == [
        ("a", _batch_idempotency_key(job_id, 0)),
        ("b", _batch_idempotency_key(job_id, 1)),
        ("c", _batch_idempotency_key(job_id, 2)),
    ]
    assert adapter.sent[0][0] == "binding_batch"
    service.clear_batches()


def test_run_batch_single_failure_does_not_abort(monkeypatch) -> None:
    engine = _engine()
    adapter = _BatchAdapter(fail_texts={"b"})
    _register_adapter(monkeypatch, adapter)
    service = BatchService()
    job_id = service.submit_batch(
        _binding(), "tenant_demo", {"channel_id": "chan_1"}, ["a", "b", "c"], db_engine=engine, autostart=False
    )
    service.run_batch(job_id, db_engine=engine)

    job = service.get_batch(job_id)
    assert job.status == "done"
    assert job.succeeded == 2
    assert job.failed == 1
    assert len(job.errors) == 1
    assert "index=1" in job.errors[0]
    service.clear_batches()


def test_run_batch_without_adapter_send_marks_failed(monkeypatch) -> None:
    class _NoSendAdapter:
        pass

    engine = _engine()
    _register_adapter(monkeypatch, _NoSendAdapter())
    service = BatchService()
    job_id = service.submit_batch(
        _binding(), "tenant_demo", {"channel_id": "chan_1"}, ["a"], db_engine=engine, autostart=False
    )
    service.run_batch(job_id, db_engine=engine)

    job = service.get_batch(job_id)
    assert job.status == "failed"
    assert job.errors == ["adapter_no_send"]
    service.clear_batches()


def test_run_batch_supports_rich_payload_items(monkeypatch) -> None:
    engine = _engine()
    adapter = _BatchAdapter()
    _register_adapter(monkeypatch, adapter)
    service = BatchService()
    job_id = service.submit_batch(
        _binding(),
        "tenant_demo",
        {"channel_id": "chan_1"},
        ["plain", {"content": "rich", "embeds": [{"title": "t"}]}],
        db_engine=engine,
        autostart=False,
    )
    service.run_batch(job_id, db_engine=engine)

    job = service.get_batch(job_id)
    assert job.succeeded == 2
    # 富媒体项以 payload_json 命名参数传递,不再塞进 target
    assert adapter.payloads == [
        None,
        json.dumps({"embeds": [{"title": "t"}]}, ensure_ascii=False),
    ]
    rich_call = adapter.sent[1]
    assert rich_call[1] == "rich"
    service.clear_batches()


def test_client_batch_id_deduplicates(monkeypatch) -> None:
    engine = _engine()
    adapter = _BatchAdapter()
    _register_adapter(monkeypatch, adapter)
    service = BatchService()
    first = service.submit_batch(
        _binding(),
        "tenant_demo",
        {"channel_id": "chan_1"},
        ["a"],
        client_batch_id="cb-1",
        db_engine=engine,
        autostart=False,
    )
    second = service.submit_batch(
        _binding(),
        "tenant_demo",
        {"channel_id": "chan_1"},
        ["a"],
        client_batch_id="cb-1",
        db_engine=engine,
        autostart=False,
    )
    assert first == second
    service.clear_batches()


def test_clear_batches_keeps_running_jobs() -> None:
    service = BatchService()
    job_id = service.submit_batch(
        _binding(), "tenant_demo", {"channel_id": "c"}, ["a"], autostart=False
    )
    service.get_batch(job_id).status = "done"
    assert service.clear_batches() == 1
    assert service.get_batch(job_id) is None


# ---------- 回填作业 ----------


def _backfill_messages() -> list[dict]:
    return [
        {"id": "msg_1", "content": "hello", "channel_id": "chan_1", "guild_id": "guild_1", "author": {"id": "user_1"}},
        {"id": "msg_2", "content": "world", "channel_id": "chan_1", "guild_id": "guild_1", "author": {"id": "user_2"}},
    ]


def test_run_backfill_writes_backfilled_events_without_agent(monkeypatch) -> None:
    engine = _engine()
    adapter = _BackfillAdapter(_backfill_messages())
    _register_adapter(monkeypatch, adapter)
    service = BatchService()
    job_id = service.submit_backfill(
        _binding(), channel_id="chan_1", limit=100, db_engine=engine, autostart=False
    )
    service.run_backfill(job_id, db_engine=engine)

    job = service.get_backfill(job_id)
    assert job.status == "done"
    assert job.written == 2
    assert job.duplicates == 0
    assert adapter.calls == [("chan_1", None, None, 100)]
    with Session(engine) as db:
        events = db.exec(select(ChannelInboundEvent).order_by(ChannelInboundEvent.event_id)).all()
        assert len(events) == 2
        for event in events:
            # backfilled 状态不在 durable intake 的 received 集合,不触发 agent
            assert event.status == "backfilled"
            assert event.target_json["message_id"] == event.event_id
            assert event.target_json["channel_id"] == "chan_1"
            assert event.payload_json["backfilled"] is True
    service.clear_backfills()


def test_run_backfill_accepts_channel_inbound_objects(monkeypatch) -> None:
    """真实 Discord 适配器返回 ChannelInbound(dataclass):须归一为 dict 落库。"""
    from app.channels.adapters.base import ChannelInbound

    messages = [
        ChannelInbound(
            channel="discord",
            event_id="m_1",
            from_user_id="user_1",
            to_user_id="bot_1",
            session_id="chan_1",
            group_id="guild_1",
            context_token="",
            text="历史消息一",
            is_group=True,
            raw={"channel_id": "chan_1", "guild_id": "guild_1", "created_at": "2026-08-01T00:00:00+00:00"},
            sender_name="Alice",
        ),
        ChannelInbound(
            channel="discord",
            event_id="m_2",
            from_user_id="user_2",
            to_user_id="bot_1",
            session_id="chan_1",
            group_id="guild_1",
            context_token="",
            text="历史消息二",
            is_group=True,
            raw={"channel_id": "chan_1", "guild_id": "guild_1"},
        ),
    ]
    engine = _engine()
    adapter = _BackfillAdapter(messages)  # type: ignore[arg-type]
    _register_adapter(monkeypatch, adapter)
    service = BatchService()
    job_id = service.submit_backfill(
        _binding(), channel_id="chan_1", db_engine=engine, autostart=False
    )
    service.run_backfill(job_id, db_engine=engine)

    job = service.get_backfill(job_id)
    assert job.status == "done"
    assert job.written == 2
    with Session(engine) as db:
        events = db.exec(select(ChannelInboundEvent).order_by(ChannelInboundEvent.event_id)).all()
        assert [event.event_id for event in events] == ["m_1", "m_2"]
        stored = events[0].payload_json["message"]
        assert stored["id"] == "m_1"
        assert stored["channel_id"] == "chan_1"
        assert stored["guild_id"] == "guild_1"
        assert stored["author_id"] == "user_1"
        assert stored["created_at"] == "2026-08-01T00:00:00+00:00"
        assert events[1].payload_json["message"]["created_at"] == ""
    service.clear_backfills()


def test_run_backfill_is_idempotent_by_message_id(monkeypatch) -> None:
    engine = _engine()
    adapter = _BackfillAdapter(_backfill_messages())
    _register_adapter(monkeypatch, adapter)
    service = BatchService()
    job_id = service.submit_backfill(
        _binding(), channel_id="chan_1", db_engine=engine, autostart=False
    )
    service.run_backfill(job_id, db_engine=engine)
    service.run_backfill(job_id, db_engine=engine)

    job = service.get_backfill(job_id)
    assert job.status == "done"
    assert job.written == 2
    assert job.duplicates == 2
    with Session(engine) as db:
        count = len(db.exec(select(ChannelInboundEvent)).all())
        assert count == 2
    service.clear_backfills()


def test_run_backfill_without_fetch_history_marks_failed(monkeypatch) -> None:
    class _NoHistoryAdapter:
        pass

    engine = _engine()
    _register_adapter(monkeypatch, _NoHistoryAdapter())
    service = BatchService()
    job_id = service.submit_backfill(
        _binding(), channel_id="chan_1", db_engine=engine, autostart=False
    )
    service.run_backfill(job_id, db_engine=engine)

    job = service.get_backfill(job_id)
    assert job.status == "failed"
    assert job.errors == ["adapter_no_fetch_history"]
    service.clear_backfills()


def test_run_backfill_missing_binding_marks_failed(monkeypatch) -> None:
    engine = _engine()
    adapter = _BackfillAdapter([])
    _register_adapter(monkeypatch, adapter)
    service = BatchService()
    job_id = service.submit_backfill(
        _binding(), channel_id="chan_1", db_engine=engine, autostart=False
    )
    # 提交后删除绑定,模拟绑定已删
    with Session(engine) as db:
        binding = db.get(ChannelBinding, "binding_batch")
        db.delete(binding)
        db.commit()
    service.run_backfill(job_id, db_engine=engine)

    job = service.get_backfill(job_id)
    assert job.status == "failed"
    assert job.errors == ["binding_not_found"]
