"""Content transactions; task progress is committed outside content writes."""
import base64
from sqlalchemy import select, delete, update
from fastapi import HTTPException
from . import storage as s
from .processing import Processing
from .algorithms import _build_section_nodes
from . import algorithms as rules


def serialize(row):
    return {key: value.isoformat() if hasattr(value, "isoformat") else value for key, value in row.items()}


class Content:
    def __init__(self, runtime):
        self.runtime = runtime

    def archive(self, document_id, context, actor, expected=None):
        row = self.get(s.documents, document_id, context, actor, "write")
        with self.runtime.engine.begin() as db:
            statement = update(s.documents).where(s.documents.c.id == row["id"])
            if expected is not None:
                if row['updated_at'].isoformat() != expected: raise HTTPException(409, "Concurrent delete")
                statement = statement.where(s.documents.c.updated_at == row['updated_at'])
            changed = db.execute(statement.values(status="archived", updated_at=s.now()))
            if changed.rowcount != 1: raise HTTPException(409, "Concurrent delete")
            for plugin in self.runtime.plugins.values(): plugin.content_changed(db, document_id)
        return {"status": "archived"}

    def citation(self, chunk_id, context, actor):
        return self.runtime.evidence.resolve(chunk_id, context, actor)

    def get(self, table, resource_id, context, actor, action="read"):
        self.runtime.authorize(actor, context, action)
        versions = self.runtime.plugins.get("versions")
        historical = versions.historical(table, context) if versions else None
        if historical is not None:
            row = next((row for row in historical if row["id"] == resource_id), None)
            if row is None: raise HTTPException(404, "Resource not found")
            return row
        with self.runtime.engine.connect() as db:
            row = db.execute(select(table).where(table.c.id == resource_id)).mappings().first()
        if row is None:
            raise HTTPException(404, "Resource not found")
        document_id = row["id"] if table is s.documents else row.get("document_id")
        self.runtime.require_content(document_id, context)
        return dict(row)

    def list(self, table, context, actor, document_id=None, bucket_id=None):
        self.runtime.authorize(actor, context, "read")
        versions = self.runtime.plugins.get("versions")
        historical = versions.historical(table, context) if versions else None
        if historical is not None:
            return [row for row in historical if (not document_id or row.get("document_id", row["id"]) == document_id)
                    and (not bucket_id or row.get("bucket_id") == bucket_id)]
        statement = select(table)
        if document_id:
            self.get(s.documents, document_id, context, actor)
            statement = statement.where(table.c.document_id == document_id)
        if bucket_id:
            self.get(s.buckets, bucket_id, context, actor)
            statement = statement.where(table.c.bucket_id == bucket_id)
        with self.runtime.engine.connect() as db:
            rows = [dict(row) for row in db.execute(statement.order_by(table.c.created_at, table.c.id)).mappings()]
        return [row for row in rows if self.runtime.visible(row["id"] if table is s.documents else row.get("document_id"), context)]

    def write(self, document_id, filename, text, title, metadata, context, actor, model_id=None, expected=None, job_id=None, file_type=None):
        self.runtime.authorize(actor, context, "write")
        model, config, _ = self.runtime.ports(context, model_id)
        text, title, card, specs, trace = Processing(model, config).process(filename, text, title)
        if not text.strip(): raise HTTPException(422, "Document is empty")
        prepared = {name: plugin.prepare(filename, text, specs, context, model, config) for name, plugin in self.runtime.plugins.items()}
        moment = s.now()
        if expected is not None:
            metadata = {key: value for key, value in metadata.items() if key != "revision_source_updated_at"}
        with self.runtime.engine.begin() as db:
            if job_id and context.get('ingest_version_id'):
                versions = self.runtime.plugins['versions']
                branch = versions.branch(db, context)
                # Claim the captured head in the content transaction so version
                # changes cannot redirect an in-flight ingestion.
                claimed = db.execute(update(versions.branches).where(
                    versions.branches.c.id == branch['id'],
                    versions.branches.c.head_version_id == context['ingest_version_id'],
                ).values(head_version_id=context['ingest_version_id']))
                if claimed.rowcount != 1:
                    raise HTTPException(409, 'Knowledge branch changed during ingestion; retry the upload')
            if expected is not None:
                before = db.execute(select(s.documents).where(s.documents.c.id == document_id)).mappings().first()
                if not before or before["updated_at"].isoformat() != expected:
                    raise HTTPException(409, "Concurrent edit")
                for table in (s.chunks, s.buckets):
                    db.execute(delete(table).where(table.c.document_id == document_id))
            bucket_rows, chunk_rows = Processing().layers(document_id, filename, text, card, specs, moment)
            for bucket in bucket_rows: db.execute(s.buckets.insert().values(**bucket))
            for row in chunk_rows:
                db.execute(s.chunks.insert().values(**row))
                db.execute(s.citations.insert().values(id=row["id"], document_id=document_id,
                    payload={**serialize(row), "recovery_contexts": [dict(context)]}))
            sections = _build_section_nodes(text)
            values = {"filename": filename, "file_type": file_type or filename.rsplit(".", 1)[-1], "title": title,
                      "content": text, "status": "ready", "metadata": {**metadata, "raw_text": text, "document_card": card,
                          "section_tree": sections, "route_trace": trace,
                          "ingest_schema_version": rules.KNOWLEDGE_INGEST_SCHEMA_VERSION, "char_count": len(text),
                          "section_stats": {"section_count": len(sections), "paragraph_count": len(rules._paragraph_blocks(text))},
                          "chunk_stats": {"total_chunks": len(chunk_rows), "chunk_count": len(chunk_rows),
                              "target_chars": rules.EVIDENCE_CHUNK_CHARS, "section_target_chars": rules.SECTION_TARGET_CHARS},
                          "bucket_quality": [{"bucket_id": row['id'], "title": row['title'], "quality": row['metadata']['quality']} for row in bucket_rows]},
                      "bucket_count": len(bucket_rows), "chunk_count": len(chunk_rows), "updated_at": moment}
            if expected is None:
                db.execute(s.documents.insert().values(id=document_id, created_at=moment, **values))
            else:
                db.execute(update(s.documents).where(s.documents.c.id == document_id).values(**values))
            self.runtime.assign_content(db, document_id, context)
            for name, plugin in self.runtime.plugins.items():
                plugin.process(db, document_id, text, bucket_rows, chunk_rows, context, prepared[name])
                plugin.content_changed(db, document_id)
            concepts = self.runtime.plugins.get('concepts')
            if concepts:
                rows = db.execute(select(concepts.concepts).where(concepts.concepts.c.document_id == document_id)).mappings().all()
                values['metadata']['okf'] = {'version': '0.1', 'concept_count': len(rows),
                    'concept_types': sorted({row['concept_type'] for row in rows})}
                db.execute(update(s.documents).where(s.documents.c.id == document_id).values(metadata=values['metadata']))
            if job_id:
                job = db.execute(select(s.jobs).where(s.jobs.c.id == job_id)).mappings().one()
                if job["status"] in {"cancel_requested", "cancelled"}:
                    raise Cancelled()
                count = len(rows) if concepts else 0
                context = {**job['context'], 'stage_detail': f'完成入库：{count} 个 Wiki 页面，{len(bucket_rows)} 个内部索引，{len(chunk_rows)} 个引用来源',
                    'stage_stats': {'concept_count': count, 'bucket_count': len(bucket_rows), 'chunk_count': len(chunk_rows)}}
                db.execute(update(s.jobs).where(s.jobs.c.id == job_id).values(status="succeeded", stage="done", progress=1,
                    document_id=document_id, payload=None, context=context, finished_at=moment, updated_at=moment))
        return self.get(s.documents, document_id, context, actor)

    def edit(self, table, resource_id, changes, context, actor):
        row = self.get(table, resource_id, context, actor, "write")
        expected = changes.pop("expected_updated_at")
        if table is s.documents and changes.get("content_md") is not None:
            return self.write(resource_id, row["filename"], changes["content_md"], changes.get("title") or row["title"],
                              changes.get("metadata") if changes.get('metadata') is not None else row["metadata"], context, actor, expected=expected, file_type=row["file_type"])
        values = {key: value for key, value in changes.items() if value is not None}
        if "content_md" in values: values["content"] = values.pop("content_md")
        if table is s.buckets and "content" in values:
            values["metadata"] = {**values.get("metadata", row["metadata"]), "content": values["content"]}
        if "metadata" in row and "revision_source_updated_at" in row["metadata"]:
            values["metadata"] = {key: value for key, value in values.get("metadata", row["metadata"]).items() if key != "revision_source_updated_at"}
        values["updated_at"] = s.now()
        with self.runtime.engine.begin() as db:
            changed = db.execute(update(table).where(table.c.id == resource_id, table.c.updated_at == row["updated_at"])
                                 .values(**values)) if row["updated_at"].isoformat() == expected else None
            if changed is None or changed.rowcount != 1: raise HTTPException(409, "Concurrent edit")
            if table in (s.buckets, s.chunks):
                document = db.execute(select(s.documents).where(s.documents.c.id == row["document_id"])).mappings().one()
                metadata = {key: value for key, value in document["metadata"].items() if key != "revision_source_updated_at"}
                db.execute(update(s.documents).where(s.documents.c.id == row["document_id"]).values(metadata=metadata, updated_at=values["updated_at"]))
            if table is s.chunks:
                current = {**row, **values}
                citation_id = resource_id + ":" + values["updated_at"].isoformat()
                db.execute(s.citations.insert().values(id=citation_id, document_id=row["document_id"],
                    payload={**serialize(current), "recovery_contexts": [dict(context)]}))
            for plugin in self.runtime.plugins.values(): plugin.content_changed(db, resource_id if table is s.documents else row["document_id"])
        return self.get(table, resource_id, context, actor)

    def query(self, value, context, actor):
        return self.runtime.retrieval.query(value, context, actor)


