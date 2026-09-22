"""Real storage/worker contracts; no deployed service or paid model calls."""
from __future__ import annotations

import pytest

from staffdeck import APIError


def test_knowledge_lifecycle(knowledge_worker, tmp_path):
    sdk_factory, jobs = knowledge_worker
    with sdk_factory() as sdk:
        kb = sdk.knowledge_bases.create("agent_api", {"name": "差旅制度"}).data
        kb_id = kb["id"]
        assert any(k["id"] == kb_id for k in sdk.knowledge_bases.list("agent_api").data["data"])
        changed = sdk.knowledge_bases.update("agent_api", kb_id, {"description": "SDK 管理"})
        assert changed.data["description"] == "SDK 管理"
        body = {"entries": [{"external_id": "policy-1", "title": "报销", "content": "# 报销\n差旅可报销。"}]}
        receipt = sdk.knowledge_bases.upsert_entries("agent_api", kb_id, body, idempotency_key="entry-1")
        job_id = receipt.data["id"]
        assert receipt.status_code == 202
        assert sdk.jobs.get(job_id).data["kind"] == "knowledge.ingest"
        assert sdk.knowledge_bases.upsert_entries(
            "agent_api", kb_id, body, idempotency_key="entry-1",
        ).data["id"] == job_id
        jobs.run_job(job_id)
        result = sdk.jobs.wait(job_id).data
        assert result["job"]["status"] == "succeeded", result
        document_id = result["result"]["documents"][0]["document_id"]
        documents = sdk.knowledge_bases.documents("agent_api", kb_id).data["data"]
        document = next(d for d in documents if d["id"] == document_id)
        assert document["status"] == "ready"
        edited = sdk.knowledge_bases.update_document("agent_api", kb_id, document_id, {
            "content_md": "# 报销制度\n出差交通费用可以报销。", "title": "新报销制度",
            "expected_updated_at": document["updated_at"],
        }).data
        assert edited["metadata"]["raw_text"] == "# 报销制度\n出差交通费用可以报销。"
        assert edited["title"] == "新报销制度"
        with pytest.raises(APIError) as stale:
            sdk.knowledge_bases.update_document("agent_api", kb_id, edited["id"], {
                "title": "stale", "expected_updated_at": document["updated_at"],
            })
        assert stale.value.status_code == 409
        search = sdk.knowledge_bases.search("agent_api", kb_id, {"query": "报销"}).data
        assert search["citations"]
        assert sdk.knowledge_bases.concepts("agent_api", kb_id).data["data"]

        source = tmp_path / "policy.md"
        source.write_text("# 休假制度\n每年有带薪年假。", encoding="utf-8")
        upload = sdk.knowledge_bases.upload_document("agent_api", kb_id, source, title="休假")
        jobs.run_job(upload.data["id"])
        uploaded = sdk.jobs.wait(upload.data["id"]).data["result"]["documents"][0]
        assert uploaded["document_id"]
        assert sdk.knowledge_bases.archive_document(
            "agent_api", kb_id, uploaded["document_id"],
        ).data["status"] == "archived"
        versions = sdk.knowledge_bases.versions("agent_api", kb_id).data["data"]
        assert versions
        assert sdk.knowledge_bases.rollback("agent_api", kb_id, versions[0]["version"]).status_code == 200
        assert sdk.knowledge_bases.archive("agent_api", kb_id).data["status"] == "archived"


def test_knowledge_document_path_and_employee_boundaries(knowledge_worker):
    sdk_factory, jobs = knowledge_worker
    with sdk_factory() as sdk:
        kb = sdk.knowledge_bases.create("agent_api", {"name": "private"}).data["id"]
        other = sdk.knowledge_bases.create("agent_api", {"name": "other"}).data["id"]
        receipt = sdk.knowledge_bases.upsert_entries("agent_api", kb, {
            "entries": [{"title": "private", "content": "private content"}],
        })
        jobs.run_job(receipt.data["id"])
        doc = sdk.jobs.wait(receipt.data["id"]).data["result"]["documents"][0]["document_id"]
        for agent_id, base_id in [("agent_api", other), ("agent_other", kb)]:
            with pytest.raises(APIError) as denied:
                sdk.knowledge_bases.update_document(agent_id, base_id, doc, {"title": "wrong"})
            assert denied.value.status_code == 404
        with pytest.raises(APIError) as denied:
            sdk.knowledge_bases.upsert_entries("agent_other", kb, {
                "entries": [{"title": "wrong", "content": "wrong"}],
            })
        assert denied.value.status_code == 404
        assert sdk.knowledge_bases.documents("agent_api", kb).data["data"][0]["title"] == "private"


