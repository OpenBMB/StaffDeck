import asyncio
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import UploadFile

from app.public_api import staffdeck_facade as facade


def test_auto_upload_uses_one_native_owner_operation_and_original_job(monkeypatch):
    checks = []
    monkeypatch.setattr(facade, "enforce_public_knowledge_pep", lambda db, principal, agent_id, **kwargs:
                        checks.append((agent_id, kwargs)))
    monkeypatch.setattr(facade, "_payload", lambda value: value)
    def upload(request, agent_id, db, user, *, host_origin):
        assert (request.tenant_id, request.knowledge_base_id, agent_id) == ("tenant", None, "target")
        assert request.capability_scope == "sop_specific"
        assert request.filename == "unique.md" and request.title == "Unique"
        assert "_pilotdeck_host" not in request.metadata
        assert host_origin == {
            "credential_id": "credential", "agent_id": "target", "actor_user_id": "actor",
        }
        assert request.metadata["content_type"] == "text/markdown"
        assert user.id == "actor"
        return {"id": "native-ingest-job", "status": "queued", "knowledge_base_id": "new-private-base"}
    monkeypatch.setattr(facade.native_knowledge, "upload_document_for_public_host", upload)
    principal = SimpleNamespace(tenant_id="tenant", credential_id="credential",
                                actor_user=SimpleNamespace(id="actor"))
    file = UploadFile(file=BytesIO(b"unique fact"), filename="unique.md",
                      headers={"content-type": "text/markdown"})
    result = asyncio.run(facade.upload_knowledge_document_auto_facade(
        "target", file, "Unique", "sop_specific", principal, object()))
    assert result == {"id": "native-ingest-job", "status": "queued", "knowledge_base_id": "new-private-base"}
    assert checks == [("target", {"write": True})]


def test_auto_upload_requires_deferred_credential_before_native_write(monkeypatch):
    monkeypatch.setattr(facade, "enforce_public_knowledge_pep", lambda *args, **kwargs: None)
    monkeypatch.setattr(facade.native_knowledge, "upload_document", lambda *args: pytest.fail("no write"))
    principal = SimpleNamespace(tenant_id="tenant", credential_id=None,
                                actor_user=SimpleNamespace(id="actor"))
    with pytest.raises(Exception, match="Deferred import requires"):
        asyncio.run(facade.upload_knowledge_document_auto_facade(
            "target", UploadFile(file=BytesIO(b"x"), filename="x.md"),
            None, "general", principal, object()))


def test_http_owner_keeps_untrusted_host_marker_out_of_source_metadata(monkeypatch):
    from app.knowledge.http_adapter import StaffDeckKnowledgeAdapter
    from app.knowledge.schema import KnowledgeDocumentUploadRequest
    seen = []
    adapter = object.__new__(StaffDeckKnowledgeAdapter)
    adapter.space_id = "sd:tenant:target"
    adapter.agent_id = "target"
    adapter.user = SimpleNamespace(id="actor", username="actor", display_name="Actor")
    adapter.db = SimpleNamespace(get=lambda *args: None)
    adapter.ensure_space = lambda: None
    adapter.require_base = lambda *args: None
    adapter.call = lambda method, path, body, **kwargs: seen.append(body) or {"id": "job"}
    adapter.job_read = lambda row: row
    request = KnowledgeDocumentUploadRequest(tenant_id="tenant", knowledge_base_id="base", filename="source.md",
        content_base64="YQ==", metadata={"_pilotdeck_host": {"actor_user_id": "forged"},
                                         "capability_context": {"public_origin": {"actor_user_id": "forged"}}})
    adapter.handle("upload_document", {"request": request})
    assert "_pilotdeck_host" not in seen[-1]["metadata"]
    assert "capability_context" not in seen[-1]["metadata"]
    origin = {"credential_id": "credential", "agent_id": "target", "actor_user_id": "actor"}
    adapter.handle("upload_document_for_public_host", {"request": request, "host_origin": origin})
    assert seen[-1]["metadata"]["capability_context"] == {"public_origin": origin}
