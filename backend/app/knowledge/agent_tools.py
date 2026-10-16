"""Model-facing knowledge operations, guarded by host grants and live resource policy."""
import base64
from urllib.parse import quote
from fastapi import HTTPException
from staffdeck_harness.contracts.invocation import ModuleResult


SPECS = {
    'list_documents': ('GET', '/knowledge-bases/{knowledge_base_id}/documents', [], [], False),
    'get_document': ('GET', '/documents/{document_id}', ['document_id'], [], False),
    'import_document': ('POST', '/knowledge-bases/{knowledge_base_id}/documents', ['title', 'content_md'], ['filename', 'model_id'], True),
    'update_document': ('PUT', '/documents/{document_id}', ['document_id', 'expected_updated_at', 'content_md'], ['title'], True),
    'delete_document': ('DELETE', '/documents/{document_id}', ['document_id', 'expected_updated_at'], [], True),
    'list_document_buckets': ('GET', '/documents/{document_id}/buckets', ['document_id'], [], False),
    'list_document_chunks': ('GET', '/documents/{document_id}/chunks', ['document_id'], [], False),
    'get_bucket': ('GET', '/buckets/{bucket_id}', ['bucket_id'], [], False),
    'get_chunk': ('GET', '/chunks/{chunk_id}', ['chunk_id'], [], False),
    'update_bucket': ('PUT', '/buckets/{bucket_id}', ['bucket_id', 'expected_updated_at', 'content_md'], ['title'], True),
    'update_chunk': ('PUT', '/chunks/{chunk_id}', ['chunk_id', 'expected_updated_at', 'content_md'], [], True),
    'get_job': ('GET', '/jobs/{job_id}', ['job_id'], [], False),
    'cancel_job': ('POST', '/jobs/{job_id}/cancel', ['job_id'], [], True),
    'list_concepts': ('GET', '/knowledge-bases/{knowledge_base_id}/concepts', [], [], False),
    'get_concept': ('GET', '/knowledge-bases/{knowledge_base_id}/concepts/{concept_id}', ['concept_id'], [], False),
    'upsert_concept': ('PUT', '/knowledge-bases/{knowledge_base_id}/concepts/{concept_id}', ['concept_id', 'content_md', 'mode', 'document_id'], ['expected_updated_at'], True),
    'resolve_citation': ('GET', '/citations/{chunk_id}', ['chunk_id'], [], False),
}


def operation(method): return 'knowledge.' + method + '/v1'


def schemas():
    result = {}
    for name, (_, _, required, optional, write) in SPECS.items():
        properties = {field: {'type': 'string', 'minLength': 1} for field in ['knowledge_base_id', *required, *optional]}
        if name == 'upsert_concept': properties['mode'] = {'type': 'string', 'enum': ['create', 'edit']}
        result['knowledge_' + name] = {'operation': operation(name), 'description':
            f'{name}: 操作当前员工绑定且用户有权访问的知识库。修改和删除必须使用读取所得 expected_updated_at；入库返回任务，需轮询成功后才能报告完成。Wiki mode=create 不覆盖已有页面，mode=edit 必须携带修订。',
            'parameters': {'type': 'object', 'properties': properties, 'required': ['knowledge_base_id', *required], 'additionalProperties': False}, 'side_effecting': write}
    return result


def configured_methods():
    from app.config import get_settings
    names = [name.strip() for name in get_settings().knowledge_agent_methods.split(',') if name.strip()]
    unknown = set(names) - {'query', *SPECS}
    if unknown: raise ValueError('Unknown knowledge_agent_methods: ' + ', '.join(sorted(unknown)))
    return names


def supported_methods():
    """Only expose operations advertised by the configured independent owner."""
    import os
    import httpx
    endpoint = os.getenv('STAFFDECK_KM_URL', '').rstrip('/')
    if not endpoint: return set()
    headers = {'Authorization': 'Bearer ' + os.getenv('STAFFDECK_KM_BEARER', '')}
    try:
        response = httpx.get(endpoint + '/api/km/config', headers=headers, timeout=10)
        response.raise_for_status(); catalog = response.json()
        operations = catalog['operations']
        aliases = {'list_concepts': 'list_okf_concepts', 'get_concept': 'get_okf_concept', 'upsert_concept': 'upsert_okf_concept'}
        return {name for name in configured_methods() if name in operations or aliases.get(name) in operations}
    except (httpx.HTTPError, ValueError, KeyError): return set()


