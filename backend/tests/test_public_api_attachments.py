from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select
from test_public_api_v1 import _client, _tenant_key

from app.api import chat as chat_api
from app.config import get_settings
from app.core.harness_attachments import materialize_task_attachments
from app.core.harness_session_cleanup import harness_task_workspace_path
from app.db.models import APIJob
from app.public_api import attachments as public_attachments
from app.public_api import runs as public_runs
from app.session.attachment_store import read_staged_chat_attachment
from app.session.session_schema import ChatAttachmentRead


def _upload(
    client,
    token: str,
    files: list[tuple[str, tuple[str, bytes, str]]],
    agent_id: str = "agent_api",
):
    return client.post(
        f"/agents/{agent_id}/attachments",
        headers={"Authorization": f"Bearer {token}"},
        files=[("files[]", value) for _field, value in files],
    )


def _create_key(
    client,
    admin_token: str,
    name: str,
    scopes: list[str],
    agent_id: str | None = None,
) -> str:
    created = client.post(
        "/api-clients",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"name": name, "scopes": ["*"]},
    )
    assert created.status_code == 201, created.text
    credential = client.post(
        f"/api-clients/{created.json()['id']}/credentials",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"name": f"{name} runtime", "scopes": scopes, "agent_id": agent_id},
    )
    assert credential.status_code == 201, credential.text
    return credential.json()["api_key"]


