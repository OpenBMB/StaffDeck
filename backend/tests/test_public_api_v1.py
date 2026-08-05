from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db import get_session
from app.db.models import (
    APIClient,
    APICredential,
    APIJob,
    APIJobEvent,
    APISOPDraft,
    AgentEvent,
    AgentProfile,
    ChatSession,
    Message,
    Skill,
    Tenant,
    User,
)
from app.api.agents import (
    create_agent_api_credential,
    list_agent_api_credentials,
    revoke_agent_api_credential,
    rotate_agent_api_credential,
)
from app.api.auth import (
    AccountAPICredentialCreateRequest,
    create_account_api_credential,
    list_account_api_credentials,
    revoke_account_api_credential,
    rotate_account_api_credential,
)
from app.agents.schema import AgentAPICredentialCreateRequest
from app.public_api.credential_profiles import (
    AGENT_RUNTIME_SCOPES,
    USER_FULL_ACCESS_SCOPES,
)
from app.public_api.app import create_public_api_app
from app.public_api.json_patch import JSONPatchError, apply_json_patch
from app.public_api.runs import execute_run
from app.security.auth import create_access_token
from app.session.helpers import public_session
from app.session.session_schema import ChatTurnResponse


def _skill_card() -> dict:
    return {
        "skill_id": "expense_policy_v1",
        "name": "报销制度",
        "version": "1.0.0",
        "description": "回答报销政策",
        "trigger_intents": ["查询报销制度"],
        "nodes": [
            {
                "node_id": "answer",
                "type": "respond",
                "name": "回答",
                "instruction": "依据制度回答并给出引用",
                "capability_refs": {
                    "general_skill_ids": [],
                    "tool_ids": [],
                    "knowledge_base_ids": [],
                },
            }
        ],
        "edges": [],
        "start_node_id": "answer",
        "terminal_node_ids": ["answer"],
    }


def _client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_api", name="API Tenant"))
        admin = User(
            id="user_api_admin",
            tenant_id="tenant_api",
            username="api_admin",
            role="admin",
            password_hash="x",
        )
        db.add(admin)
        db.add(
            AgentProfile(
                id="agent_api",
                tenant_id="tenant_api",
                name="API Employee",
                status="active",
                is_overall=False,
                metadata_json={"owner_user_id": admin.id, "owner_username": admin.username},
            )
        )
        db.add(
            AgentProfile(
                id="agent_other",
                tenant_id="tenant_api",
                name="Other Employee",
                status="active",
                is_overall=False,
                metadata_json={"owner_user_id": admin.id, "owner_username": admin.username},
            )
        )
        db.commit()
        token = create_access_token(admin)

    app = create_public_api_app()

    def session_override():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_session] = session_override
    monkeypatch.setattr("app.public_api.app.engine", engine)
    monkeypatch.setattr("app.public_api.jobs.enqueue_async_job", lambda *args, **kwargs: "queued")
    return TestClient(app), engine, token


def _tenant_key(client: TestClient, admin_token: str, scopes: list[str]) -> str:
    created = client.post(
        "/api-clients",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"name": "integration", "scopes": ["*"]},
    )
    assert created.status_code == 201, created.text
    credential = client.post(
        f"/api-clients/{created.json()['id']}/credentials",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"name": "tenant runtime", "scopes": scopes},
    )
    assert credential.status_code == 201, credential.text
    return credential.json()["api_key"]


def test_problem_details_and_openapi_contract(monkeypatch) -> None:
    client, _engine, _token = _client(monkeypatch)
    unauthorized = client.get("/agents")
    assert unauthorized.status_code == 401
    assert unauthorized.headers["content-type"].startswith("application/problem+json")
    assert unauthorized.json()["code"] == "NOT_AUTHENTICATED"
    assert unauthorized.json()["request_id"].startswith("req_")

    schema = client.get("/openapi.json").json()
    expected = {
        "/agents/{agent_id}/runs",
        "/agents/{agent_id}/runs:stream",
        "/runs/{run_id}/events",
        "/agents/{agent_id}/sops:generate",
        "/agents/{agent_id}/sops/{sop_id}",
        "/sops/{sop_id}:publish",
        "/agents/{agent_id}/knowledge-bases/{knowledge_base_id}/entries",
        "/agents/{agent_id}/tools",
        "/agents/{agent_id}/scheduled-tasks",
        "/gallery/agents",
        "/gallery/agents/{agent_id}:add",
    }
    assert expected.issubset(schema["paths"])


