from __future__ import annotations

import pytest
from app.api import chat
from app.db.models import AgentProfile, ChatSession, Message, Tenant, User
from app.session.session_schema import ChatTurnRequest
from fastapi import HTTPException
from staffdeck_harness.contracts.errors import AuthorizationUnavailable, PermissionDenied
from test_chat_agent_binding import _test_session


def denied():
    return PermissionDenied(
        "share revoked",
        details={
            "operation": "staff.use/v1",
            "resource_type": "agent",
            "resource": "shared-agent",
        },
    )


@pytest.mark.parametrize("outage", [False, True])
def test_history_survives_revocation_and_authorization_outage(monkeypatch, outage):
    from staffdeck_harness.composition import sources

    def authorize(*args):
        raise AuthorizationUnavailable("authorization unavailable") if outage else denied()

    monkeypatch.setattr(sources, "authorize_staff", authorize)
    with _test_session() as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        user = User(
            id="recipient", tenant_id="tenant_demo", username="recipient", password_hash="x"
        )
        db.add(user)
        db.add(AgentProfile(id="shared-agent", tenant_id="tenant_demo", name="共享客服"))
        row = ChatSession(
            id="session_revoked", tenant_id="tenant_demo", user_id=user.id, agent_id="shared-agent"
        )
        db.add(row)
        db.add(
            Message(
                id="old-answer",
                tenant_id="tenant_demo",
                session_id=row.id,
                role="assistant",
                content="共享撤销前的回复",
            )
        )
        db.commit()
        detail = chat.get_chat_session(row.id, "tenant_demo", current_user=user, db=db)
        assert detail.agent_available is (None if outage else False)
        assert detail.agent_name == "共享客服"
        assert (
            chat.list_chat_messages(row.id, "tenant_demo", current_user=user, db=db)[0].content
            == "共享撤销前的回复"
        )
        request = ChatTurnRequest(
            tenant_id="tenant_demo", session_id=row.id, agent_id=row.agent_id, message="继续提问"
        )
        # Both synchronous and stream entry points must reject before starting any worker.
        with pytest.raises(HTTPException) as caught:
            chat.chat_stream(request, current_user=user, db=db)
        assert caught.value.status_code == (503 if outage else 403)
        assert caught.value.detail["code"] == (
            "AUTHORIZATION_UNAVAILABLE" if outage else "AGENT_UNAVAILABLE"
        )
        assert len(chat.list_chat_messages(row.id, "tenant_demo", current_user=user, db=db)) == 1
        other = User(id="other", tenant_id="tenant_demo", username="other", password_hash="x")
        with pytest.raises(HTTPException):
            chat.get_chat_session(row.id, "tenant_demo", current_user=other, db=db)


def test_only_target_staff_denials_are_classified_as_employee_unavailable():
    assert chat._is_chat_agent_unavailable(denied(), "shared-agent")
    assert not chat._is_chat_agent_unavailable(denied(), "another-agent")
    assert not chat._is_chat_agent_unavailable(
        PermissionDenied("source identity does not match request"), "shared-agent"
    )
    assert not chat._is_chat_agent_unavailable(
        PermissionDenied(
            "tool denied",
            details={
                "operation": "tool.invoke/v1",
                "resource_type": "tool",
                "resource": "shared-agent",
            },
        ),
        "shared-agent",
    )
    assert not chat._is_chat_agent_unavailable(
        AuthorizationUnavailable("authorization unavailable"), "shared-agent"
    )
