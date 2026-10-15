import base64
import time

from fastapi.testclient import TestClient
from knowledge_core.api import create_app
from support import full_runtime as Runtime
from sqlalchemy import inspect


def wait_job(client, job_id):
    for _ in range(100):
        response = client.get(f"/api/km/v1/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.02)
    raise AssertionError("Ingest did not finish")


def test_fresh_database_without_actor_complete_workflow(tmp_path):
    runtime = Runtime(f"sqlite:///{tmp_path / 'knowledge.sqlite'}")
    with TestClient(create_app(runtime)) as client:
        assert client.get("/health/ready").status_code == 200
        tables = set(inspect(runtime.engine).get_table_names())
        expected = set(__import__('knowledge_core.kernel.storage', fromlist=['metadata']).metadata.tables)
        expected.update(name for plugin in runtime.plugins.values() for name in plugin.metadata.tables)
        assert tables == expected
        assert not any(name.startswith('knowledge_') for name in tables)
        response = client.post("/api/km/v1/knowledge-bases", json={"name": "Expense policy"})
        assert response.status_code == 201, response.text
        kb_id = response.json()["id"]
        assert client.post("/api/km/v1/knowledge-bases", json={"name": "Expense policy"}).status_code == 409
        upload = client.post(
            f"/api/km/v1/knowledge-bases/{kb_id}/documents",
            json={
                "filename": "expense.md",
                "content_base64": base64.b64encode(
                    b"# Expense reimbursement\n\nExpense reimbursement requires receipts. Submit within 30 days."
                ).decode(),
            },
        )
        assert upload.status_code == 202, upload.text
        job = wait_job(client, upload.json()["id"])
        assert job["status"] == "succeeded", job
        assert "content_base64" not in job["metadata"]
        document_id = job["document_id"]
        document = client.get(f"/api/km/v1/documents/{document_id}").json()
        assert document["chunk_count"] > 0
        concepts = client.get(f"/api/km/v1/documents/{document_id}/concepts").json()
        assert concepts
        concept_path = f"/api/km/v1/knowledge-bases/{kb_id}/concepts/{concepts[0]['concept_id']}"
        assert client.get(concept_path).json() == concepts[0]
        assert client.get(concept_path, params={"space_id": "other"}).status_code == 404
        assert client.get(f"/api/km/v1/knowledge-bases/{kb_id}/concepts/missing").status_code == 404
        assert client.put(concept_path, json={"content_md": " "}).status_code == 422
        assert client.put(concept_path, json={"content_md": concepts[0]["content_md"]}).status_code == 422
        saved = client.put(concept_path, json={"content_md": concepts[0]["content_md"], "expected_updated_at": concepts[0]["updated_at"]})
        assert saved.status_code == 200, saved.text
        assert client.put(concept_path, json={"content_md": "stale", "expected_updated_at": concepts[0]["updated_at"]}).status_code == 409
        assert saved.json()["document_id"] == concepts[0]["document_id"]
        assert saved.json()["source_refs"] == concepts[0]["source_refs"]
        concepts = client.get(f"/api/km/v1/documents/{document_id}/concepts").json()
        edited_concept = client.put(f"/api/km/v1/concepts/{concepts[0]['id']}", json={
            "expected_updated_at": concepts[0]["updated_at"],
            "content_md": "# Expense policy\n\nKeep receipts.",
        })
        assert edited_concept.status_code == 200, edited_concept.text
        assert edited_concept.json()["content_md"]
        document = client.get(f"/api/km/v1/documents/{document_id}").json()
        result = client.post(
            "/api/km/v1/query", json={"query": "Expense reimbursement receipts"}
        ).json()
        assert result["chunks"], result
        filtered = client.post("/api/km/v1/query", json={"query": "Expense receipts", "version_ids": ["missing"]}).json()
        assert not filtered["chunks"] and not filtered["evidence_pack"]
        chunk_id = result["chunks"][0]["id"]
        assert client.get(f"/api/km/v1/citations/{chunk_id}").status_code == 200
        updated = client.put(
            f"/api/km/v1/documents/{document_id}",
            json={
                "expected_updated_at": document["updated_at"],
                "content_md": "# Expense reimbursement\n\nExpense reimbursement requires original receipts.",
            },
        )
        assert updated.status_code == 200, updated.text
        conflict = client.put(
            f"/api/km/v1/documents/{document_id}",
            json={
                "expected_updated_at": document["updated_at"],
                "content_md": "Stale",
            },
        )
        assert conflict.status_code == 409
        exported = client.get(f"/api/km/v1/knowledge-bases/{kb_id}/okf")
        assert exported.status_code == 200, exported.text
        assert exported.content.startswith(b"PK")
        invalid_okf = client.post(
            f"/api/km/v1/knowledge-bases/{kb_id}/okf",
            json={
                "filename": "invalid.okf.zip",
                "content_base64": base64.b64encode(b"not a zip archive").decode(),
            },
        )
        assert invalid_okf.status_code == 422, invalid_okf.text
        copied = client.post(f"/api/km/v1/knowledge-bases/{kb_id}/versions/copy", json={})
        assert copied.status_code == 200, copied.text
        assert client.get(f"/api/km/v1/citations/{chunk_id}").status_code == 200