def test_api_key_scope_agent_boundary_and_idempotent_run(monkeypatch) -> None:
    client, engine, admin_token = _client(monkeypatch)
    tenant_key = _tenant_key(client, admin_token, ["agents:read", "runs:create", "runs:read"])
    headers = {"Authorization": f"Bearer {tenant_key}", "Idempotency-Key": "run-order-1"}

    first = client.post(
        "/agents/agent_api/runs",
        headers=headers,
        json={"input": "查询制度", "session_mode": "stateless"},
    )
    second = client.post(
        "/agents/agent_api/runs",
        headers=headers,
        json={"input": "查询制度", "session_mode": "stateless"},
    )
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    with Session(engine) as db:
        assert len(db.exec(select(APIJob)).all()) == 1

    client_row = client.get(
        "/api-clients",
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()[0]
    employee_credential = client.post(
        f"/api-clients/{client_row['id']}/credentials",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "name": "employee runtime",
            "agent_id": "agent_api",
            "scopes": ["agents:read", "runs:create", "runs:read"],
        },
    )
    assert employee_credential.status_code == 201, employee_credential.text
    employee_key = employee_credential.json()["api_key"]
    assert client.get(
        "/agents/agent_other",
        headers={"Authorization": f"Bearer {employee_key}"},
    ).status_code == 403


