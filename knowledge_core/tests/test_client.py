import base64
import time

from fastapi.testclient import TestClient
from knowledge_core.api import BaseCreate, Edit, QueryRequest, Upload, create_app
from knowledge_core.client import KnowledgeClient
from knowledge_core.responses import KnowledgeBase, KnowledgeChunk, KnowledgeDocument, KnowledgeIngestJob, KnowledgeDiscoverySuggestion
from knowledge_core.runtime import Runtime
from knowledge_core.configuration import Configuration


def test_typed_client_roundtrips_serialized_metadata_and_async_jobs(tmp_path):
    runtime = Runtime(f"sqlite:///{tmp_path / 'client.sqlite'}", configuration=Configuration('full'))
    with TestClient(create_app(runtime)) as transport:
        client = KnowledgeClient("http://testserver")
        client.http.close()
        client.http = transport
        base = client.create_base(BaseCreate(name="Client policy"))
        assert isinstance(base, KnowledgeBase)
        assert base.metadata_json["owner_id"] == "local"
        job = client.upload(base.id, Upload(filename="policy.md", content_base64=base64.b64encode(
            b"# Expenses\n\nKeep expense receipts."
        ).decode()))
        assert isinstance(job, KnowledgeIngestJob)
        for _ in range(100):
            job = client.job(job.id)
            if job.status in {"succeeded", "failed"}:
                break
            time.sleep(.02)
        assert job.status == "succeeded"
        document = client.document(job.document_id)
        assert isinstance(document, KnowledgeDocument)
        chunks_before = client.layers(document.id, "chunks")
        tagged = client.edit(document.id, Edit(expected_updated_at=document.updated_at.isoformat(),
            title="Tagged policy", metadata={"label": "accounting"}, status="ready"))
        assert tagged.metadata_json["label"] == "accounting"
        assert tagged.metadata_json["raw_text"] == document.metadata_json["raw_text"]
        assert [row.id for row in client.layers(document.id, "chunks")] == [row.id for row in chunks_before]
        document = tagged
        edited = client.edit(document.id, Edit(expected_updated_at=document.updated_at.isoformat(),
            content_md="# Expenses\n\nKeep original expense receipts."))
        assert "original" in edited.metadata_json["raw_text"]
        result = client.query(QueryRequest(query="expense receipts"))
        citation = client.resolve_citation(result.chunks[0].id)
        assert isinstance(citation, KnowledgeChunk)
        assert citation.content


def test_typed_client_exports_and_rejects_documentless_lint_suggestion(tmp_path):
    runtime = Runtime(f"sqlite:///{tmp_path / 'suggestion.sqlite'}", configuration=Configuration('full'))
    with TestClient(create_app(runtime)) as transport:
        client = KnowledgeClient("http://testserver")
        client.http.close()
        client.http = transport
        base = client.create_base(BaseCreate(name="Lint"))
        response = transport.put(f'/api/km/v1/knowledge-bases/{base.id}/concepts/topics/orphan',
                                 json={'content_md': '# Orphan\n\n[[topics/missing]]', 'create_only': True})
        assert response.status_code == 200, response.text
        assert client.lint(base.id)['issue_count'] > 0
        suggestion = client.suggestions(base.id)[0]
        exported = client.suggestion(suggestion.id, 'export')
        assert isinstance(exported, KnowledgeDiscoverySuggestion)
        assert exported.knowledge_base_id == base.id
        assert client.suggestion(suggestion.id, 'reject') == {'status': 'rejected', 'published': False}
        assert client.suggestions(base.id)[0].status == 'rejected'
