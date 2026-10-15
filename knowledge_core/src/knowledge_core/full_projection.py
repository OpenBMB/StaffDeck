"""Compatibility fields are projected from module relations, never core columns."""
from sqlalchemy import select
from .kernel import storage as s
from .kernel.use_cases import serialize


class FullProjection:
    def __init__(self, runtime): self.runtime = runtime

    def content(self, row, actor, context):
        document_id = row.get('document_id', row['id'])
        catalog, versions = (self.runtime.plugins[name] for name in ('catalog', 'versions'))
        with self.runtime.engine.connect() as db:
            base_id = context.get('knowledge_base_id') or db.execute(select(catalog.contents.c.knowledge_base_id)
                .where(catalog.contents.c.document_id == document_id)).scalar()
            branch = versions.branch(db, {**context, 'knowledge_base_id': base_id})
        common = {'tenant_id': actor.tenant_id, 'knowledge_base_id': base_id,
                  'knowledge_base_version_id': context.get('version_id') or branch['head_version_id']}
        result = {**serialize(row), **common}
        if 'metadata' in result:
            result['metadata'] = {key: value for key, value in result['metadata'].items()
                if key not in {'route_trace', 'revision_source_id', 'revision_source_updated_at', 'capability_context'}}
        if 'metadata' in result and 'bucket_key' in result['metadata']:
            result['metadata'] = {key: value for key, value in result['metadata'].items() if key != 'bucket_key'}
        result.pop('content', None) if 'filename' in row else None
        if 'filename' in row: result['error'] = None
        if 'bucket_id' not in row and 'document_id' in row and 'summary' in row:
            result.pop('content', None)
            result.update(bucket_key=row['metadata'].get('bucket_key', row['id']), token_estimate=max(1, len(row.get('content') or '') // 2))
            result['chunk_count'] = row['metadata'].get('chunk_count', 0)
        return result

    def branch(self, row, actor, context):
        return {'id': row['id'], 'tenant_id': actor.tenant_id, 'space_id': context.get('space_id', 'local'),
            'knowledge_base_id': row['knowledge_base_id'], 'base_version_id': row['base_version_id'],
            'head_version_id': row['head_version_id'], 'status': row['status'], 'sync_state': row['sync_state'],
            'created_at': row['created_at'].isoformat(), 'updated_at': row['updated_at'].isoformat()}

    def version(self, row, actor):
        fields = ('id', 'knowledge_base_id', 'version', 'name', 'description', 'status', 'capability_scope', 'metadata', 'created_at', 'updated_at')
        return {**serialize({key: row[key] for key in fields}), 'tenant_id': actor.tenant_id}

    def binding(self, row, actor):
        return {**serialize(row), 'tenant_id': actor.tenant_id}