def test_streaming_run_endpoint_emits_reply_deltas(monkeypatch) -> None:
    client, engine, admin_token = _client(monkeypatch)
    tenant_key = _tenant_key(client, admin_token, ["agents:read", "runs:create", "runs:read"])

    class FakeStreamingLoop:
        def __init__(self, db):
            self.db = db

        def handle_turn_stream(self, request):
            session = self.db.get(ChatSession, request.session_id)
            self.db.add(
                AgentEvent(
                    tenant_id=request.tenant_id,
                    session_id=request.session_id,
                    event_type="stream_delta",
                    payload_json={"content": "流式答复", "turn_id": request.client_turn_id},
                )
            )
            self.db.add(
                AgentEvent(
                    tenant_id=request.tenant_id,
                    session_id=request.session_id,
                    event_type="stream_end",
                    payload_json={"turn_id": request.client_turn_id},
                )
            )
            self.db.add(
                Message(
                    tenant_id=request.tenant_id,
                    session_id=request.session_id,
                    role="assistant",
                    content="流式答复",
                    metadata_json={"client_turn_id": request.client_turn_id},
                )
            )
            self.db.commit()
            response = ChatTurnResponse(
                reply="流式答复",
                session_id=request.session_id,
                session_state=public_session(session),
            )
            yield {"event": "complete", "data": response.model_dump(mode="json")}

    monkeypatch.setattr("app.public_api.jobs.engine", engine)
    monkeypatch.setattr("app.public_api.runs.engine", engine)
    monkeypatch.setattr("app.public_api.runs.AgentLoop", FakeStreamingLoop)
    monkeypatch.setattr(
        "app.public_api.jobs.enqueue_async_job",
        lambda _name, func, job_id: func(job_id),
    )
    response = client.post(
        "/agents/agent_api/runs:stream",
        headers={
            "Authorization": f"Bearer {tenant_key}",
            "Idempotency-Key": "stream-run-1",
            "Accept": "text/event-stream",
        },
        json={"input": "请流式回答", "session_mode": "stateless"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-run-id"].startswith("apijob_")
    assert "event: run.output.delta" in response.text
    assert '"content": "流式答复"' in response.text
    assert "event: run.output.completed" in response.text


def test_sop_changes_remain_isolated_until_publish(monkeypatch) -> None:
    client, engine, admin_token = _client(monkeypatch)
    key = _tenant_key(client, admin_token, ["sops:read", "sops:write", "sops:publish"])
    headers = {"Authorization": f"Bearer {key}", "Idempotency-Key": "sop-create-1"}
    created = client.post(
        "/agents/agent_api/sops",
        headers=headers,
        json={"content": _skill_card()},
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    with Session(engine) as db:
        assert db.exec(select(APISOPDraft)).first() is not None
        assert db.exec(select(Skill).where(Skill.skill_id == "expense_policy_v1")).first() is None

    missing_etag = client.patch(
        f"/agents/agent_api/sops/expense_policy_v1?draft_id={draft['id']}",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json-patch+json"},
        json=[{"op": "replace", "path": "/description", "value": "新版"}],
    )
    assert missing_etag.status_code == 428

    patched = client.patch(
        f"/agents/agent_api/sops/expense_policy_v1?draft_id={draft['id']}",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json-patch+json",
            "If-Match": draft["etag"],
        },
        json=[{"op": "replace", "path": "/description", "value": "新版"}],
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["content"]["description"] == "新版"
    stale = client.patch(
        f"/agents/agent_api/sops/expense_policy_v1?draft_id={draft['id']}",
        headers={"Authorization": f"Bearer {key}", "If-Match": '"stale"'},
        json=[{"op": "replace", "path": "/description", "value": "覆盖"}],
    )
    assert stale.status_code == 412
    validation = client.post(
        f"/sops/expense_policy_v1:validate?agent_id=agent_api&draft_id={draft['id']}",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert validation.status_code == 200, validation.text
    assert validation.json()["valid"] is True
    published = client.post(
        "/sops/expense_policy_v1:publish?agent_id=agent_api",
        headers={"Authorization": f"Bearer {key}"},
        json={"draft_id": draft["id"]},
    )
    assert published.status_code == 200, published.text
    with Session(engine) as db:
        assert db.exec(select(Skill).where(Skill.skill_id == "expense_policy_v1")).first() is not None
        assert db.get(APISOPDraft, draft["id"]).status == "published"


def test_rfc6902_supports_array_append_move_copy_and_test() -> None:
    source = {"nodes": [{"id": "a"}], "meta": {"owner": "x"}}
    patched = apply_json_patch(
        source,
        [
            {"op": "test", "path": "/meta/owner", "value": "x"},
            {"op": "add", "path": "/nodes/-", "value": {"id": "b"}},
            {"op": "copy", "from": "/meta/owner", "path": "/meta/reviewer"},
            {"op": "move", "from": "/nodes/0", "path": "/nodes/1"},
        ],
    )
    assert patched["nodes"] == [{"id": "b"}, {"id": "a"}]
    assert patched["meta"]["reviewer"] == "x"
    assert source == {"nodes": [{"id": "a"}], "meta": {"owner": "x"}}
    try:
        apply_json_patch(source, [{"op": "test", "path": "/meta/owner", "value": "no"}])
    except JSONPatchError:
        pass
    else:
        raise AssertionError("A failed RFC6902 test operation must abort the patch")


def test_resource_wrappers_derive_tenant_and_mask_tool_secrets(monkeypatch) -> None:
    client, _engine, admin_token = _client(monkeypatch)
    key = _tenant_key(
        client,
        admin_token,
        [
            "knowledge:read",
            "knowledge:write",
            "skills:read",
            "skills:write",
            "tools:read",
            "tools:write",
            "scheduled_tasks:read",
            "scheduled_tasks:write",
        ],
    )
    auth = {"Authorization": f"Bearer {key}"}
    knowledge = client.post(
        "/agents/agent_api/knowledge-bases",
        headers=auth,
        json={"name": "Policy", "description": "Expense policy", "capability_scope": "general"},
    )
    assert knowledge.status_code == 201, knowledge.text
    assert client.get("/agents/agent_api/knowledge-bases", headers=auth).status_code == 200

    skill = client.post(
        "/agents/agent_api/general-skills",
        headers=auth,
        json={
            "name": "Weather",
            "slug": "weather",
            "markdown": "---\nname: Weather\ndescription: Weather lookup\n---\n# Weather\nUse the available weather source.",
            "status": "published",
            "capability_scope": "general",
        },
    )
    assert skill.status_code == 201, skill.text
    assert client.get("/agents/agent_api/general-skills", headers=auth).status_code == 200

    tool = client.post(
        "/agents/agent_api/tools",
        headers=auth,
        json={
            "name": "policy_api",
            "method": "GET",
            "url": "https://example.com/policy",
            "headers": {"Authorization": "Bearer secret"},
            "auth": {"token": "secret"},
            "capability_scope": "general",
        },
    )
    assert tool.status_code == 201, tool.text
    assert tool.json()["headers"]["Authorization"] == "********"
    assert tool.json()["auth"]["token"] == "********"
    listed = client.get("/agents/agent_api/tools", headers=auth)
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"][0]["headers"]["Authorization"] == "********"

    scheduled = client.post(
        "/agents/agent_api/scheduled-tasks",
        headers=auth,
        json={
            "title": "Daily policy check",
            "prompt": "检查报销制度更新",
            "schedule_type": "daily",
            "schedule": {"time": "09:00"},
            "timezone": "Asia/Shanghai",
        },
    )
    assert scheduled.status_code == 201, scheduled.text
    assert client.get("/agents/agent_api/scheduled-tasks", headers=auth).status_code == 200


def test_run_handler_relays_live_public_trace_and_returns_citations(monkeypatch) -> None:
    _client_value, engine, _token = _client(monkeypatch)
    with Session(engine) as db:
        client = APIClient(
            id="client_run",
            tenant_id="tenant_api",
            name="run-client",
            scopes_json=["runs:*"],
            created_by_user_id="user_api_admin",
        )
        credential = APICredential(
            id="credential_run",
            tenant_id="tenant_api",
            client_id=client.id,
            name="runtime",
            key_prefix="sd_live_test_prefix",
            key_digest="digest",
            scopes_json=["runs:create", "runs:read"],
        )
        job = APIJob(
            id="apijob_live_trace",
            tenant_id="tenant_api",
            credential_id=credential.id,
            agent_id="agent_api",
            kind="run",
            status="running",
            request_json={"input": "查询政策", "session_mode": "stateless"},
        )
        db.add(client)
        db.add(credential)
        db.add(job)
        db.commit()

    class FakeLoop:
        def __init__(self, db):
            self.db = db

        def handle_turn(self, request):
            session = self.db.get(ChatSession, request.session_id)
            self.db.add(
                AgentEvent(
                    tenant_id=request.tenant_id,
                    session_id=request.session_id,
                    event_type="turn_plan_created",
                    payload_json={
                        "decision": "answer_only",
                        "reason": "policy query",
                        "system_prompt": "must not leak",
                    },
                )
            )
            self.db.add(
                Message(
                    tenant_id=request.tenant_id,
                    session_id=request.session_id,
                    role="assistant",
                    content="制度答复 [1]",
                    metadata_json={
                        "client_turn_id": request.client_turn_id,
                        "knowledge_citations": [{"label": "[1]", "document_id": "doc_1"}],
                    },
                )
            )
            self.db.commit()
            return ChatTurnResponse(
                reply="制度答复 [1]",
                session_id=request.session_id,
                session_state=public_session(session),
            )

        def handle_turn_stream(self, request):
            response = self.handle_turn(request)
            self.db.add(
                AgentEvent(
                    tenant_id=request.tenant_id,
                    session_id=request.session_id,
                    event_type="stream_delta",
                    payload_json={"content": "制度答复 [1]", "turn_id": request.client_turn_id},
                )
            )
            self.db.add(
                AgentEvent(
                    tenant_id=request.tenant_id,
                    session_id=request.session_id,
                    event_type="stream_end",
                    payload_json={"turn_id": request.client_turn_id},
                )
            )
            self.db.commit()
            yield {"event": "complete", "data": response.model_dump(mode="json")}

    monkeypatch.setattr("app.public_api.runs.engine", engine)
    monkeypatch.setattr("app.public_api.runs.AgentLoop", FakeLoop)
    with Session(engine) as db:
        job = db.get(APIJob, "apijob_live_trace")
        result = execute_run(db, job)
        public_events = db.exec(
            select(APIJobEvent).where(APIJobEvent.job_id == job.id)
        ).all()
    assert result["citations"] == [{"label": "[1]", "document_id": "doc_1"}]
    plan_event = next(event for event in public_events if event.event_type == "run.plan")
    assert plan_event.data_json["decision"] == "answer_only"
    assert "system_prompt" not in plan_event.data_json
    output_event = next(event for event in public_events if event.event_type == "run.output.delta")
    assert output_event.data_json["content"] == "制度答复 [1]"


def test_employee_settings_manage_runtime_keys(monkeypatch) -> None:
    _client_value, engine, _token = _client(monkeypatch)
    with Session(engine) as db:
        admin = db.get(User, "user_api_admin")
        created = create_agent_api_credential(
            "agent_api",
            AgentAPICredentialCreateRequest(
                tenant_id="tenant_api",
                name="财务助手运行密钥",
                access="runtime",
            ),
            db,
            admin,
        )
        assert created.api_key.startswith("sd_live_")
        assert set(created.scopes) == set(AGENT_RUNTIME_SCOPES)
        assert "runs:create" in created.scopes
        assert "knowledge:read" not in created.scopes
        assert "knowledge:write" not in created.scopes

        stored = db.get(APICredential, created.id)
        assert stored is not None
        assert stored.key_digest != created.api_key

        listed = list_agent_api_credentials("agent_api", "tenant_api", db, admin)
        assert listed[0].access == "runtime"
        assert not hasattr(listed[0], "api_key")

        rotated = rotate_agent_api_credential(
            "agent_api", created.id, "tenant_api", db, admin
        )
        assert rotated.api_key.startswith("sd_live_")
        assert rotated.api_key != created.api_key

        revoked = revoke_agent_api_credential(
            "agent_api", created.id, "tenant_api", db, admin
        )
        assert revoked.status == "revoked"


def test_account_master_key_follows_user_visible_agents(monkeypatch) -> None:
    client, engine, _admin_token = _client(monkeypatch)
    with Session(engine) as db:
        admin = db.get(User, "user_api_admin")
        member = User(
            id="user_api_member",
            tenant_id="tenant_api",
            username="api_member",
            role="member",
            password_hash="x",
        )
        db.add(member)
        db.add_all(
            [
                AgentProfile(
                    id="agent_member_owned",
                    tenant_id="tenant_api",
                    name="Member Employee",
                    status="active",
                    is_overall=False,
                    metadata_json={"owner_user_id": member.id},
                ),
                AgentProfile(
                    id="agent_published",
                    tenant_id="tenant_api",
                    name="Published Employee",
                    status="active",
                    is_overall=False,
                    metadata_json={
                        "owner_user_id": admin.id,
                        "published_to_gallery": True,
                    },
                ),
                AgentProfile(
                    id="agent_private",
                    tenant_id="tenant_api",
                    name="Private Employee",
                    status="active",
                    is_overall=False,
                    metadata_json={"owner_user_id": admin.id},
                ),
                AgentProfile(
                    id="agent_overall",
                    tenant_id="tenant_api",
                    name="Overall Employee",
                    status="active",
                    is_overall=True,
                    metadata_json={"owner_user_id": admin.id},
                ),
            ]
        )
        db.commit()

        created = create_account_api_credential(
            AccountAPICredentialCreateRequest(
                name="成员账号全量密钥",
            ),
            member,
            db,
        )
        assert created.api_key.startswith("sd_live_")
        assert set(created.scopes) == set(USER_FULL_ACCESS_SCOPES)
        assert "knowledge:read" in created.scopes
        assert "knowledge:write" in created.scopes
        assert "gallery:use" in created.scopes
        stored = db.get(APICredential, created.id)
        assert stored is not None and stored.agent_id is None

    auth = {"Authorization": f"Bearer {created.api_key}"}
    listed_agents = client.get("/agents", headers=auth)
    assert listed_agents.status_code == 200, listed_agents.text
    agent_ids = {row["id"] for row in listed_agents.json()["data"]}
    assert {"agent_member_owned", "agent_published", "agent_overall"} <= agent_ids
    assert "agent_private" not in agent_ids
    assert "agent_api" not in agent_ids

    for agent_id in ("agent_member_owned", "agent_published", "agent_overall"):
        response = client.get(f"/agents/{agent_id}/capabilities", headers=auth)
        assert response.status_code == 200, response.text
    private_response = client.get("/agents/agent_private/capabilities", headers=auth)
    assert private_response.status_code == 404
    assert private_response.json()["code"] == "AGENT_NOT_FOUND"

    gallery = client.get("/gallery/agents", headers=auth)
    assert gallery.status_code == 200, gallery.text
    gallery_rows = gallery.json()["data"]
    assert [row["id"] for row in gallery_rows] == ["agent_published"]
    assert gallery_rows[0]["added"] is False

    added = client.post(
        "/gallery/agents/agent_published:add",
        headers={**auth, "Idempotency-Key": "add-published-1"},
    )
    replayed = client.post(
        "/gallery/agents/agent_published:add",
        headers={**auth, "Idempotency-Key": "add-published-1"},
    )
    assert added.status_code == replayed.status_code == 200
    assert added.json()["id"] == replayed.json()["id"] == "agent_published"
    assert added.json()["added"] is True
    assert client.get("/gallery/agents", headers=auth).json()["data"][0]["added"] is True
    assert client.post(
        "/gallery/agents/agent_private:add",
        headers={**auth, "Idempotency-Key": "add-private-1"},
    ).status_code == 404

    created_agent = client.post(
        "/agents",
        headers={**auth, "Idempotency-Key": "create-member-agent-1"},
        json={"name": "Member API Employee", "source_mode": "blank"},
    )
    assert created_agent.status_code == 201, created_agent.text
    assert created_agent.json()["metadata"]["owner_user_id"] == "user_api_member"
    published = client.get("/agents/agent_published", headers=auth)
    assert published.status_code == 200
    forbidden = client.patch(
        "/agents/agent_published",
        headers={**auth, "If-Match": published.headers["etag"]},
        json={"description": "不应允许修改"},
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "AGENT_MANAGE_FORBIDDEN"

    # Visibility is evaluated on every request, not frozen into the key.
    with Session(engine) as db:
        private = db.get(AgentProfile, "agent_private")
        private.metadata_json = {
            **dict(private.metadata_json or {}),
            "published_to_gallery": True,
        }
        db.add(private)
        db.commit()
    refreshed_agents = client.get("/agents", headers=auth)
    assert "agent_private" in {row["id"] for row in refreshed_agents.json()["data"]}

    with Session(engine) as db:
        member = db.get(User, "user_api_member")
        rows = list_account_api_credentials(member, db)
        assert rows[0].access == "user_full_access"
        assert not hasattr(rows[0], "api_key")
        rotated = rotate_account_api_credential(
            created.id, member, db
        )
        assert rotated.api_key != created.api_key
        revoked = revoke_account_api_credential(
            created.id, member, db
        )
        assert revoked.status == "revoked"


def test_admin_account_master_key_sees_all_visible_tenant_agents(monkeypatch) -> None:
    client, engine, _admin_token = _client(monkeypatch)
    with Session(engine) as db:
        admin = db.get(User, "user_api_admin")
        db.add(
            AgentProfile(
                id="agent_hidden",
                tenant_id="tenant_api",
                name="Hidden Employee",
                status="active",
                is_overall=False,
                metadata_json={
                    "owner_user_id": admin.id,
                    "hidden_from_staffdeck": True,
                },
            )
        )
        db.commit()
        created = create_account_api_credential(
            AccountAPICredentialCreateRequest(
                name="管理员账号全量密钥",
            ),
            admin,
            db,
        )

    response = client.get(
        "/agents",
        headers={"Authorization": f"Bearer {created.api_key}"},
    )
    assert response.status_code == 200, response.text
    agent_ids = {row["id"] for row in response.json()["data"]}
    assert {"agent_api", "agent_other"} <= agent_ids
    assert "agent_hidden" not in agent_ids


def test_public_api_rejects_new_full_access_employee_keys(monkeypatch) -> None:
    client, _engine, admin_token = _client(monkeypatch)
    api_client = client.post(
        "/api-clients",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"name": "employee-full-access", "scopes": ["*"]},
    ).json()
    credential = client.post(
        f"/api-clients/{api_client['id']}/credentials",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "name": "employee master key",
            "agent_id": "agent_api",
            "scopes": sorted(USER_FULL_ACCESS_SCOPES),
        },
    )
    assert credential.status_code == 400
    assert credential.json()["code"] == "AGENT_SCOPE_INVALID"
