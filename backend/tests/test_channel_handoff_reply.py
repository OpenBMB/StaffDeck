"""/回复反馈 指令的确认回执必须按渠道构造投递目标
回归 backdrop:确认回执曾写死飞书的 receive_id/receive_id_type,而该指令对四个
渠道都开放。微信的 send() 读 to_user_id、钉钉读 session_webhook,收到飞书格式会直接抛错,处理人拿不到"已收到你的回复"。
"""

from __future__ import annotations

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.channels.service_intake as intake_mod
from app.channels.adapters.base import ChannelInbound
from app.channels.crypto import encrypt_channel_secret
from app.channels.service_intake import _run_handoff_reply_command
from app.channels.service_routing import ChannelCommand
from app.db.models import (
    ChannelBinding,
    ChannelDelivery,
    ChannelIdentity,
    ChatSession,
    HumanHandoffRequest,
    Tenant,
    User,
    utc_now,
)

# 各渠道入站时构造并暂存到 ChannelInboundEvent.target_json 的投递目标,
# 与 feishu_runtime / service_wecom_inbox / service_dingtalk_inbox 保持一致;
# 微信不进暂存守护,走 process_inbound 的兜底 target
# required_key 是该渠道 send() 用来定位收件人的字段,缺失即投递失败
CHANNEL_TARGETS = {
    "feishu": (
        {
            "message_id": "evt_1",
            "reply_in_thread": False,
            "receive_id_type": "open_id",
            "receive_id": "u_assignee",
        },
        "receive_id",
    ),
    "wecom": ({"to_user_id": "u_assignee", "context_token": "ctx_1"}, "to_user_id"),
    "wechat": ({"to_user_id": "u_assignee", "context_token": "ctx_1"}, "to_user_id"),
    "dingtalk": (
        {
            "to_user_id": "u_assignee",
            "context_token": "https://oapi.dingtalk.com/robot/send?access_token=t",
            "session_webhook": "https://oapi.dingtalk.com/robot/send?access_token=t",
            "session_webhook_expired_time": 0,
            "conversation_id": "cid_1",
            "conversation_type": "1",
            "message_id": "evt_1",
        },
        "session_webhook",
    ),
}

def _test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _seed(db: Session, channel: str) -> ChannelBinding:
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.add(
        User(
            id="assignee_user",
            tenant_id="tenant_demo",
            username="assignee",
            display_name="指派人",
            password_hash="x",
        )
    )
    binding = ChannelBinding(
        id=f"binding_{channel}",
        tenant_id="tenant_demo",
        agent_id="agent_demo",
        channel=channel,
        status="active",
        config_json={},
        credentials_enc=encrypt_channel_secret("secret-value"),
        external_account_key=f"{channel}:test",
        config_revision=1,
    )
    db.add(binding)
    db.add(
        ChannelIdentity(
            tenant_id="tenant_demo",
            channel=channel,
            external_account_scope="",
            external_user_id="u_assignee",
            staffdeck_user_id="assignee_user",
        )
    )
    db.add(
        ChatSession(
            id="session_1",
            tenant_id="tenant_demo",
            agent_id="agent_demo",
            status="handoff",
        )
    )
    db.add(
        HumanHandoffRequest(
            id="handoff_1",
            tenant_id="tenant_demo",
            session_id="session_1",
            agent_id="agent_demo",
            assignee_user_id="assignee_user",
            pending_question="网络故障",
            context_summary="user: 网络断了",
            status="pending",
        )
    )
    db.commit()
    return binding

def _inbound(channel: str) -> ChannelInbound:
    return ChannelInbound(
        channel=channel,
        event_id="evt_1",
        from_user_id="u_assignee",
        to_user_id="u_bot",
        session_id="u_assignee",
        group_id="",
        context_token="ctx_1",
        text="/回复反馈 已修复网络",
        is_group=False,
        raw={},
    )

def _run(db: Session, binding: ChannelBinding, channel: str, monkeypatch):
    """执行 /回复反馈 指令,返回 (回执投递, _apply_handoff_reply 收到的 source)"""
    seen: list[str] = []
    def fake_apply(db_arg, row, reply, *, answered_by_user_id, source="web"):
        row.status = "answered"
        row.human_reply = reply
        row.answered_at = utc_now()
        db_arg.add(row)
        db_arg.commit()
        seen.append(source)


    import app.api.chat as chat_api
    monkeypatch.setattr(chat_api, "_apply_handoff_reply", fake_apply)
    monkeypatch.setattr(intake_mod, "external_account_scope", lambda _db, _b: "")
    target, _ = CHANNEL_TARGETS[channel]
    result = _run_handoff_reply_command(
        db,
        binding,
        _inbound(channel),
        ChannelCommand(kind="handoff_reply", query="已修复网络"),
        dict(target),
    )
    assert result is intake_mod._HANDOFF_REPLY_HANDLED
    ack = db.exec(
        select(ChannelDelivery).where(ChannelDelivery.kind == "handoff_ack")
    ).first()
    assert ack is not None
    return ack, seen


@pytest.mark.parametrize("channel", sorted(CHANNEL_TARGETS))
def test_handoff_reply_ack_uses_channel_target(channel: str, monkeypatch) -> None:
    """回执投递目标沿用本渠道的target，而非写死飞书字段"""
    engine = _test_engine()
    with Session(engine) as db:
        binding = _seed(db, channel)
        ack, _ = _run(db, binding, channel, monkeypatch)

        target, required_key = CHANNEL_TARGETS[channel]
        assert ack.target_json == target
        # send靠该字段定位收件人,缺失时适配器直接抛错,回执永远送不达
        assert ack.target_json.get(required_key)
        assert "已收到你的回复" in ack.text
@pytest.mark.parametrize("channel", sorted(CHANNEL_TARGETS))
def test_handoff_reply_records_originating_channel(channel: str, monkeypatch) -> None:
    """人工答复来源记为实际渠道,不再一律记成feishu"""
    engine = _test_engine()
    with Session(engine) as db:
        binding = _seed(db, channel)
        _, seen = _run(db, binding, channel, monkeypatch)

        assert seen == [channel]
        assert db.get(HumanHandoffRequest, "handoff_1").status == "answered"

@pytest.mark.parametrize("channel", ["wecom", "wechat", "dingtalk"])
def test_feishu_shaped_target_is_undeliverable_elsewhere(channel: str) -> None:
    """写死的飞书目标在其他渠道上不可投递——这正是本次修复要避免的回归
    三个适配器都在触网前校验目标字段,故此断言与网络无关
    """
    from app.channels.adapters.dingtalk import DingTalkAdapter
    from app.channels.adapters.wechat import WeChatAdapter
    from app.channels.adapters.wecom import WeComAdapter
    adapter = {
        "wecom": WeComAdapter,
        "wechat": WeChatAdapter,
        "dingtalk": DingTalkAdapter,
    }[channel]()
    binding = ChannelBinding(
        id=f"binding_{channel}",
        tenant_id="tenant_demo",
        agent_id="agent_demo",
        channel=channel,
        status="active",
        config_json={},
    )
    feishu_target = {"receive_id_type": "open_id", "receive_id": "u_assignee"}
    with pytest.raises(Exception) as excinfo:
        adapter.send(binding, feishu_target, "已收到你的回复")
    assert "目标" in str(excinfo.value)
