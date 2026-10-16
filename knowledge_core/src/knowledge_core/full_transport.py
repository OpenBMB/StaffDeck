"""Full transport delegates legacy use cases to the modular composition."""
import base64
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile, ZIP_DEFLATED
from fastapi import Depends, HTTPException, Response
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, update, delete
from .contracts import (BaseCreate, BaseEdit, BindingAction, BindingEdit, ConceptEdit, Edit,
                        LayerEdit, QueryRequest, SpaceCreate, Upload, VersionAction)
from .kernel import storage as s
from .kernel.use_cases import serialize
from .full_catalog import FullCatalog
from .full_content import FullContent


def register(app, runtime, principal):
    prefix = '/api/km/v1'
    catalog, content = FullCatalog(runtime), FullContent(runtime)
    spaces, concepts, discovery = (runtime.plugins[name] for name in ('spaces', 'concepts', 'discovery'))
    # Replace shared-path transports while keeping the unscoped core document API.
    replaced = {'/spaces', '/knowledge-bases', '/query', '/jobs', '/jobs/{job_id}', '/jobs/{job_id}/cancel',
                '/citations/{chunk_id}', '/documents/{document_id}', '/buckets/{bucket_id}/chunks',
                '/documents/{document_id}/buckets', '/documents/{document_id}/chunks'}
    app.router.routes = [route for route in app.router.routes if not (route.path.startswith(prefix) and
        (route.path[len(prefix):] in replaced or route.path.endswith('/{resource_id}')))]

    @app.get('/api/km/config')
    def configuration():
        from .capability_catalog import FULL, describe
        return {**runtime.configuration.as_dict(), 'api_protocol': 'knowledge-core/v1',
            'identity_required': runtime.configuration.enabled('authentication'),
            'model_mode': 'model' if runtime.model_config or runtime.capabilities else 'lexical',
            'operations': describe({'identity': ('GET', '/identity'), **{name: operation for name, operation in FULL.items()
                if name != 'publish_discovery' or runtime.publisher is not None}})}
    app.router.routes.insert(0, app.router.routes.pop())

    @app.get(prefix + '/spaces')
    def list_spaces(actor=Depends(principal)):
        runtime.authorize(actor, {}, 'read', require_scope=False)
        with runtime.engine.connect() as db:
            rows = db.execute(select(spaces.spaces).where(spaces.spaces.c.tenant_id == actor.tenant_id)).mappings().all()
        result = []
        for row in rows:
            if '*' not in actor.spaces and row['id'] not in actor.spaces: continue
            try: runtime.authorization.authorize(actor, row['id'], 'write'); writable = True
            except HTTPException as error:
                if error.status_code != 403: raise
                writable = False
            result.append({**serialize(dict(row)), 'can_write': writable})
        return result

    @app.post(prefix + '/spaces', status_code=201)
    def create_space(body: SpaceCreate, actor=Depends(principal)):
        runtime.authorization.authorize(actor, '*', 'write')
        row = {'id': body.id or s.identifier('space'), 'name': body.name, 'tenant_id': actor.tenant_id,
            'owner_id': actor.id, 'owner_name': actor.display_name, 'status': 'active', 'created_at': s.now(), 'updated_at': s.now()}
        with runtime.engine.begin() as db: db.execute(spaces.spaces.insert().values(**row))
        return jsonable_encoder(row)

    @app.get(prefix + '/knowledge-bases')
    def bases(space_id='local', actor=Depends(principal)): return catalog.list(actor, space_id)

    @app.post(prefix + '/knowledge-bases', status_code=201)
    def create_base(body: BaseCreate, actor=Depends(principal)): return catalog.create(body, actor)

    @app.get(prefix + '/knowledge-bases/{kb_id}')
    def get_base(kb_id: str, space_id='local', actor=Depends(principal)): return catalog.get(kb_id, actor, space_id)

    @app.put(prefix + '/knowledge-bases/{kb_id}')
    def edit_base(kb_id: str, body: BaseEdit, space_id='local', actor=Depends(principal)): return catalog.edit(kb_id, body, actor, space_id)

    @app.delete(prefix + '/knowledge-bases/{kb_id}')
    def remove(kb_id: str, space_id='local', permanent: bool=False, actor=Depends(principal)): return catalog.remove(kb_id, actor, space_id, permanent)

    @app.put(prefix + '/knowledge-bases/{kb_id}/branch')
    def edit_branch(kb_id: str, body: BaseEdit, space_id='local', actor=Depends(principal)): return catalog.edit_branch(kb_id, body, actor, space_id)

    @app.post(prefix + '/knowledge-bases/{kb_id}/bindings')
    def bind(kb_id: str, body: BindingAction, source_space_id='local', actor=Depends(principal)): return catalog.bind(kb_id, body, actor, source_space_id)

    @app.get(prefix + '/bindings')
    def bindings(space_id='local', actor=Depends(principal)): return catalog.bindings(actor, space_id)

    @app.put(prefix + '/knowledge-bases/{kb_id}/binding')
    def edit_binding(kb_id: str, body: BindingEdit, space_id='local', actor=Depends(principal)): return catalog.edit_binding(kb_id, body, actor, space_id)

    @app.get(prefix + '/knowledge-bases/{kb_id}/versions')
    def versions(kb_id: str, space_id='local', actor=Depends(principal)): return catalog.list_versions(kb_id, actor, space_id)

    @app.post(prefix + '/knowledge-bases/{kb_id}/versions/{operation}')
    def version(kb_id: str, operation: str, body: VersionAction, space_id='local', actor=Depends(principal)): return catalog.change_version(kb_id, operation, body, actor, space_id)

    @app.get(prefix + '/knowledge-bases/{kb_id}/documents')
    def documents(kb_id: str, space_id='local', actor=Depends(principal)): return content.documents(kb_id, actor, space_id)

    @app.post(prefix + '/knowledge-bases/{kb_id}/documents', status_code=202)
    def upload(kb_id: str, body: Upload, actor=Depends(principal)): return content.upload(kb_id, body, actor)

    @app.get(prefix + '/jobs/{job_id}')
    def job(job_id: str, space_id='local', actor=Depends(principal)): return content.job(job_id, actor, space_id)

    @app.get(prefix + '/jobs')
    def jobs(space_id='local', status: str | None=None, limit: int=20, actor=Depends(principal)):
        catalog.scope(actor, space_id)
        with runtime.engine.connect() as db: rows = db.execute(select(s.jobs).order_by(s.jobs.c.created_at.desc())).mappings().all()
        result = []
        for row in rows:
            if status and row['status'] != status: continue
            try: item = content.job(row['id'], actor, space_id)
            except HTTPException: continue
            with runtime.engine.connect() as db:
                branch = catalog.versions.branch(db, catalog.scope(actor, space_id, item['knowledge_base_id']))
            if item['knowledge_base_version_id'] != branch['head_version_id']: continue
            result.append(item)
        return result[:limit]

    @app.post(prefix + '/jobs/{job_id}/cancel')
    def cancel(job_id: str, space_id='local', actor=Depends(principal)):
        content.job(job_id, actor, space_id, 'write'); runtime.tasks.cancel(job_id)
        return content.job(job_id, actor, space_id)

    @app.get(prefix + '/documents/{document_id}')
    def document(document_id: str, space_id='local', actor=Depends(principal)): return content.get(s.documents, document_id, actor, space_id)

    @app.put(prefix + '/documents/{document_id}')
    def edit_document(document_id: str, body: Edit, space_id='local', actor=Depends(principal)): return content.edit(s.documents, document_id, body, actor, space_id)

    @app.delete(prefix + '/documents/{document_id}')
    def archive(document_id: str, space_id='local', expected_updated_at: str | None=None, actor=Depends(principal)):
        row, context = content.locate(s.documents, document_id, actor, space_id, 'write')
        return runtime.content.archive(document_id, context, actor, expected_updated_at)

    @app.post(prefix + '/query')
    def query(body: QueryRequest, actor=Depends(principal)): return jsonable_encoder(content.query(body, actor))

    @app.get(prefix + '/citations/{chunk_id}')
    def citation(chunk_id: str, space_id='local', actor=Depends(principal)):
        context = catalog.scope(actor, space_id)
        if ':' not in chunk_id:
            with runtime.engine.connect() as db:
                current = db.execute(select(s.chunks.c.id).where(s.chunks.c.id == chunk_id)).scalar()
            if current: return content.get(s.chunks, chunk_id, actor, space_id)
        row = runtime.evidence.resolve(chunk_id, context, actor)
        if 'tenant_id' in row: return row
        return content.projection.content(row, actor, context)

    def concept_view(row, actor, context):
        result = content.projection.content(row, actor, context)
        from .modules.okf_rules import parse_okf_markdown
        frontmatter = parse_okf_markdown(row['concept_id'], row['content_md']).frontmatter
        return {**result, 'frontmatter': frontmatter, 'description': frontmatter.get('description'), 'status': 'active'}

    def scope_concepts(kb_id, actor, space_id, materialize=False):
        context = catalog.scope(actor, space_id, kb_id)
        if materialize:
            runtime.authorize(actor, context, 'write')
            documents = runtime.content.list(s.documents, context, actor)
            with runtime.engine.begin() as db:
                for document in documents:
                    if document['status'] != 'ready' or db.execute(select(concepts.concepts.c.id).where(
                        concepts.concepts.c.document_id == document['id'])).first(): continue
                    if document['metadata'].get('section_tree') or db.execute(select(s.buckets.c.id).where(s.buckets.c.document_id == document['id'])).first():
                        content.refresh_wiki(db, document, concepts.rows(context))
        return context, concepts.rows(context)

    @app.get(prefix + '/knowledge-bases/{kb_id}/concepts')
    def list_concepts(kb_id: str, space_id='local', materialize: bool=False, actor=Depends(principal)):
        context, rows = scope_concepts(kb_id, actor, space_id, materialize)
        return jsonable_encoder([concept_view(row, actor, context) for row in rows])

    @app.get(prefix + '/knowledge-bases/{kb_id}/concepts/{concept_id:path}')
    def get_concept(kb_id: str, concept_id: str, space_id='local', actor=Depends(principal)):
        context, rows = scope_concepts(kb_id, actor, space_id)
        row = next((row for row in rows if row['concept_id'] == concept_id.removesuffix('.md')), None)
        if not row: raise HTTPException(404, 'Concept not found')
        return jsonable_encoder(concept_view(row, actor, context))

    @app.put(prefix + '/knowledge-bases/{kb_id}/concepts/{concept_id:path}')
    def put_concept(kb_id: str, concept_id: str, body: ConceptEdit, space_id='local', actor=Depends(principal)):
        context, rows = scope_concepts(kb_id, actor, space_id)
        catalog.scope(actor, space_id, kb_id, 'write')
        if not body.content_md.strip(): raise HTTPException(422, 'Concept ID and content cannot be empty')
        before = next((row for row in rows if row['concept_id'] == concept_id.removesuffix('.md')), None)
        if before and body.create_only: raise HTTPException(409, 'Concept already exists')
        if before and not body.expected_updated_at: raise HTTPException(422, 'expected_updated_at is required')
        if before: catalog.check_revision(before, body.expected_updated_at)
        if not before and body.expected_updated_at: raise HTTPException(409, 'Concept no longer exists at the expected revision')
        document_id = body.document_id if 'document_id' in body.model_fields_set else (before['document_id'] if before else None)
        if document_id: runtime.content.get(s.documents, document_id, context, actor, 'write')
        with runtime.engine.begin() as db:
            if before and document_id != before['document_id']:
                db.execute(update(concepts.concepts).where(concepts.concepts.c.id == before['id']).values(document_id=document_id))
            row = concepts.upsert(db, {'concept_id': concept_id, 'content_md': body.content_md,
                'create_only': body.create_only,
                'source_refs': before['source_refs'] if before else []}, document_id, body.expected_updated_at)
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert
            scope = {**context, 'version_id': catalog.versions.branch(db, context)['head_version_id']}
            db.execute(sqlite_insert(concepts.contexts).values(concept_id=row['id'], context=scope).on_conflict_do_update(
                index_elements=['concept_id'], set_={'context': scope}))
        return jsonable_encoder(concept_view(row, actor, context))

    @app.get(prefix + '/knowledge-bases/{kb_id}/okf')
    def export_okf(kb_id: str, space_id='local', actor=Depends(principal)):
        from .modules.okf_rules import build_index_markdown, build_log_markdown, parse_okf_markdown
        context, rows = scope_concepts(kb_id, actor, space_id)
        base = catalog.get(kb_id, actor, space_id)
        items = [SimpleNamespace(**row, description=None,
            frontmatter_json=parse_okf_markdown(row['concept_id'], row['content_md']).frontmatter) for row in rows]
        buffer = BytesIO()
        with ZipFile(buffer, 'w', ZIP_DEFLATED) as archive:
            archive.writestr('index.md', build_index_markdown(SimpleNamespace(**base), items))
            archive.writestr('log.md', build_log_markdown([], items))
            for row in rows: archive.writestr(row['concept_id'] + '.md', row['content_md'])
        return Response(buffer.getvalue(), media_type='application/zip', headers={'Content-Disposition': 'attachment; filename="knowledge.okf.zip"'})

    @app.post(prefix + '/knowledge-bases/{kb_id}/okf/lint')
    def lint(kb_id: str, space_id='local', materialize: bool=False, actor=Depends(principal)):
        from .modules import okf_rules
        context, rows = scope_concepts(kb_id, actor, space_id, materialize)
        items = [SimpleNamespace(**row, frontmatter_json=okf_rules.parse_okf_markdown(row['concept_id'], row['content_md']).frontmatter,
            citations_json=row['citations'], links_json=row['links']) for row in rows]
        issues = okf_rules.lint_okf_concepts(items)
        with runtime.engine.begin() as db:
            branch = catalog.versions.branch(db, context)
            existing = set(db.execute(select(discovery.suggestions.c.title).join(discovery.contexts,
                discovery.suggestions.c.id == discovery.contexts.c.suggestion_id).where(discovery.suggestions.c.status == 'pending',
                    discovery.contexts.c.context['knowledge_base_id'].as_string() == kb_id,
                    discovery.contexts.c.context['version_id'].as_string() == branch['head_version_id'])).scalars())
            for issue in issues:
                issue.update(title=f"{issue['title']}：{issue['message']}", knowledge_base_id=kb_id,
                    knowledge_base_version_id=branch['head_version_id'])
                if issue['title'] in existing: continue
                suggestion_id = s.identifier('kdisc')
                db.execute(discovery.suggestions.insert().values(id=suggestion_id, document_id=issue['document_id'] or '',
                    suggestion_type='warning', title=issue['title'], status='pending', payload={**issue, 'okf_lint': True},
                    source_refs=[{'concept_id': issue['concept_id']}], reason=issue['message'], created_at=s.now(), updated_at=s.now()))
                db.execute(discovery.contexts.insert().values(suggestion_id=suggestion_id, context={**context, 'version_id': branch['head_version_id']}))
                existing.add(issue['title'])
        return {'issue_count': len(issues), 'issues': issues}

    @app.post(prefix + '/knowledge-bases/{kb_id}/okf')
    def import_okf(kb_id: str, body: Upload, actor=Depends(principal)):
        from .modules.okf_rules import parse_okf_bundle
        context = catalog.scope(actor, body.space_id, kb_id, 'write')
        try: parsed = parse_okf_bundle(body.filename, base64.b64decode(body.content_base64, validate=True))
        except Exception as error: raise HTTPException(422, str(error)) from error
        with runtime.engine.begin() as db:
            catalog.writable_branch(db, context)
            document = {'id': s.identifier('kdoc'), 'filename': body.filename, 'file_type': 'okf', 'title': body.title or body.filename,
                'content': '', 'metadata': {}, 'status': 'ready', 'bucket_count': 0, 'chunk_count': 0, 'created_at': s.now(), 'updated_at': s.now()}
            db.execute(s.documents.insert().values(**document))
            runtime.assign_content(db, document['id'], context)
            existing = concepts.rows(context)
            saved = []
            for item in parsed:
                before = next((row for row in existing if row['concept_id'] == item.concept_id), None)
                if before: db.execute(update(concepts.concepts).where(concepts.concepts.c.id == before['id']).values(document_id=document['id']))
                saved.append(concepts.upsert(db, {'concept_id': item.concept_id, 'content_md': item.content_md},
                    document['id'], before['updated_at'].isoformat() if before else None))
            content.concept_evidence(db, document, saved)
        return {'document_id': document['id'], 'concept_count': len(parsed)}

    @app.get(prefix + '/knowledge-bases/{kb_id}/suggestions')
    def suggestions(kb_id: str, space_id='local', actor=Depends(principal)):
        context = catalog.scope(actor, space_id, kb_id)
        with runtime.engine.connect() as db: rows = db.execute(select(discovery.suggestions)).mappings().all()
        with runtime.engine.connect() as db:
            scopes = {row['suggestion_id']: row['context'] for row in db.execute(select(discovery.contexts)).mappings()}
        return jsonable_encoder([content.projection.content(dict(row), actor, context) for row in rows
            if row['document_id'] and runtime.visible(row['document_id'], context)] + [content.projection.content(dict(row), actor, context)
                for row in rows if not row['document_id'] and all(scopes.get(row['id'], {}).get(key) == context.get(key)
                    for key in ('tenant_id', 'space_id', 'knowledge_base_id'))])

    @app.post(prefix + '/suggestions/{suggestion_id}/{operation}')
    def suggestion(suggestion_id: str, operation: str, space_id='local', actor=Depends(principal)):
        context = catalog.scope(actor, space_id, action='read' if operation == 'export' else 'write')
        row = discovery.get(suggestion_id, context, actor, 'read' if operation == 'export' else 'write')
        if operation == 'export': return jsonable_encoder(content.projection.content(row, actor, context))
        if operation == 'publish':
            if not runtime.publisher: raise HTTPException(404, 'Publication unavailable')
            return runtime.plugins['publication'].publish(suggestion_id, context, actor)
        if operation not in {'review', 'reject'}: raise HTTPException(404, 'Suggestion operation not found')
        if row['status'] != 'pending': raise HTTPException(409, 'Only pending suggestions can be reviewed or rejected')
        status = 'reviewed' if operation == 'review' else 'rejected'
        with runtime.engine.begin() as db: db.execute(update(discovery.suggestions).where(discovery.suggestions.c.id == suggestion_id).values(status=status, updated_at=s.now()))
        return {'status': status, 'published': False}

    def register_layer(name, table):
        @app.get(prefix + '/' + name + '/{resource_id}')
        def get(resource_id: str, space_id='local', actor=Depends(principal)):
            if name == 'suggestions':
                context = catalog.scope(actor, space_id)
                row = discovery.get(resource_id, context, actor, 'read')
                with runtime.engine.connect() as db:
                    scope = db.execute(select(discovery.contexts.c.context).where(discovery.contexts.c.suggestion_id == resource_id)).scalar() or {}
                return jsonable_encoder(content.projection.content(row, actor, {**context, 'knowledge_base_id': scope.get('knowledge_base_id')}))
            if name == 'concepts':
                row, context = content.locate(table, resource_id, actor, space_id)
                return jsonable_encoder(concept_view(row, actor, context))
            return content.get(table, resource_id, actor, space_id)
        if name != 'suggestions':
            @app.put(prefix + '/' + name + '/{resource_id}')
            def edit(resource_id: str, body: LayerEdit, space_id='local', actor=Depends(principal)):
                if name != 'concepts': return content.edit(table, resource_id, body, actor, space_id)
                row, context = content.locate(table, resource_id, actor, space_id, 'write')
                catalog.check_revision(row, body.expected_updated_at)
                with runtime.engine.begin() as db:
                    saved = concepts.upsert(db, {'concept_id': row['concept_id'], 'content_md': body.content_md or row['content_md'],
                        'source_refs': row['source_refs']}, row['document_id'], body.expected_updated_at)
                return jsonable_encoder(concept_view(saved, actor, context))
        @app.get(prefix + '/documents/{document_id}/' + name)
        def layers(document_id: str, space_id='local', actor=Depends(principal)):
            if name == 'concepts':
                _, context = content.locate(s.documents, document_id, actor, space_id)
                with runtime.engine.connect() as db:
                    rows = db.execute(select(table).where(table.c.document_id == document_id)).mappings().all()
                return jsonable_encoder([concept_view(dict(row), actor, context) for row in rows])
            rows = content.layers(document_id, table, actor, space_id)
            return rows
    for name, table in (('buckets', s.buckets), ('chunks', s.chunks), ('concepts', concepts.concepts), ('suggestions', discovery.suggestions)):
        register_layer(name, table)

    @app.get(prefix + '/buckets/{bucket_id}/chunks')
    def bucket_chunks(bucket_id: str, space_id='local', actor=Depends(principal)):
        bucket, context = content.locate(s.buckets, bucket_id, actor, space_id)
        with runtime.engine.connect() as db: rows = db.execute(select(s.chunks).where(s.chunks.c.bucket_id == bucket_id).order_by(s.chunks.c.chunk_index)).mappings().all()
        return [content.projection.content(dict(row), actor, context) for row in rows]