def test_sop_generate_rewrite_versions_and_archive(api, skill_card, monkeypatch):
    from app.public_api import jobs, sops
    from app.skills.skill_schema import SkillCard, SkillDistillResponse, SkillRewriteResponse

    _, engine, _, sdk_factory = api
    monkeypatch.setattr(jobs, "engine", engine)
    monkeypatch.setattr(sops.internal_skills, "_get_request_model", lambda *a: None)
    monkeypatch.setattr(sops.SkillDistiller, "distill", lambda *a: SkillDistillResponse(
        draft_skill=SkillCard.model_validate(skill_card),
    ))

    def rewrite(self, request, model):
        content = request.current_skill.model_dump(mode="json")
        content["description"] = "修订后的制度"
        return SkillRewriteResponse(
            draft_skill=SkillCard.model_validate(content), assistant_message="已修订",
            changed_paths=["/description"],
        )

    monkeypatch.setattr(sops.SkillEditor, "rewrite", rewrite)
    with sdk_factory() as sdk:
        receipt = sdk.sops.generate("agent_api", {"title": "报销", "raw_content": "依据制度回答"},
                                    idempotency_key="generate-1")
        jobs.run_job(receipt.data["id"])
        draft = sdk.jobs.wait(receipt.data["id"]).data["result"]["draft"]
        sop_id, draft_id = draft["sop_id"], draft["id"]
        assert draft["status"] == "draft"
        assert not sdk.sops.list("agent_api").data["data"]
        assert sdk.sops.validate("agent_api", sop_id, draft_id).data["valid"]
        sdk.sops.publish("agent_api", sop_id, draft_id)
        old_version = draft["draft_version"]
        receipt = sdk.sops.rewrite("agent_api", sop_id, {"instruction": "修订制度"},
                                   idempotency_key="rewrite-1")
        jobs.run_job(receipt.data["id"])
        revised = sdk.jobs.wait(receipt.data["id"]).data["result"]["draft"]
        assert revised["status"] == "draft"
        assert sdk.sops.get_version("agent_api", sop_id, old_version).data["content"]["description"] \
            == skill_card["description"]
        sdk.sops.publish("agent_api", sop_id, revised["id"])
        changes = sdk.sops.diff(
            "agent_api", sop_id, revised["draft_version"], compare_to=old_version,
        ).data["changes"]
        assert any(c["path"] == "/description" for c in changes)
        restored = sdk.sops.rollback("agent_api", sop_id, old_version).data
        assert restored["status"] == "draft"
        assert sdk.sops.validate("agent_api", sop_id, restored["id"]).data["valid"]
        published = sdk.sops.publish("agent_api", sop_id, restored["id"])
        assert published.data["sop"]["content"]["description"] == skill_card["description"]
        assert sdk.sops.archive("agent_api", sop_id).data["status"] == "archived"


def test_member_key_can_edit_own_knowledge_but_cannot_read_other_employee(api, knowledge_worker):
    from app.api.auth import AccountAPICredentialCreateRequest, create_account_api_credential
    from app.db.models import AgentProfile, User
    from sqlmodel import Session

    _, engine, _, sdk_factory = api
    _, jobs = knowledge_worker
    with Session(engine) as db:
        member = User(id="member", tenant_id="tenant_api", username="member", role="member",
                      password_hash="x")
        db.add(member)
        agent = db.get(AgentProfile, "agent_api")
        agent.metadata_json = {"owner_user_id": member.id}
        db.add(agent)
        db.commit()
        key = create_account_api_credential(AccountAPICredentialCreateRequest(name="member"),
                                            member, db).api_key

    with sdk_factory() as admin:
        foreign_kb = admin.knowledge_bases.create("agent_other", {"name": "private"}).data["id"]
        foreign_job = admin.knowledge_bases.upsert_entries("agent_other", foreign_kb, {
            "entries": [{"title": "private", "content": "private"}],
        }).data["id"]
        jobs.run_job(foreign_job)
    with sdk_factory(key) as sdk:
        kb = sdk.knowledge_bases.create("agent_api", {"name": "member knowledge"}).data["id"]
        job = sdk.knowledge_bases.upsert_entries("agent_api", kb, {
            "entries": [{"title": "own", "content": "own content"}],
        }).data["id"]
        jobs.run_job(job)
        doc = sdk.jobs.wait(job).data["result"]["documents"][0]["document_id"]
        assert sdk.knowledge_bases.update_document(
            "agent_api", kb, doc, {"title": "member edit"},
        ).data["title"] == "member edit"
        operations = [
            lambda: sdk.knowledge_bases.documents("agent_other", foreign_kb),
            lambda: sdk.knowledge_bases.versions("agent_other", foreign_kb),
            lambda: sdk.knowledge_bases.concepts("agent_other", foreign_kb),
            lambda: sdk.sops.diff("agent_other", "unknown", "1.0.1", compare_to="1.0.0"),
            lambda: sdk.jobs.get(foreign_job),
            lambda: sdk.jobs.result(foreign_job),
            lambda: sdk.jobs.cancel(foreign_job),
        ]
        for operation in operations:
            with pytest.raises(APIError) as denied:
                operation()
            assert denied.value.status_code == 404
            assert denied.value.code == "AGENT_NOT_FOUND"


def test_runtime_key_cannot_manage_knowledge_or_sops(api, tmp_path):
    server, _, token, sdk_factory = api
    auth = {"Authorization": f"Bearer {token}"}
    client_id = server.get("/api-clients", headers=auth).json()[0]["id"]
    key = server.post(f"/api-clients/{client_id}/credentials", headers=auth, json={
        "name": "runtime", "agent_id": "agent_api", "scopes": ["agents:read", "runs:read"],
    }).json()["api_key"]
    file = tmp_path / "doc.md"
    file.write_text("sample")
    with sdk_factory(key) as sdk:
        for operation in [
            lambda: sdk.knowledge_bases.list("agent_api"),
            lambda: sdk.knowledge_bases.create("agent_api", {"name": "forbidden"}),
            lambda: sdk.knowledge_bases.upload_document("agent_api", "kb", file),
            lambda: sdk.sops.generate("agent_api", {"title": "forbidden", "raw_content": "text"}),
        ]:
            with pytest.raises(APIError) as denied:
                operation()
            assert denied.value.status_code == 403