def test_chat_attachment_limit_is_configurable(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_ATTACHMENT_MAX_BYTES", "3")
    get_settings.cache_clear()
    try:
        assert get_settings().chat_attachment_max_bytes == 3
    finally:
        get_settings.cache_clear()

    monkeypatch.setattr(
        chat_api,
        "get_settings",
        lambda: SimpleNamespace(chat_attachment_max_bytes=3),
    )
    request = chat_api.ChatTurnRequest(
        tenant_id="tenant_api",
        user_id="user_api_admin",
        message="上传文件",
        attachments=[
            ChatAttachmentRead(
                id="file-1",
                filename="a.txt",
                content_type="text/plain",
                size=4,
                kind="text",
            )
        ],
    )
    with pytest.raises(HTTPException) as error:
        chat_api._validate_chat_turn_attachments(request)
    assert error.value.status_code == 400


def test_public_attachment_upload_stages_original_bytes_and_returns_descriptors(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    client, _engine, admin_token = _client(monkeypatch)
    token = _tenant_key(client, admin_token, ["runs:create", "runs:read"])
    files = [
        ("files", ("requirements.txt", b"first requirement\n", "text/plain")),
        ("files", ("requirements.pdf", b"%PDF-1.7\n", "application/pdf")),
    ]

    response = _upload(client, token, files)

    assert response.status_code == 200, response.text
    attachments = [ChatAttachmentRead.model_validate(item) for item in response.json()]
    assert [item.filename for item in attachments] == ["requirements.txt", "requirements.pdf"]
    assert all(item.sha256 for item in attachments)
    assert all(item.sandbox_path for item in attachments)
    assert read_staged_chat_attachment(
        attachments[0],
        tenant_id="tenant_api",
        user_id="user_api_admin",
    ) == files[0][1][1]
    assert attachments[0].sha256 == hashlib.sha256(files[0][1][1]).hexdigest()


def test_public_attachment_upload_enforces_scope_and_configured_size(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    client, _engine, admin_token = _client(monkeypatch)
    unauthenticated = client.post(
        "/agents/agent_api/attachments",
    )
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["code"] == "NOT_AUTHENTICATED"

    read_only = _create_key(client, admin_token, "read-only", ["runs:read"])

    forbidden = _upload(client, read_only, [("files", ("a.txt", b"a", "text/plain"))])
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "INSUFFICIENT_SCOPE"

    token = _create_key(client, admin_token, "upload", ["runs:create"])
    empty = client.post(
        "/agents/agent_api/attachments",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert empty.status_code == 400
    assert empty.json()["code"] == "NO_FILES_UPLOADED"

    too_many = _upload(
        client,
        token,
        [
            ("files", (f"file-{index}.txt", b"a", "text/plain"))
            for index in range(9)
        ],
    )
    assert too_many.status_code == 400
    assert too_many.json()["code"] == "TOO_MANY_ATTACHMENTS"

    agent_token = _create_key(
        client,
        admin_token,
        "agent-bound",
        ["runs:create"],
        agent_id="agent_api",
    )
    wrong_agent = _upload(
        client,
        agent_token,
        [("files", ("a.txt", b"a", "text/plain"))],
        agent_id="agent_other",
    )
    assert wrong_agent.status_code == 403
    assert wrong_agent.json()["code"] == "AGENT_SCOPE_MISMATCH"

    monkeypatch.setattr(
        public_attachments,
        "get_settings",
        lambda: SimpleNamespace(chat_attachment_max_bytes=3),
    )
    too_large = _upload(client, token, [("files", ("a.txt", b"abcd", "text/plain"))])
    assert too_large.status_code == 413
    assert too_large.json()["code"] == "ATTACHMENT_TOO_LARGE"


@pytest.mark.parametrize("field", ["id", "filename", "size", "sandbox_path", "sha256"])
def test_run_rejects_missing_or_tampered_staged_attachment_before_job_creation(
    monkeypatch,
    tmp_path: Path,
    field: str,
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    client, engine, admin_token = _client(monkeypatch)
    token = _tenant_key(client, admin_token, ["runs:create", "runs:read"])
    uploaded = _upload(client, token, [("files", ("urs.txt", b"URS", "text/plain"))])
    assert uploaded.status_code == 200, uploaded.text
    descriptor = uploaded.json()[0]
    if field == "id":
        descriptor["id"] = "missing-file"
    elif field == "filename":
        descriptor["filename"] = "tampered.txt"
    elif field == "size":
        descriptor["size"] += 1
    elif field == "sandbox_path":
        descriptor["sandbox_path"] = "/workspace/attachments/tampered"
    else:
        descriptor["sha256"] = "0" * 64

    response = client.post(
        "/agents/agent_api/runs",
        headers={"Authorization": f"Bearer {token}"},
        json={"input": "请分析附件", "session_mode": "stateless", "attachments": [descriptor]},
    )

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "INVALID_ATTACHMENT"
    with Session(engine) as db:
        assert db.exec(select(APIJob)).all() == []


def test_run_and_streaming_run_accept_uploaded_attachment_descriptors(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    client, engine, admin_token = _client(monkeypatch)
    token = _tenant_key(client, admin_token, ["runs:create", "runs:read"])
    uploaded = _upload(client, token, [("files", ("urs.txt", b"URS", "text/plain"))])
    assert uploaded.status_code == 200, uploaded.text
    descriptor = uploaded.json()[0]

    stream = StreamingResponse(
        iter(["event: complete\ndata: {}\n\n"]),
        media_type="text/event-stream",
    )
    monkeypatch.setattr(public_runs, "stream_job_events", lambda *args, **kwargs: stream)
    first = client.post(
        "/agents/agent_api/runs",
        headers={"Authorization": f"Bearer {token}"},
        json={"input": "分析附件", "session_mode": "stateless", "attachments": [descriptor]},
    )
    second = client.post(
        "/agents/agent_api/runs:stream",
        headers={"Authorization": f"Bearer {token}"},
        json={"input": "继续分析附件", "session_mode": "stateless", "attachments": [descriptor]},
    )

    assert first.status_code == 202, first.text
    assert second.status_code == 200, second.text
    with Session(engine) as db:
        jobs = db.exec(select(APIJob).where(APIJob.kind == "run")).all()
        assert len(jobs) == 2
        assert all(
            job.request_json["attachments"][0]["sha256"] == descriptor["sha256"]
            for job in jobs
        )


def test_same_session_can_rematerialize_descriptor_but_does_not_auto_inherit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    client, engine, admin_token = _client(monkeypatch)
    token = _tenant_key(
        client,
        admin_token,
        ["runs:create", "runs:read", "sessions:read", "sessions:write"],
    )
    uploaded = _upload(client, token, [("files", ("urs.txt", b"URS", "text/plain"))])
    assert uploaded.status_code == 200, uploaded.text
    descriptor = ChatAttachmentRead.model_validate(uploaded.json()[0])
    session_response = client.post(
        "/agents/agent_api/sessions",
        headers={"Authorization": f"Bearer {token}"},
        json={"external_session_id": "follow-up"},
    )
    assert session_response.status_code == 201, session_response.text
    session_id = session_response.json()["id"]

    first = materialize_task_attachments(
        [descriptor],
        tenant_id="tenant_api",
        session_id=session_id,
        task_frame_id="frame-one",
        user_id="user_api_admin",
    )
    second = materialize_task_attachments(
        [descriptor],
        tenant_id="tenant_api",
        session_id=session_id,
        task_frame_id="frame-two",
        user_id="user_api_admin",
    )

    assert first[0]["materialized"] is True
    assert second[0]["materialized"] is True
    first_workspace = harness_task_workspace_path(
        tenant_id="tenant_api",
        session_id=session_id,
        task_frame_id="frame-one",
    )
    second_workspace = harness_task_workspace_path(
        tenant_id="tenant_api",
        session_id=session_id,
        task_frame_id="frame-two",
    )
    assert first_workspace != second_workspace
    assert (
        first_workspace / first[0]["workspace_relative_path"]
    ).read_bytes() == b"URS"
    assert (
        second_workspace / second[0]["workspace_relative_path"]
    ).read_bytes() == b"URS"

    follow_up = client.post(
        "/agents/agent_api/runs",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "input": "根据刚才的附件继续分析",
            "session_id": session_id,
            "attachments": [descriptor.model_dump(mode="json")],
        },
    )
    assert follow_up.status_code == 202, follow_up.text

    response = client.post(
        "/agents/agent_api/runs",
        headers={"Authorization": f"Bearer {token}"},
        json={"input": "不带附件的后续问题", "session_id": session_id},
    )
    assert response.status_code == 202, response.text
    with Session(engine) as db:
        follow_up_job = db.get(APIJob, follow_up.json()["id"])
        assert follow_up_job is not None
        assert follow_up_job.request_json["attachments"][0]["id"] == descriptor.id
        job = db.get(APIJob, response.json()["id"])
        assert job is not None
        assert job.request_json["attachments"] == []
