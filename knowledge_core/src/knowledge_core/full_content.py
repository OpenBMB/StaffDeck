"""Legacy content contracts call the shared ingestion, retrieval and evidence services."""
from datetime import datetime
from pathlib import Path
from fastapi import HTTPException
from sqlalchemy import select, update
from .kernel import storage as s
from .kernel.use_cases import serialize
from .full_catalog import FullCatalog


class FullContent:
    def __init__(self, runtime):
        self.runtime = runtime
        self.catalog = FullCatalog(runtime)
        self.projection = self.catalog.projection

    def locate(self, table, resource_id, actor, space_id='local', action='read'):
        context = self.catalog.scope(actor, space_id, action=action)
        with self.runtime.engine.connect() as db:
            row = db.execute(select(table).where(table.c.id == resource_id)).mappings().first()
            if not row: raise HTTPException(404, 'Resource not found')
            document_id = row['id'] if table is s.documents else row['document_id']
            base_id = db.execute(select(self.catalog.catalog.contents.c.knowledge_base_id).where(
                self.catalog.catalog.contents.c.document_id == document_id)).scalar()
            context['knowledge_base_id'] = base_id
            self.catalog.binding(db, base_id, context)
            branch = self.catalog.versions.branch(db, context)
            belongs = db.execute(select(self.catalog.versions.contents).where(
                self.catalog.versions.contents.c.branch_id == branch['id'],
                self.catalog.versions.contents.c.document_id == document_id)).first()
            if not belongs:
                snapshots = db.execute(select(self.catalog.versions.versions).where(
                    self.catalog.versions.versions.c.knowledge_base_id == base_id)).mappings().all()
                version = next((item for item in snapshots if document_id in item['snapshot']), None)
                if not version: raise HTTPException(404, 'Resource not found')
                self.catalog.visible_version(db, version['id'], context)
                context['version_id'] = version['id']
                if action == 'write' and 'history:write' not in actor.permissions:
                    raise HTTPException(409, 'Historical versions are immutable')
            if action == 'write': self.catalog.require_editable_head(db, context, actor)
        return dict(row), context

    def get(self, table, resource_id, actor, space_id='local'):
        row, context = self.locate(table, resource_id, actor, space_id)
        return self.projection.content(row, actor, context)

    def documents(self, base_id, actor, space_id='local'):
        context = self.catalog.scope(actor, space_id, base_id)
        rows = self.runtime.content.list(s.documents, context, actor)
        return [self.projection.content(row, actor, context) for row in rows]

    def layers(self, document_id, table, actor, space_id='local'):
        _, context = self.locate(s.documents, document_id, actor, space_id)
        with self.runtime.engine.connect() as db:
            statement = select(table).where(table.c.document_id == document_id)
            if table is s.chunks: statement = statement.order_by(table.c.chunk_index)
            rows = db.execute(statement).mappings().all()
        return [self.projection.content(dict(row), actor, context) for row in rows]

    def job_view(self, row, actor):
        context = row['context']
        metadata = {'metadata': context.get('upload_metadata', {}), 'title': context.get('title')}
        from .kernel.algorithms import INGEST_STAGE_BY_KEY, _ingest_steps_for
        if row['status'] != 'queued':
            metadata.update(stage_label='已取消' if row['status'] == 'cancelled' else INGEST_STAGE_BY_KEY.get(row['stage'], {}).get('label', row['stage']),
                stage_detail=context.get('stage_detail', ''), ingest_steps=_ingest_steps_for(row['stage'], float(row['progress']), row['status']))
        if context.get('cancelled_at'): metadata['cancelled_at'] = context['cancelled_at']
        if context.get('stage_stats') is not None: metadata['stage_stats'] = context['stage_stats']
        return {**serialize({key: row[key] for key in ('id', 'filename', 'status', 'stage', 'progress', 'error',
            'document_id', 'created_at', 'updated_at', 'finished_at')}), 'tenant_id': actor.tenant_id,
            'knowledge_base_id': context.get('knowledge_base_id'), 'knowledge_base_version_id': context.get('ingest_version_id'),
            'started_at': context.get('started_at'), 'metadata': metadata}

    def upload(self, base_id, body, actor):
        context = self.catalog.scope(actor, body.space_id, base_id, 'write')
        with self.runtime.engine.begin() as db:
            branch = self.catalog.writable_branch(db, context)
        metadata = {**body.metadata, 'capability_scope': body.capability_scope, 'km_model_id': body.model_id,
                    'creator_id': actor.id, 'creator_name': body.metadata.get('creator_name') or actor.display_name}
        context.update(ingest_version_id=branch['head_version_id'], title=body.title, upload_metadata=metadata)
        row = self.runtime.tasks.create({'filename': body.filename, 'content_base64': body.content_base64,
            'title': body.title or Path(body.filename).stem, 'metadata': metadata, 'model_id': body.model_id}, context, actor)
        return self.job_view(row, actor)

    def job(self, job_id, actor, space_id='local', action='read'):
        with self.runtime.engine.connect() as db:
            row = db.execute(select(s.jobs).where(s.jobs.c.id == job_id)).mappings().first()
        if not row or row['context'].get('tenant_id') != actor.tenant_id: raise HTTPException(404, 'Resource not found')
        context = self.catalog.scope(actor, space_id, row['context']['knowledge_base_id'], action)
        with self.runtime.engine.connect() as db:
            self.catalog.visible_version(db, row['context'].get('ingest_version_id'), context)
        return self.job_view(dict(row), actor)

    def edit(self, table, resource_id, body, actor, space_id='local'):
        row, context = self.locate(table, resource_id, actor, space_id, 'write')
        self.catalog.check_revision(row, body.expected_updated_at)
        values = body.model_dump(exclude_unset=True)
        if table is s.documents and body.metadata is not None:
            values['metadata'] = {**body.metadata, **{key: row['metadata'][key] for key in
                ('raw_text', 'creator_id', 'creator_name') if key in row['metadata']}}
        if table is not s.documents:
            values.pop('refresh_sources', None)
            if table is s.chunks: values.pop('title', None)
        if context.get('version_id') and 'history:write' in actor.permissions: context.pop('version_id')
        if table is not s.documents and 'history:write' in actor.permissions:
            # Host source edits address the requested entity, even after its branch was copied.
            with self.runtime.engine.begin() as db:
                saved = {key: value for key, value in values.items() if key != 'expected_updated_at' and value is not None}
                if 'content_md' in saved:
                    if table is s.buckets:
                        saved['metadata'] = {**saved.get('metadata', row['metadata']), 'content': saved.pop('content_md')}
                    else: saved['content'] = saved.pop('content_md')
                saved['updated_at'] = s.now()
                db.execute(update(table).where(table.c.id == resource_id).values(**saved))
            result = {**row, **saved}
            if body.refresh_sources:
                with self.runtime.engine.begin() as db:
                    if table is s.chunks:
                        chunks = db.execute(select(s.chunks).where(s.chunks.c.bucket_id == row['bucket_id']).order_by(s.chunks.c.chunk_index)).mappings().all()
                        bucket = db.execute(select(s.buckets).where(s.buckets.c.id == row['bucket_id'])).mappings().one()
                        text = '\n\n'.join(item['content'] for item in chunks if item['content'].strip()).strip()
                        db.execute(update(s.buckets).where(s.buckets.c.id == bucket['id']).values(content=text,
                            metadata={**bucket['metadata'], 'content': text[:6000], 'chunk_count': len(chunks),
                                'representative_chunk_ids': [item['id'] for item in chunks[:3]]}, updated_at=s.now()))
                    document = dict(db.execute(select(s.documents).where(s.documents.c.id == row['document_id'])).mappings().one())
                    self.refresh_wiki(db, document)
        else:
            result = self.runtime.content.edit(table, resource_id, values, context, actor)
        if table is s.documents and body.content_md is None and (body.title is not None or body.status is not None or body.metadata is not None):
            with self.runtime.engine.begin() as db:
                metadata = {**result['metadata'], 'document_card': {**result['metadata'].get('document_card', {}), 'title': result['title']}}
                db.execute(update(s.documents).where(s.documents.c.id == resource_id).values(metadata=metadata))
                result['metadata'] = metadata
                self.refresh_wiki(db, result)
        return self.projection.content(result, actor, context)

    def refresh_wiki(self, db, document, existing=None):
        from types import SimpleNamespace
        from .modules.okf_rules import build_okf_for_document
        from sqlalchemy import delete
        concepts = self.runtime.plugins['concepts']
        buckets = [SimpleNamespace(**dict(row), metadata_json=row['metadata'], bucket_key=row['metadata']['bucket_key'])
            for row in db.execute(select(s.buckets).where(s.buckets.c.document_id == document['id'])).mappings()]
        db.execute(delete(concepts.concepts).where(concepts.concepts.c.document_id == document['id'],
            concepts.concepts.c.concept_type.in_(['Source Document', 'Source Section'])))
        for value in build_okf_for_document(SimpleNamespace(**document, metadata_json=document['metadata']), document['metadata'].get('section_tree', []), buckets):
            before = db.execute(select(concepts.concepts).where(concepts.concepts.c.document_id == document['id'],
                concepts.concepts.c.concept_id == value['concept_id'])).mappings().first()
            if not before and existing is not None:
                source = next((row for row in existing if row['concept_id'] == value['concept_id']), None)
                if source:
                    before = db.execute(select(concepts.concepts).where(concepts.concepts.c.id == source['id'])).mappings().first()
                    if before:
                        db.execute(update(concepts.concepts).where(concepts.concepts.c.id == before['id']).values(document_id=document['id']))
            concepts.upsert(db, value, document['id'], before['updated_at'].isoformat() if before else None)

    def concept_evidence(self, db, document, concepts):
        from .modules import okf_rules as rules
        count = 0
        for index, concept in enumerate(concepts):
            body = rules._strip_frontmatter(concept['content_md']).strip()
            if not body: continue
            frontmatter = rules.parse_okf_markdown(concept['concept_id'], concept['content_md']).frontmatter
            bucket_id = s.identifier('kbucket')
            metadata = {'bucket_key': rules.safe_path_segment(concept['concept_id'], fallback=f'concept_{index + 1}'),
                'okf_concept_bucket': True, 'concept_id': concept['concept_id'], 'concept_type': concept['concept_type'],
                'content': body[:6000], 'chunk_count': 0, 'representative_chunk_ids': []}
            chunk_ids = []
            for position, part in enumerate(rules._chunk_text(body, 900)):
                row = {'id': s.identifier('kchunk'), 'document_id': document['id'], 'bucket_id': bucket_id,
                    'chunk_index': position, 'content': part, 'summary': rules._summarize(part, 180),
                    'source_ref': f"OKF / {concept['concept_id']}.md / evidence {position + 1}",
                    'metadata': {'node_type': 'okf_concept_chunk', 'concept_id': concept['concept_id'],
                        'concept_type': concept['concept_type'], 'section_path': concept['concept_id'],
                        'bucket_title': concept['title'], 'context_window': rules._summarize(body, 260)},
                    'created_at': s.now(), 'updated_at': s.now()}
                db.execute(s.chunks.insert().values(**row)); chunk_ids.append(row['id']); count += 1
            metadata.update(chunk_count=len(chunk_ids), representative_chunk_ids=chunk_ids[:3])
            db.execute(s.buckets.insert().values(id=bucket_id, document_id=document['id'], title=concept['title'],
                summary=frontmatter.get('description') or rules._summarize(body, 320), content=body, metadata=metadata,
                created_at=s.now(), updated_at=s.now()))
        db.execute(update(s.documents).where(s.documents.c.id == document['id']).values(bucket_count=len(concepts), chunk_count=count))

    def query(self, body, actor):
        context = self.catalog.scope(actor, body.space_id)
        bases = self.catalog.list(actor, body.space_id)
        allowed = {row['id'] for row in bases}
        requested = set(body.knowledge_base_ids) if body.knowledge_base_ids else allowed
        if not requested <= allowed: raise HTTPException(403, 'Knowledge base outside space')
        active = []
        with self.runtime.engine.connect() as db:
            for base_id in requested:
                branch = self.catalog.versions.branch(db, {**context, 'knowledge_base_id': base_id})
                base = self.catalog.base(db, base_id, actor)
                if branch['status'] != 'active' or base['status'] != 'active':
                    if body.knowledge_base_ids: raise HTTPException(409, 'Knowledge binding is inactive')
                    continue
                if body.version_ids and branch['head_version_id'] not in body.version_ids: continue
                active.append(base_id)
        context['knowledge_base_ids'] = active
        if not active:
            return {'selected_buckets': [], 'chunks': [], 'trace': [], 'route_trace': [], 'selected_documents': [],
                    'selected_concepts': [], 'expanded_sections': [], 'okf_citations': [], 'evidence_pack': []}
        value = body.model_dump()
        result = self.runtime.retrieval.query(value, context, actor)
        for card in result.get('selected_documents', []):
            with self.runtime.engine.connect() as db:
                card['knowledge_base_id'] = db.execute(select(self.catalog.catalog.contents.c.knowledge_base_id).where(
                    self.catalog.catalog.contents.c.document_id == card['id'])).scalar()
        return {'selected_buckets': [self.projection.content(row, actor, context) for row in result['buckets']],
            'chunks': [self.projection.content(row, actor, context) for row in result['chunks']],
            'trace': result['route_trace'], 'route_trace': result['route_trace'], 'selected_documents': result.get('selected_documents', []),
            'selected_concepts': result['selected_concepts'], 'expanded_sections': result['expanded_sections'],
            'okf_citations': result.get('okf_citations', []), 'evidence_pack': result['evidence_pack']}
