import base64
import time

from fastapi.testclient import TestClient
from knowledge_core.api import BaseCreate, Edit, QueryRequest, Upload, create_app
from knowledge_core.client import KnowledgeClient
from knowledge_core.responses import KnowledgeBase, KnowledgeChunk, KnowledgeDocument, KnowledgeIngestJob
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
