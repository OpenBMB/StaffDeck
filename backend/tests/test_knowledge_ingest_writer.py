from __future__ import annotations

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from knowledge_core.models import KnowledgeModel, KnowledgeBucket, KnowledgeChunk, KnowledgeDocument, KnowledgeIngestJob
from knowledge_core.service import KnowledgeIngestCancelled, KnowledgeService


def test_chunk_build_allows_concurrent_management_and_durable_cancel(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'ingest.sqlite'}", connect_args={"timeout": 0})
    KnowledgeModel.metadata.create_all(engine)
    with Session(engine) as db:
        document = KnowledgeDocument(tenant_id="tenant", knowledge_base_id="kb", filename="long.md", file_type="md")
        bucket = KnowledgeBucket(tenant_id="tenant", knowledge_base_id="kb", document_id=document.id,
                                 bucket_key="section", title="Section", summary="Section",
                                 metadata_json={"section_ids": ["section"]})
        job = KnowledgeIngestJob(tenant_id="tenant", knowledge_base_id="kb", filename="long.md",
                                 document_id=document.id, status="running")
        db.add_all([document, bucket, job])
        db.commit()
        job_id, document_id, bucket_id = job.id, document.id, bucket.id
    refreshed = []
    with Session(engine) as worker:
        job = worker.get(KnowledgeIngestJob, job_id)
        document = worker.get(KnowledgeDocument, document_id)
        bucket = worker.get(KnowledgeBucket, bucket_id)

        def concurrent_management(connection, cursor, statement, parameters, context, many):
            if refreshed or not statement.lstrip().upper().startswith("SELECT"):
                return
            if not any(isinstance(row, KnowledgeChunk) for row in [*worker.new, *worker.identity_map.values()]):
                return
            refreshed.append(True)
            with Session(engine) as management:
                managed_job = management.get(KnowledgeIngestJob, job_id)
                KnowledgeService(management).cancel_ingest_job(job_id, "tenant")
                assert managed_job.status == "cancel_requested"

        event.listen(engine, "before_cursor_execute", concurrent_management)
        try:
            with pytest.raises(KnowledgeIngestCancelled):
                KnowledgeService(worker)._build_chunks("tenant", "kb", document, [bucket], [{
                    "section_id": "section", "path": "Section", "content": "First paragraph.\n\n" + "Evidence " * 1000,
                }], job)
            worker.rollback()
        finally:
            event.remove(engine, "before_cursor_execute", concurrent_management)
    with Session(engine) as db:
        assert refreshed == [True]
        assert db.get(KnowledgeIngestJob, job_id).status == "cancel_requested"
        assert list(db.exec(select(KnowledgeChunk)).all()) == []