class Cancelled(Exception):
    pass


class Tasks:
    def __init__(self, runtime): self.runtime = runtime

    def progress(self, job_id, **values):
        with self.runtime.engine.begin() as db:
            values["updated_at"] = s.now()
            row = db.execute(select(s.jobs).where(s.jobs.c.id == job_id)).mappings().one()
            context = dict(row['context'])
            if values.get('status') == 'running' and not context.get('started_at'):
                context['started_at'] = s.now().isoformat()
            if values.get('error'): context['stage_detail'] = values['error']
            values['context'] = context
            db.execute(update(s.jobs).where(s.jobs.c.id == job_id, s.jobs.c.status.not_in(["succeeded", "cancelled", "failed"])).values(**values))

    def cancel(self, job_id):
        with self.runtime.engine.begin() as db:
            row = db.execute(select(s.jobs).where(s.jobs.c.id == job_id)).mappings().one()
            db.execute(update(s.jobs).where(s.jobs.c.id == job_id, s.jobs.c.status.in_(["queued", "running"])).values(
                status="cancelled", stage="cancelled", payload=None, finished_at=s.now(), updated_at=s.now(),
                context={**row['context'], 'stage_detail': '入库任务已取消', 'cancelled_at': s.now().isoformat()}))

    def create(self, payload, context, actor):
        self.runtime.authorize(actor, context, "write")
        base64.b64decode(payload["content_base64"], validate=True)
        moment = s.now()
        row = {"id": s.identifier("kjob"), "filename": payload["filename"], "status": "queued", "stage": "queued",
               "progress": 0, "error": None, "document_id": None, "payload": payload,
               "context": {**context, 'stage_detail': '等待后台处理'}, "principal": {"id": actor.id, "tenant_id": actor.tenant_id, "permissions": list(actor.permissions), "spaces": list(actor.spaces)},
               "created_at": moment, "updated_at": moment, "finished_at": None}
        with self.runtime.engine.begin() as db: db.execute(s.jobs.insert().values(**row))
        self.runtime.executor.submit(self.run, row["id"], actor)
        return row

    def run(self, job_id, actor):
        with self.runtime.engine.connect() as db:
            row = dict(db.execute(select(s.jobs).where(s.jobs.c.id == job_id)).mappings().one())
        if row["status"] == "cancelled": return
        try:
            self.runtime.authorize(actor, row["context"], "write")
            self.progress(job_id, status="running", stage="parsing", progress="0.08")
            payload = row["payload"]
            _, _, parser = self.runtime.ports(row["context"], payload.get("model_id"))
            parse = getattr(parser, 'parse_document', None)
            content = base64.b64decode(payload["content_base64"], validate=True)
            capability_context = payload.get('metadata', {}).get('capability_context', {})
            text, file_type = parse(payload['filename'], content, capability_context) if parse else parser.parse(payload['filename'], content)
            self.progress(job_id, stage="processing", progress="0.4")
            document = self.runtime.content.write(s.identifier("kdoc"), payload["filename"], text,
                payload.get("title"), payload.get("metadata", {}), row["context"], actor, payload.get("model_id"), job_id=job_id, file_type=file_type)
        except Cancelled:
            self.progress(job_id, status="cancelled", stage="cancelled", payload=None, finished_at=s.now())
        except Exception as error:
            self.progress(job_id, status="failed", stage="failed", error=str(error), payload=None, finished_at=s.now())

    def recover(self):
        with self.runtime.engine.begin() as db:
            db.execute(update(s.jobs).where(s.jobs.c.status.in_(["queued", "running", "cancel_requested"])).values(
                status="failed", stage="interrupted", error="PROCESS_RESTART_INTERRUPTED", payload=None, finished_at=s.now(), updated_at=s.now()))
