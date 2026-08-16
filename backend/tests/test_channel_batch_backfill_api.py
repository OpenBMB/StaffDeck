"""渠道 API 扩展测试:白名单配置更新、批处理/回填端点(功能3/4/5)。"""

from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.api.channels as channels_api
from app.channels.batch_service import BatchJob, BatchService
from app.db import get_session
from app.db.models import (
    AgentProfile,
    ChannelBinding,
    ChannelInboundEvent,
    ChatSession,
    Message,
    Tenant,
    User,
    utc_now,
)
from app.security.auth import create_access_token


def _engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _client(engine) -> TestClient:
    app = FastAPI()
    app.include_router(channels_api.router)

    def override_session():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_session] = override_session
    return TestClient(app)


def _seed(engine, *, channel: str = "discord") -> User:
    with Session(engine) as db:
        db.add(Tenant(id="tenant_a", name="A"))
        owner = User(
            id="user_owner",
            tenant_id="tenant_a",
            username="owner",
            password_hash="x",
        )
        other = User(
            id="user_other",
            tenant_id="tenant_a",
            username="other",
            password_hash="x",
        )
        db.add(owner)
        db.add(other)
        db.add(
            AgentProfile(
                id="agent_a",
                tenant_id="tenant_a",
                name="Agent A",
                metadata_json={"owner_user_id": owner.id},
            )
        )
        binding = ChannelBinding(
            id="chan_discord",
            tenant_id="tenant_a",
            agent_id="agent_a",
            channel=channel,
            status="active",
            config_json={"bot_id": "bot-123", "bot_name": "Bot"},
            created_by_user_id=owner.id,
        )
        db.add(binding)
        db.commit()
        db.refresh(owner)
        db.expunge(owner)
        return owner


def _auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user)}"}


# ---------- 功能5:白名单/features 配置更新 ----------


def test_put_binding_updates_allowlist_and_features_with_revision_bump() -> None:
    engine = _engine()
    owner = _seed(engine)
    client = _client(engine)

    response = client.put(
        "/api/enterprise/channels/chan_discord?tenant_id=tenant_a",
        json={
            "allowlist": {
                "mode": "deny_all",
                "guild_ids": ["guild_1"],
                "channel_ids": ["chan_1", "chan_2"],
                "role_ids": [],
                "user_ids": ["user_1"],
                "deny": ["chan_3"],
            },
            "features": {"slash_commands": True, "voice": True, "backfill": False},
        },
        headers=_auth(owner),
    )
    assert response.status_code == 200
    with Session(engine) as db:
        binding = db.get(ChannelBinding, "chan_discord")
        assert binding.config_revision == 1
        allowlist = binding.config_json["allowlist"]
        assert allowlist["mode"] == "deny_all"
        assert allowlist["guild_ids"] == ["guild_1"]
        assert allowlist["channel_ids"] == ["chan_1", "chan_2"]
        assert allowlist["user_ids"] == ["user_1"]
        assert allowlist["deny"] == ["chan_3"]
        features = binding.config_json["features"]
        assert features["voice"] is True
        assert features["backfill"] is False
        # 未声明的功能保持 schema 默认值
        assert features["typing"] is True


def test_put_binding_allowlist_only_keeps_agents_untouched() -> None:
    engine = _engine()
    owner = _seed(engine)
    client = _client(engine)
    response = client.put(
        "/api/enterprise/channels/chan_discord?tenant_id=tenant_a",
        json={"allowlist": {"channel_ids": ["chan_1"]}},
        headers=_auth(owner),
    )
    assert response.status_code == 200
    assert [(a["agent_id"], a["is_default"]) for a in response.json()["agents"]] == [("agent_a", True)]
    with Session(engine) as db:
        binding = db.get(ChannelBinding, "chan_discord")
        assert binding.config_revision == 1
        assert "features" not in binding.config_json


def test_put_binding_allowlist_invalid_id_rejected() -> None:
    engine = _engine()
    owner = _seed(engine)
    client = _client(engine)
    response = client.put(
        "/api/enterprise/channels/chan_discord?tenant_id=tenant_a",
        json={"allowlist": {"channel_ids": ["  "]}},
        headers=_auth(owner),
    )
    assert response.status_code == 422


