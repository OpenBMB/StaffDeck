"""Full catalog contracts backed by the same module-owned tables as partial mode."""
from copy import deepcopy
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import delete, select, update
from .kernel import storage as s
from .full_projection import FullProjection


class FullCatalog:
    def __init__(self, runtime):
        self.runtime = runtime
        self.catalog = runtime.plugins['catalog']
        self.spaces = runtime.plugins['spaces']
        self.versions = runtime.plugins['versions']
        self.projection = FullProjection(runtime)

    def scope(self, actor, space_id='local', base_id=None, action='read'):
        context = {'tenant_id': actor.tenant_id, 'space_id': space_id}
        self.runtime.authorize(actor, context, action)
        if base_id:
            context['knowledge_base_id'] = base_id
            self.catalog.validate(context)
        return context

    def base(self, db, base_id, actor):
        row = db.execute(select(self.catalog.catalogs).where(self.catalog.catalogs.c.id == base_id,
            self.catalog.catalogs.c.tenant_id == actor.tenant_id)).mappings().first()
        if not row: raise HTTPException(404, 'Resource not found')
        return dict(row)

    def binding(self, db, base_id, context):
        row = db.execute(select(self.catalog.bindings).where(self.catalog.bindings.c.knowledge_base_id == base_id,
            self.catalog.bindings.c.space_id == context['space_id'])).mappings().first()
        if not row or row['hidden']: raise HTTPException(404, 'Knowledge binding not found')
        return dict(row)

    def get(self, base_id, actor, space_id='local'):
        context = self.scope(actor, space_id, base_id)
        with self.runtime.engine.connect() as db:
            self.binding(db, base_id, context)
            row = self.base(db, base_id, actor)
            branch = self.versions.branch(db, context)
        return {**self.projection.binding(row, actor), 'branch': self.projection.branch(branch, actor, context)}

    def list(self, actor, space_id='local'):
        context = self.scope(actor, space_id)
        with self.runtime.engine.connect() as db:
            rows = db.execute(select(self.catalog.catalogs).join(self.catalog.bindings,
                self.catalog.catalogs.c.id == self.catalog.bindings.c.knowledge_base_id).where(
                self.catalog.bindings.c.space_id == space_id, self.catalog.bindings.c.hidden == False,
                self.catalog.catalogs.c.tenant_id == actor.tenant_id)).mappings().all()
        return [self.projection.binding(dict(row), actor) for row in rows]

    def create(self, body, actor):
        context = self.scope(actor, body.space_id, action='write')
        name = body.name.strip()
        if not name: raise HTTPException(400, 'Knowledge base name cannot be empty')
        moment = s.now()
        row = {'id': s.identifier('kb'), 'tenant_id': actor.tenant_id, 'name': name, 'description': body.description,
               'status': 'active', 'capability_scope': body.capability_scope,
               'metadata': {**body.metadata, 'owner_id': actor.id, 'owner_name': actor.display_name},
               'created_at': moment, 'updated_at': moment}
        version_id = f"kbver_{row['id']}_1_0_0"
        row['metadata']['published_version_id'] = version_id
        with self.runtime.engine.begin() as db:
            duplicate = db.execute(select(self.catalog.catalogs.c.id).where(self.catalog.catalogs.c.tenant_id == actor.tenant_id,
                self.catalog.catalogs.c.name == name)).first()
            if duplicate: raise HTTPException(409, 'Knowledge base name already exists')
            db.execute(self.catalog.catalogs.insert().values(**row))
            db.execute(self.catalog.bindings.insert().values(knowledge_base_id=row['id'], space_id=body.space_id))
            context['knowledge_base_id'] = row['id']
            branch = self.versions.branch(db, context, create=True)
            db.execute(self.versions.versions.insert().values(id=version_id, knowledge_base_id=row['id'], name=name,
                version='1.0.0', description=None, status='active', capability_scope=body.capability_scope,
                metadata={}, snapshot={}, created_at=moment, updated_at=moment))
            db.execute(self.versions.branches.update().where(self.versions.branches.c.id == branch['id']).values(
                head_version_id=version_id, base_version_id=version_id, published_version_id=version_id))
            self.grant(db, body.space_id, version_id)
        return self.projection.binding(row, actor)

    def grant(self, db, space_id, version_id):
        table = self.spaces.version_access
        if not db.execute(select(table).where(table.c.space_id == space_id, table.c.version_id == version_id)).first():
            db.execute(table.insert().values(space_id=space_id, version_id=version_id))

    def check_revision(self, row, expected):
        if row['updated_at'].isoformat() != expected: raise HTTPException(409, 'Resource changed; reload before saving')

    def edit(self, base_id, body, actor, space_id='local'):
        context = self.scope(actor, space_id, base_id, 'write')
        with self.runtime.engine.begin() as db:
            row = self.base(db, base_id, actor)
            self.check_revision(row, body.expected_updated_at)
            values = body.model_dump(exclude_unset=True, exclude={'expected_updated_at'})
            if values.get('status', 'active') not in {'active', 'archived'}: raise HTTPException(422, 'Invalid status')
            if 'metadata' in values:
                values['metadata'] = {**(values['metadata'] or {}), **{key: row['metadata'][key] for key in
                    ('owner_id', 'owner_name', 'published_version_id') if key in row['metadata']}}
            if 'name' in values:
                duplicate = db.execute(select(self.catalog.catalogs.c.id).where(self.catalog.catalogs.c.id != base_id,
                    self.catalog.catalogs.c.tenant_id == actor.tenant_id, self.catalog.catalogs.c.name == values['name'])).first()
                if duplicate: raise HTTPException(409, 'Knowledge base name already exists')
            values['updated_at'] = s.now()
            db.execute(update(self.catalog.catalogs).where(self.catalog.catalogs.c.id == base_id).values(**values))
        return self.projection.binding({**row, **values}, actor)

    def visible_version(self, db, version_id, context):
        base = db.execute(select(self.catalog.catalogs).where(self.catalog.catalogs.c.id == context['knowledge_base_id'])).mappings().one()
        branch = self.versions.branch(db, context)
        if version_id in {branch['head_version_id'], branch['base_version_id'], base['metadata'].get('published_version_id')}:
            return
        if not db.execute(select(self.spaces.version_access).where(self.spaces.version_access.c.space_id == context['space_id'],
            self.spaces.version_access.c.version_id == version_id)).first(): raise HTTPException(404, 'Version not visible in this space')

    def list_versions(self, base_id, actor, space_id='local'):
        context = self.scope(actor, space_id, base_id)
        with self.runtime.engine.connect() as db:
            branch = self.versions.branch(db, context)
            base = self.base(db, base_id, actor)
            allowed = {branch['head_version_id'], branch['base_version_id'], base['metadata'].get('published_version_id')}
            allowed.update(db.execute(select(self.spaces.version_access.c.version_id).where(
                self.spaces.version_access.c.space_id == space_id)).scalars())
            rows = db.execute(select(self.versions.versions).where(self.versions.versions.c.knowledge_base_id == base_id,
                self.versions.versions.c.id.in_(allowed))).mappings().all()
        return [self.projection.version(dict(row), actor) for row in rows]

    def checkpoint_head(self, db, branch):
        if branch['head_version_id']:
            db.execute(update(self.versions.versions).where(self.versions.versions.c.id == branch['head_version_id']).values(
                snapshot=self.versions.snapshot(db, branch)))

    def activate(self, db, branch, target, context):
        db.execute(delete(self.versions.contents).where(self.versions.contents.c.branch_id == branch['id']))
        for document_id, snapshot in target['snapshot'].items():
            if not db.execute(select(s.documents.c.id).where(s.documents.c.id == document_id)).first():
                from datetime import datetime
                def restore_row(table, source):
                    row = {key: value for key, value in source.items() if key in table.c}
                    for key in ('created_at', 'updated_at'):
                        if isinstance(row.get(key), str): row[key] = datetime.fromisoformat(row[key])
                    db.execute(table.insert().values(**row))
                restore_row(s.documents, snapshot)
                for table, key in ((s.buckets, 'buckets'), (s.chunks, 'chunks')):
                    for source in snapshot[key]: restore_row(table, source)
                self.catalog.assign(db, document_id, context)
                for name, rows in snapshot.get('modules', {}).items():
                    plugin = self.runtime.plugins.get(name)
                    table = getattr(plugin, 'concepts', None) if name == 'concepts' else getattr(plugin, 'suggestions', None) if name == 'discovery' else None
                    if table is not None:
                        for source in rows or []:
                            if not db.execute(select(table.c.id).where(table.c.id == source['id'])).first():
                                restore_row(table, source)
                                contexts = plugin.contexts
                                field = 'concept_id' if name == 'concepts' else 'suggestion_id'
                                db.execute(contexts.insert().values(**{field: source['id'], 'context': {**context, 'version_id': target['id']}}))
            db.execute(self.versions.contents.insert().values(branch_id=branch['id'], document_id=document_id))
            self.spaces.assign(db, document_id, context)
        db.execute(update(self.versions.branches).where(self.versions.branches.c.id == branch['id']).values(head_version_id=target['id']))

    def clone(self, db, context, source, *, label=None, empty=False, preserve_metadata=False, name=None, description=None):
        branch = self.versions.branch(db, context)
        self.checkpoint_head(db, branch)
        source = dict(db.execute(select(self.versions.versions).where(self.versions.versions.c.id == source['id'])).mappings().one())
        base = db.execute(select(self.catalog.catalogs).where(self.catalog.catalogs.c.id == source['knowledge_base_id'])).mappings().one()
        target = {**source, 'id': s.identifier('kbver'), 'version': label or 'draft-' + uuid4().hex,
                  'parent_id': source['id'], 'name': name or source['name'],
                  'description': base['description'] if preserve_metadata else source['description'], 'created_at': s.now(), 'updated_at': s.now()}
        target['snapshot'] = {} if empty else deepcopy(source['snapshot'])
        db.execute(self.versions.versions.insert().values(**target))
        concepts = self.runtime.plugins.get('concepts')
        if concepts and not empty:
            rows = db.execute(select(concepts.concepts, concepts.contexts.c.context).join(concepts.contexts,
                concepts.concepts.c.id == concepts.contexts.c.concept_id).where(concepts.concepts.c.document_id.is_(None))).mappings().all()
            for row in rows:
                if row['context'].get('version_id') != source['id']: continue
                item = {key: row[key] for key in concepts.concepts.c.keys()}
                item.update(id=s.identifier('kconcept'), updated_at=s.now())
                db.execute(concepts.concepts.insert().values(**item))
                db.execute(concepts.contexts.insert().values(concept_id=item['id'], context={**row['context'], 'version_id': target['id']}))
        self.grant(db, context['space_id'], target['id'])
        self.versions.restore(db, branch, target, context, preserve_metadata=preserve_metadata)
        branch = dict(db.execute(select(self.versions.branches).where(self.versions.branches.c.id == branch['id'])).mappings().one())
        target['snapshot'] = self.versions.snapshot(db, branch)
        db.execute(update(self.versions.versions).where(self.versions.versions.c.id == target['id']).values(snapshot=target['snapshot']))
        db.execute(update(self.versions.branches).where(self.versions.branches.c.id == branch['id']).values(sync_state='modified'))
        return target

    def writable_branch(self, db, context):
        branch = self.versions.branch(db, context)
        base = db.execute(select(self.catalog.catalogs).where(self.catalog.catalogs.c.id == context['knowledge_base_id'])).mappings().one()
        shared = db.execute(select(self.versions.branches.c.id).where(self.versions.branches.c.id != branch['id'],
            self.versions.branches.c.head_version_id == branch['head_version_id'])).first()
        if shared or base['metadata'].get('published_version_id') == branch['head_version_id']:
            source = db.execute(select(self.versions.versions).where(self.versions.versions.c.id == branch['head_version_id'])).mappings().one()
            self.clone(db, context, source)
            branch = dict(db.execute(select(self.versions.branches).where(self.versions.branches.c.id == branch['id'])).mappings().one())
        return branch

    def change_version(self, base_id, operation, body, actor, space_id='local'):
        context = self.scope(actor, space_id, base_id, 'write')
        with self.runtime.engine.begin() as db:
            branch = self.versions.branch(db, context)
            base = self.base(db, base_id, actor)
            self.checkpoint_head(db, branch)
            if operation == 'copy':
                source_id = body.version_id or branch['head_version_id']
                source = db.execute(select(self.versions.versions).where(self.versions.versions.c.id == source_id)).mappings().first()
                if not source: raise HTTPException(404, 'Resource not found')
                if source['knowledge_base_id'] != base_id: raise HTTPException(409, 'Version belongs to another knowledge base')
                self.visible_version(db, source_id, context)
                self.clone(db, context, source, label=body.version_label, empty=body.empty,
                    preserve_metadata=body.preserve_metadata, name=body.name, description=body.description)
            elif operation == 'publish':
                db.execute(update(self.catalog.catalogs).where(self.catalog.catalogs.c.id == base_id).values(
                    metadata={**base['metadata'], 'published_version_id': branch['head_version_id']}))
                db.execute(update(self.versions.branches).where(self.versions.branches.c.id == branch['id']).values(
                    base_version_id=branch['head_version_id'], published_version_id=branch['head_version_id'], sync_state='synced'))
            elif operation in {'sync', 'rollback'}:
                target_id = base['metadata'].get('published_version_id') if operation == 'sync' else body.version_id
                target = db.execute(select(self.versions.versions).where(self.versions.versions.c.id == target_id)).mappings().first()
                if not target: raise HTTPException(404, 'Resource not found')
                if target['knowledge_base_id'] != base_id: raise HTTPException(409, 'Version belongs to another knowledge base')
                self.visible_version(db, target_id, context)
                self.activate(db, branch, target, context)
                db.execute(update(self.versions.branches).where(self.versions.branches.c.id == branch['id']).values(
                    base_version_id=target_id if operation == 'sync' else branch['base_version_id'],
                    sync_state='synced' if operation == 'sync' or target_id == branch['base_version_id'] else 'modified'))
            else: raise HTTPException(404, 'Version operation not found')
            db.execute(update(self.versions.branches).where(self.versions.branches.c.id == branch['id']).values(updated_at=s.now()))
            branch = dict(db.execute(select(self.versions.branches).where(self.versions.branches.c.id == branch['id'])).mappings().one())
            self.grant(db, space_id, branch['head_version_id'])
        return self.projection.branch(branch, actor, context)

    def bind(self, base_id, body, actor, source_space_id='local'):
        source = self.scope(actor, source_space_id, base_id)
        target = self.scope(actor, body.space_id, action='write')
        target['knowledge_base_id'] = base_id
        with self.runtime.engine.begin() as db:
            branch = self.versions.branch(db, source)
            self.checkpoint_head(db, branch)
            table = self.catalog.bindings
            row = db.execute(select(table).where(table.c.knowledge_base_id == base_id, table.c.space_id == body.space_id)).mappings().first()
            if row:
                db.execute(update(table).where(table.c.id == row['id']).values(hidden=body.hidden, updated_at=s.now()))
            else:
                db.execute(table.insert().values(knowledge_base_id=base_id, space_id=body.space_id, hidden=body.hidden))
                remote = self.versions.branch(db, target, create=True)
                snapshot = db.execute(select(self.versions.versions).where(self.versions.versions.c.id == branch['head_version_id'])).mappings().one()
                self.activate(db, remote, snapshot, target)
                db.execute(update(self.versions.branches).where(self.versions.branches.c.id == remote['id']).values(
                    base_version_id=branch['base_version_id']))
            self.grant(db, body.space_id, branch['head_version_id'])
            self.grant(db, body.space_id, branch['base_version_id'])
            row = dict(db.execute(select(table).where(table.c.knowledge_base_id == base_id, table.c.space_id == body.space_id)).mappings().one())
        return self.projection.binding(row, actor)

    def bindings(self, actor, space_id='local'):
        context = self.scope(actor, space_id)
        with self.runtime.engine.connect() as db:
            rows = db.execute(select(self.catalog.bindings).where(self.catalog.bindings.c.space_id == space_id)).mappings().all()
            result = []
            for row in rows:
                base = self.base(db, row['knowledge_base_id'], actor)
                branch = self.versions.branch(db, {**context, 'knowledge_base_id': row['knowledge_base_id']})
                head = db.execute(select(self.versions.versions).where(self.versions.versions.c.id == branch['head_version_id'])).mappings().one()
                result.append({**self.projection.binding(dict(row), actor), 'branch': self.projection.branch(branch, actor, context),
                    'metadata': {**row['metadata'], 'name': head['name'] if branch['sync_state'] == 'modified' else base['name']}})
        return result

    def edit_binding(self, base_id, body, actor, space_id='local'):
        context = self.scope(actor, space_id, action='write'); context['knowledge_base_id'] = base_id
        with self.runtime.engine.begin() as db:
            self.base(db, base_id, actor)
            table = self.catalog.bindings
            row = db.execute(select(table).where(table.c.knowledge_base_id == base_id, table.c.space_id == space_id)).mappings().first()
            if not row: raise HTTPException(404, 'Knowledge binding not found')
            branch = self.versions.branch(db, context)
            if body.status is not None:
                if body.status not in {'active', 'inactive', 'deleted'}: raise HTTPException(422, 'Invalid binding status')
                db.execute(update(self.versions.branches).where(self.versions.branches.c.id == branch['id']).values(status=body.status))
            changes = {key: getattr(body, key) for key in ('hidden', 'metadata') if getattr(body, key) is not None}
            changes['updated_at'] = s.now()
            db.execute(update(table).where(table.c.id == row['id']).values(**changes))
            db.execute(update(self.versions.branches).where(self.versions.branches.c.id == branch['id']).values(updated_at=s.now()))
            branch = dict(db.execute(select(self.versions.branches).where(self.versions.branches.c.id == branch['id'])).mappings().one())
        return {**self.projection.binding({**row, **changes}, actor), 'branch': self.projection.branch(branch, actor, context)}

    def edit_branch(self, base_id, body, actor, space_id='local'):
        context = self.scope(actor, space_id, base_id, 'write')
        with self.runtime.engine.begin() as db:
            branch = self.versions.branch(db, context)
            row = dict(db.execute(select(self.versions.versions).where(self.versions.versions.c.id == branch['head_version_id'])).mappings().one())
            self.check_revision(row, body.expected_updated_at)
            branch = self.writable_branch(db, context)
            row = dict(db.execute(select(self.versions.versions).where(self.versions.versions.c.id == branch['head_version_id'])).mappings().one())
            changes = {key: getattr(body, key) for key in ('name', 'description', 'capability_scope') if key in body.model_fields_set and getattr(body, key) is not None}
            if 'name' in changes:
                changes['name'] = changes['name'].strip()
                if not changes['name']: raise HTTPException(400, 'Knowledge base name cannot be empty')
            if body.metadata is not None: changes['metadata'] = {**row['metadata'], **body.metadata}
            if body.status is not None:
                if body.status not in {'active', 'archived'}: raise HTTPException(422, 'Invalid status')
                db.execute(update(self.versions.branches).where(self.versions.branches.c.id == branch['id']).values(status='active' if body.status == 'active' else 'inactive'))
            changes['updated_at'] = s.now()
            db.execute(update(self.versions.versions).where(self.versions.versions.c.id == row['id']).values(**changes))
            db.execute(update(self.versions.branches).where(self.versions.branches.c.id == branch['id']).values(sync_state='modified', updated_at=s.now()))
        return self.projection.version({**row, **changes}, actor)

    def remove(self, base_id, actor, space_id='local', permanent=False):
        context = self.scope(actor, space_id, base_id, 'write')
        with self.runtime.engine.begin() as db:
            binding = self.binding(db, base_id, context)
            if not permanent:
                db.execute(update(self.catalog.bindings).where(self.catalog.bindings.c.id == binding['id']).values(hidden=True))
                return {'status': 'hidden'}
            for space in db.execute(select(self.catalog.bindings.c.space_id).where(self.catalog.bindings.c.knowledge_base_id == base_id)).scalars():
                self.runtime.authorization.authorize(actor, space, 'write')
            jobs = db.execute(select(s.jobs)).mappings().all()
            if any(row['context'].get('knowledge_base_id') == base_id and row['status'] not in {'succeeded', 'failed', 'cancelled'} for row in jobs):
                raise HTTPException(409, 'Cancel active tasks before deleting')
            ids = set(db.execute(select(self.catalog.contents.c.document_id).where(self.catalog.contents.c.knowledge_base_id == base_id)).scalars())
            for table in (s.chunks, s.buckets, s.citations): db.execute(delete(table).where(table.c.document_id.in_(ids)))
            discovery, concepts = self.runtime.plugins['discovery'], self.runtime.plugins['concepts']
            suggestions = select(discovery.suggestions.c.id).where(discovery.suggestions.c.document_id.in_(ids))
            db.execute(delete(self.runtime.plugins['publication'].receipts).where(self.runtime.plugins['publication'].receipts.c.suggestion_id.in_(suggestions)))
            db.execute(delete(discovery.contexts).where(discovery.contexts.c.suggestion_id.in_(suggestions)))
            db.execute(delete(discovery.suggestions).where(discovery.suggestions.c.document_id.in_(ids)))
            db.execute(delete(concepts.concepts).where(concepts.concepts.c.document_id.in_(ids)))
            db.execute(delete(s.documents).where(s.documents.c.id.in_(ids)))
            db.execute(delete(self.spaces.contents).where(self.spaces.contents.c.document_id.in_(ids)))
            db.execute(delete(self.versions.contents).where(self.versions.contents.c.document_id.in_(ids)))
            branches = select(self.versions.branches.c.id).where(self.versions.branches.c.knowledge_base_id == base_id)
            versions = select(self.versions.versions.c.id).where(self.versions.versions.c.knowledge_base_id == base_id)
            db.execute(delete(self.spaces.branch_scopes).where(self.spaces.branch_scopes.c.branch_id.in_(branches)))
            db.execute(delete(self.spaces.version_access).where(self.spaces.version_access.c.version_id.in_(versions)))
            for table in (self.versions.branches, self.versions.versions, self.catalog.contents, self.catalog.bindings):
                db.execute(delete(table).where(table.c.knowledge_base_id == base_id))
            for job in jobs:
                if job['context'].get('knowledge_base_id') == base_id: db.execute(delete(s.jobs).where(s.jobs.c.id == job['id']))
            db.execute(delete(self.catalog.catalogs).where(self.catalog.catalogs.c.id == base_id))
        return {'status': 'deleted'}