def invoke(host, inv):
    from app.db.models import User
    from app.knowledge.http_adapter import StaffDeckKnowledgeAdapter
    name = inv.operation.removeprefix('knowledge.').removesuffix('/v1')
    if name not in SPECS or name not in configured_methods(): return ModuleResult.fail('KNOWLEDGE_OPERATION_DISABLED', 'Agent knowledge operation is not enabled')
    method, path, required, optional, write = SPECS[name]
    value = dict(inv.arguments)
    allowed = set(schemas()['knowledge_' + name]['parameters']['properties'])
    if set(value) - allowed: return ModuleResult.fail('INVALID_ARGUMENTS', 'Unknown knowledge arguments')
    from jsonschema import validate, ValidationError
    try: validate(value, schemas()['knowledge_' + name]['parameters'])
    except ValidationError as error: return ModuleResult.fail('INVALID_ARGUMENTS', error.message)
    base = value.get('knowledge_base_id')
    granted = {g.resource_id for g in host.slot.grants() if g.operation == inv.operation}
    if base not in granted: return ModuleResult.fail('PERMISSION_DENIED', 'Knowledge base is not bound to this operation')
    try:
        user = host.db.get(User, host.security_context.actor_user_id or host.security_context.principal_id)
        adapter = StaffDeckKnowledgeAdapter(host.db, inv.context.tenant_id, inv.context.agent_id, user, write=write)
        adapter.require_base(base, 'edit' if write else 'view')
        # Host full compatibility paths can advertise aliases; modules remain authoritative.
        import httpx
        response = httpx.get(adapter.url + '/api/km/config', headers=adapter.headers, timeout=10)
        response.raise_for_status()
        capabilities = response.json()
        if name in {'list_concepts', 'get_concept', 'upsert_concept'} and not capabilities['modules'].get('concepts'):
            return ModuleResult.fail('KNOWLEDGE_OPERATION_DISABLED', 'Concept module is disabled')
        resource = None
        for field, collection in [('document_id', 'documents'), ('bucket_id', 'buckets'), ('chunk_id', 'chunks'), ('job_id', 'jobs')]:
            if value.get(field):
                resource = adapter.call('GET', '/' + collection + '/' + quote(value[field], safe='')) if name != 'resolve_citation' else adapter.call('GET', '/citations/' + quote(value[field], safe=''))
                if resource.get('knowledge_base_id') != base: raise HTTPException(403, 'Resource is outside the bound knowledge base')
        payload = {key: value[key] for key in ['content_md', 'title', 'expected_updated_at'] if key in value}
        if name == 'import_document':
            payload = {'filename': value.get('filename', 'knowledge.md'), 'title': value['title'],
                       'content_base64': base64.b64encode(value['content_md'].encode()).decode(),
                       'space_id': adapter.space_id, **({'model_id': value['model_id']} if value.get('model_id') else {})}
        if name == 'upsert_concept':
            if value['mode'] == 'edit' and not value.get('expected_updated_at'): raise HTTPException(422, 'expected_updated_at is required')
            if value['mode'] == 'create' and value.get('expected_updated_at'): raise HTTPException(422, 'Create cannot supply a revision')
            payload.update(document_id=value['document_id'], create_only=value['mode'] == 'create')
        from string import Formatter
        for _, field, _, _ in list(Formatter().parse(path)):
            if field: path = path.replace('{' + field + '}', quote(str(value[field]), safe=''))
        params = {'expected_updated_at': value['expected_updated_at']} if name == 'delete_document' else None
        result = adapter.call(method, path, payload if method == 'POST' or method == 'PUT' else None, params=params)
        return ModuleResult.ok(result)
    except HTTPException as error:
        return ModuleResult.fail('KNOWLEDGE_HTTP_' + str(error.status_code), str(error.detail))
    except (httpx.HTTPError, ValueError, KeyError) as error:
        return ModuleResult.fail('KNOWLEDGE_UNAVAILABLE', 'Knowledge capability discovery or response is unavailable')