def test_put_binding_allowlist_rejects_non_manager() -> None:
    engine = _engine()
    _seed(engine)
    client = _client(engine)
    other = User(id="user_other", tenant_id="tenant_a", username="other", password_hash="x")
    response = client.put(
        "/api/enterprise/channels/chan_discord?tenant_id=tenant_a",
        json={"allowlist": {"channel_ids": ["chan_1"]}},
        headers=_auth(other),
    )
    assert response.status_code == 403


# ---------- 功能3:批处理端点 ----------


def _fake_batch_service() -> tuple[BatchService, list]:
    service = BatchService()
    calls: list[dict] = []

    def fake_submit(binding, tenant_id, target, items, *, thread_id=None, client_batch_id=None, db_engine=None, autostart=True):
        calls.append(
            {
                "binding_id": binding.id,
                "tenant_id": tenant_id,
                "target": target,
                "items": items,
                "thread_id": thread_id,
                "client_batch_id": client_batch_id,
            }
        )
        job = BatchJob(
            job_id="job_batch_1",
            binding_id=binding.id,
            tenant_id=tenant_id,
            target=target,
            items=items,
            thread_id=thread_id,
            client_batch_id=client_batch_id,
            status="done",
            progress=len(items),
            succeeded=len(items),
        )
        with service._lock:
            service._batch_jobs["job_batch_1"] = job
        return "job_batch_1"

    service.submit_batch = fake_submit  # type: ignore[method-assign]
    return service, calls


def test_post_batch_submits_job_and_returns_job_id(monkeypatch) -> None:
    engine = _engine()
    owner = _seed(engine)
    service, calls = _fake_batch_service()
    monkeypatch.setattr(channels_api, "batch_service", service)
    client = _client(engine)

    response = client.post(
        "/api/enterprise/channels/chan_discord/batch?tenant_id=tenant_a&channel_id=chan_100",
        json={"items": ["hello", {"content": "rich", "embeds": [{"title": "t"}]}], "thread_id": "thread_9"},
        headers=_auth(owner),
    )
    assert response.status_code == 200
    assert response.json() == {"job_id": "job_batch_1", "status": "done"}
    assert calls == [
        {
            "binding_id": "chan_discord",
            "tenant_id": "tenant_a",
            "target": {
                "to_user_id": "",
                "channel_id": "chan_100",
                "guild_id": "",
                "thread_id": "thread_9",
            },
            "items": ["hello", {"content": "rich", "embeds": [{"title": "t"}], "files": []}],
            "thread_id": "thread_9",
            "client_batch_id": None,
        }
    ]


def test_post_batch_without_channel_id_400(monkeypatch) -> None:
    engine = _engine()
    owner = _seed(engine)
    service, _calls = _fake_batch_service()
    monkeypatch.setattr(channels_api, "batch_service", service)
    client = _client(engine)

    response = client.post(
        "/api/enterprise/channels/chan_discord/batch?tenant_id=tenant_a",
        json={"items": ["hello"]},
        headers=_auth(owner),
    )
    assert response.status_code == 400
    assert "缺少目标频道" in response.json()["detail"]


def test_post_batch_empty_items_rejected(monkeypatch) -> None:
    engine = _engine()
    owner = _seed(engine)
    service, _calls = _fake_batch_service()
    monkeypatch.setattr(channels_api, "batch_service", service)
    client = _client(engine)

    response = client.post(
        "/api/enterprise/channels/chan_discord/batch?tenant_id=tenant_a&channel_id=chan_100",
        json={"items": []},
        headers=_auth(owner),
    )
    assert response.status_code == 422


def test_get_batch_job_reports_progress(monkeypatch) -> None:
    engine = _engine()
    owner = _seed(engine)
    service = BatchService()
    with service._lock:
        service._batch_jobs["job_batch_1"] = BatchJob(
            job_id="job_batch_1",
            binding_id="chan_discord",
            tenant_id="tenant_a",
            target={"channel_id": "chan_100"},
            items=["a", "b"],
            status="done",
            progress=2,
            succeeded=2,
        )
    monkeypatch.setattr(channels_api, "batch_service", service)
    client = _client(engine)

    response = client.get(
        "/api/enterprise/channels/chan_discord/batch/job_batch_1?tenant_id=tenant_a",
        headers=_auth(owner),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "done"
    assert payload["progress"] == 2
    assert payload["total"] == 2
    assert payload["succeeded"] == 2
    assert payload["failed"] == 0
    assert payload["errors"] == []


def test_get_batch_job_wrong_binding_404(monkeypatch) -> None:
    engine = _engine()
    owner = _seed(engine)
    service, _calls = _fake_batch_service()
    monkeypatch.setattr(channels_api, "batch_service", service)
    client = _client(engine)

    response = client.get(
        "/api/enterprise/channels/chan_other/batch/job_batch_1?tenant_id=tenant_a",
        headers=_auth(owner),
    )
    assert response.status_code == 404


def test_batch_endpoints_require_authentication() -> None:
    engine = _engine()
    client = _client(engine)
    assert client.post("/api/enterprise/channels/chan_discord/batch", json={"items": ["x"]}).status_code == 401
    assert client.get("/api/enterprise/channels/chan_discord/batch/job_1").status_code == 401


# ---------- 功能4:回填端点 ----------


def test_post_backfill_submits_job(monkeypatch) -> None:
    engine = _engine()
    owner = _seed(engine)
    service = BatchService()
    calls: list[dict] = []

    def fake_submit(binding, *, channel_id, limit=100, after=None, before=None, db_engine=None, autostart=True):
        calls.append({"channel_id": channel_id, "limit": limit, "after": after, "before": before})
        from app.channels.batch_service import BackfillJob

        job = BackfillJob(
            job_id="bfill_1",
            binding_id=binding.id,
            channel_id=channel_id,
            limit=limit,
            after=after,
            before=before,
            status="running",
        )
        with service._lock:
            service._backfill_jobs["bfill_1"] = job
        return "bfill_1"

    service.submit_backfill = fake_submit  # type: ignore[method-assign]
    monkeypatch.setattr(channels_api, "batch_service", service)
    client = _client(engine)

    response = client.post(
        "/api/enterprise/channels/chan_discord/backfill?tenant_id=tenant_a",
        json={"channel_id": "chan_200", "limit": 50, "before": "msg_100"},
        headers=_auth(owner),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["job_id"] == "bfill_1"
    assert payload["status"] == "running"
    assert calls == [{"channel_id": "chan_200", "limit": 50, "after": None, "before": "msg_100"}]


def test_post_backfill_without_channel_id_400(monkeypatch) -> None:
    engine = _engine()
    owner = _seed(engine)
    service = BatchService()
    monkeypatch.setattr(channels_api, "batch_service", service)
    client = _client(engine)

    response = client.post(
        "/api/enterprise/channels/chan_discord/backfill?tenant_id=tenant_a",
        json={},
        headers=_auth(owner),
    )
    assert response.status_code == 400
    assert "缺少目标频道" in response.json()["detail"]


def test_post_backfill_rejects_non_discord_binding(monkeypatch) -> None:
    engine = _engine()
    owner = _seed(engine, channel="wechat")
    service = BatchService()
    monkeypatch.setattr(channels_api, "batch_service", service)
    client = _client(engine)

    response = client.post(
        "/api/enterprise/channels/chan_discord/backfill?tenant_id=tenant_a",
        json={"channel_id": "chan_200"},
        headers=_auth(owner),
    )
    assert response.status_code == 400
    assert "不是 Discord 渠道" in response.json()["detail"]


def test_backfill_job_query_and_404(monkeypatch) -> None:
    engine = _engine()
    owner = _seed(engine)
    service = BatchService()
    from app.channels.batch_service import BackfillJob

    with service._lock:
        service._backfill_jobs["bfill_9"] = BackfillJob(
            job_id="bfill_9",
            binding_id="chan_discord",
            channel_id="chan_200",
            status="done",
            written=3,
            duplicates=1,
        )
    monkeypatch.setattr(channels_api, "batch_service", service)
    client = _client(engine)

    response = client.get(
        "/api/enterprise/channels/chan_discord/backfill/bfill_9?tenant_id=tenant_a",
        headers=_auth(owner),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["written"] == 3
    assert payload["duplicates"] == 1

    missing = client.get(
        "/api/enterprise/channels/chan_discord/backfill/bfill_missing?tenant_id=tenant_a",
        headers=_auth(owner),
    )
    assert missing.status_code == 404


# ---------- §4.4 回填数据 web 可见性 ----------


def _seed_backfill_conversation(engine, owner: User) -> str:
    """回填会话 + 对话消息 + 3 条回填事件(2 条匹配频道、1 条其他频道)。"""
    with Session(engine) as db:
        db.add(
            ChatSession(
                id="session_backfill",
                tenant_id="tenant_a",
                user_id=owner.id,
                agent_id="agent_a",
                channel="discord",
                external_conv_id="discord_group_chan_200",
                channel_binding_id="chan_discord",
            )
        )
        db.add(
            Message(
                id="msg_web_1",
                tenant_id="tenant_a",
                session_id="session_backfill",
                role="user",
                content="web 对话消息",
                created_at=datetime(2026, 8, 10, 10, 0, 0),
            )
        )
        events = [
            # 用户历史消息 → role=user
            ChannelInboundEvent(
                id="chevt_backfill_1",
                tenant_id="tenant_a",
                binding_id="chan_discord",
                channel="discord",
                event_id="hist_1",
                payload_json={
                    "schema_version": 1,
                    "backfilled": True,
                    "message": {
                        "id": "hist_1",
                        "channel_id": "chan_200",
                        "guild_id": "guild_1",
                        "author_id": "user_1",
                        "content": "历史消息一",
                        "created_at": "2026-08-09T09:00:00+00:00",
                    },
                },
                target_json={
                    "to_user_id": "user_1",
                    "channel_id": "chan_200",
                    "guild_id": "guild_1",
                    "message_id": "hist_1",
                },
                status="backfilled",
                created_at=utc_now(),
            ),
            # bot 历史消息 → role=assistant
            ChannelInboundEvent(
                id="chevt_backfill_2",
                tenant_id="tenant_a",
                binding_id="chan_discord",
                channel="discord",
                event_id="hist_2",
                payload_json={
                    "schema_version": 1,
                    "backfilled": True,
                    "message": {
                        "id": "hist_2",
                        "channel_id": "chan_200",
                        "guild_id": "guild_1",
                        "author_id": "bot-123",
                        "content": "bot 历史回复",
                        "created_at": "2026-08-09T09:05:00+00:00",
                    },
                },
                target_json={
                    "to_user_id": "bot-123",
                    "channel_id": "chan_200",
                    "guild_id": "guild_1",
                    "message_id": "hist_2",
                },
                status="backfilled",
                created_at=utc_now(),
            ),
            # 其他频道回填 → 不应出现在本会话
            ChannelInboundEvent(
                id="chevt_other",
                tenant_id="tenant_a",
                binding_id="chan_discord",
                channel="discord",
                event_id="hist_9",
                payload_json={
                    "schema_version": 1,
                    "backfilled": True,
                    "message": {
                        "id": "hist_9",
                        "channel_id": "chan_999",
                        "guild_id": "guild_9",
                        "author_id": "user_9",
                        "content": "其他频道消息",
                        "created_at": "2026-08-08T09:00:00+00:00",
                    },
                },
                target_json={
                    "to_user_id": "user_9",
                    "channel_id": "chan_999",
                    "guild_id": "guild_9",
                    "message_id": "hist_9",
                },
                status="backfilled",
                created_at=utc_now(),
            ),
        ]
        for event in events:
            db.add(event)
        db.commit()
    return "session_backfill"


def test_list_conversation_messages_merges_backfilled_events() -> None:
    """回填历史消息与对话消息统一按时间排序出现在 web 端点;其他频道不混入。"""
    engine = _engine()
    owner = _seed(engine)
    session_id = _seed_backfill_conversation(engine, owner)

    response = _client(engine).get(
        f"/api/enterprise/channels/chan_discord/conversations/{session_id}/messages?tenant_id=tenant_a",
        headers=_auth(owner),
    )
    assert response.status_code == 200
    items = response.json()
    assert [item["content"] for item in items] == ["历史消息一", "bot 历史回复", "web 对话消息"]
    assert [item["role"] for item in items] == ["user", "assistant", "user"]
    assert all("其他频道" not in item["content"] for item in items)


def test_list_conversation_messages_backfilled_without_timestamp_falls_back() -> None:
    """回填消息缺 created_at 时兜底用事件写入时间,不报错。"""
    engine = _engine()
    owner = _seed(engine)
    with Session(engine) as db:
        db.add(
            ChatSession(
                id="session_backfill_2",
                tenant_id="tenant_a",
                user_id=owner.id,
                agent_id="agent_a",
                channel="discord",
                external_conv_id="discord_group_chan_200",
                channel_binding_id="chan_discord",
            )
        )
        db.add(
            ChannelInboundEvent(
                id="chevt_no_ts",
                tenant_id="tenant_a",
                binding_id="chan_discord",
                channel="discord",
                event_id="hist_ts",
                payload_json={
                    "schema_version": 1,
                    "backfilled": True,
                    "message": {"id": "hist_ts", "channel_id": "chan_200", "content": "无时间戳"},
                },
                target_json={"to_user_id": "user_1", "channel_id": "chan_200", "message_id": "hist_ts"},
                status="backfilled",
                created_at=datetime(2026, 8, 7, 8, 0, 0),
            )
        )
        db.commit()

    response = _client(engine).get(
        "/api/enterprise/channels/chan_discord/conversations/session_backfill_2/messages?tenant_id=tenant_a",
        headers=_auth(owner),
    )
    assert response.status_code == 200
    items = response.json()
    assert items == [
        {
            "id": "hist_ts",
            "role": "user",
            "content": "无时间戳",
            "created_at": "2026-08-07T08:00:00",
            "attachments": None,
        }
    ]


def test_list_conversation_messages_skips_backfill_for_private_conv() -> None:
    """私聊会话(external_conv_id 无 _group_ 标记)不合并回填数据。"""
    engine = _engine()
    owner = _seed(engine)
    with Session(engine) as db:
        db.add(
            ChatSession(
                id="session_dm",
                tenant_id="tenant_a",
                user_id=owner.id,
                agent_id="agent_a",
                channel="discord",
                external_conv_id="discord_p2p_user_1",
                channel_binding_id="chan_discord",
            )
        )
        db.add(
            ChannelInboundEvent(
                id="chevt_dm_1",
                tenant_id="tenant_a",
                binding_id="chan_discord",
                channel="discord",
                event_id="dm_hist_1",
                payload_json={
                    "schema_version": 1,
                    "backfilled": True,
                    "message": {"id": "dm_hist_1", "channel_id": "chan_200", "content": "私聊不应出现"},
                },
                target_json={"to_user_id": "user_1", "channel_id": "chan_200", "message_id": "dm_hist_1"},
                status="backfilled",
                created_at=utc_now(),
            )
        )
        db.commit()

    response = _client(engine).get(
        "/api/enterprise/channels/chan_discord/conversations/session_dm/messages?tenant_id=tenant_a",
        headers=_auth(owner),
    )
    assert response.status_code == 200
    assert response.json() == []


# ---------- CHANNEL_META 能力声明 ----------


def test_channel_meta_declares_discord_capabilities() -> None:
    engine = _engine()
    owner = _seed(engine)
    response = _client(engine).get(
        "/api/enterprise/channels/meta?tenant_id=tenant_a",
        headers=_auth(owner),
    )
    assert response.status_code == 200
    discord = next(row for row in response.json() if row["channel"] == "discord")
    assert set(discord["capabilities"]) == {
        "slash_commands",
        "threads",
        "auto_thread",
        "batch_send",
        "backfill",
        "typing",
        "voice",
        "rich_media",
    }
