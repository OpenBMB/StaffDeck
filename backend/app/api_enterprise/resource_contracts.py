"""Legacy UI spellings over the same authorized resource APIs used by releases.

Only equivalent operations are declared here. Personal installs, sharing, immutable
package versions and organizational scopes are not synonyms for staff bindings.
No database, credentials, edition switch or alternate permission policy lives here.
"""
import json
import re
from urllib.parse import quote

from fastapi import HTTPException
from starlette.responses import JSONResponse, Response


def rows(value):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError('resource list must be an array of objects')
    return value


def metadata(row):
    return row.get('metadata') if isinstance(row.get('metadata'), dict) else {}


def count(value):
    # Legacy UI numeric slots cannot contain null. Capability metadata below
    # distinguishes an untracked metric from a real measured installation count.
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def page(items, query):
    text = (query.get('q') or query.get('query') or '').strip().casefold()
    if text:
        items = [row for row in items if text in ' '.join(str(row.get(k) or '') for k in ('name', 'title', 'description')).casefold()]
    category = query.get('category') or query.get('category_group')
    if category and category != 'all':
        items = [row for row in items if category in {row.get('category'), row.get('categoryGroup')}]
    sort = query.get('sort')
    if sort in ('newest', 'createdAt:desc'):
        items = sorted(items, key=lambda r: str(r.get('createdAt') or r.get('created_at') or ''), reverse=True)
    elif sort in ('updated', 'updatedAt:desc'):
        items = sorted(items, key=lambda r: str(r.get('updatedAt') or r.get('updateTime') or r.get('updated_at') or ''), reverse=True)
    offset = int(query.get('offset', 0))
    limit = int(query.get('limit', query.get('max_results', len(items))))
    return items[offset:offset + limit], len(items)


def skill(row, *, detail=False):
    meta = metadata(row)
    return {
        'skillId': row['id'], 'scope': 'framework', 'name': row['slug'],
        'title': row['name'], 'description': row.get('description') or '',
        'plazaStatus': 'published' if row.get('status') == 'published' else 'unpublished',
        'category': meta.get('gallery_category'), 'categoryGroup': meta.get('gallery_category'),
        'ownerUserId': meta.get('owner_user_id'), 'ownerName': meta.get('owner_user_name'),
        'updateTime': row.get('updated_at'), 'createdAt': row.get('created_at'),
        'securityStatus': meta.get('security_status', 'not_checked'),
        'installCount': count(meta.get('install_count')), 'shareCount': count(meta.get('share_count')),
        # Local Markdown is not a fabricated immutable TeamHub version.
        'latestVersionId': None, 'versions': [], 'hasVersion': False,
        **({'skill_markdown': row.get('skill_markdown'), 'skill_files': row.get('skill_files', [])} if detail else {}),
        'capabilityScope': row.get('capability_scope'),
        'contract': {'canonicalPath': '/api/enterprise/general-skills/' + quote(row['slug'], safe=''),
                     'contentFormat': 'markdown', 'packageVersions': False,
                     'installationMetricsAvailable': 'install_count' in meta, 'actions': ['view']},
    }


def knowledge(row):
    meta = metadata(row)
    return {
        'id': row['id'], 'name': row['name'], 'description': row.get('description'),
        'createdAt': row.get('created_at'), 'updatedAt': row.get('updated_at'),
        'categoryId': meta.get('gallery_category'), 'ownerUserId': meta.get('owner_user_id'),
        'ownerUsername': meta.get('owner_user_name'),
        'documentCount': row.get('document_count', 0), 'chunkCount': row.get('chunk_count', 0),
        'capabilities': meta.get('capabilities', []),
        # No canEdit/canShare/claimed=true inference from role or mere visibility.
        'permissions': None, 'authorizationStatus': None,
        'contract': {'canonicalPath': '/api/enterprise/knowledge-bases/' + quote(row['id'], safe=''), 'actions': ['view']},
    }


def document(row):
    return {**row, 'kbId': row.get('knowledge_base_id'), 'fileName': row.get('filename'),
            'createdAt': row.get('created_at'), 'updatedAt': row.get('updated_at'),
            'chunkCount': row.get('chunk_count', 0)}


def resolve_common(method, path, query):
    from app.api_enterprise.compat import Plan
    from app.api_enterprise.projections import envelope, collection_page
    relevant = path.startswith(('/api/skills/', '/api/knowledge/v1/', '/api/agent-sops/', '/api/agent-tools/connectors'))
    if not relevant:
        return None
    for key in ('limit', 'offset', 'max_results'):
        if key in query:
            try:
                valid = 0 <= int(query[key]) <= 100000
            except (ValueError, TypeError):
                valid = False
            if not valid:
                raise HTTPException(422, {'code': 'INVALID_PAGINATION', 'message': f'Invalid {key}'})
    # Filters with no common semantic equivalent must not be silently dropped.
    if any(query.get(key) not in (None, '', 'false') for key in ('installed', 'inUse', 'cursor', 'accessScope', 'status')):
        return None
    if path == '/api/skills/skills' and method == 'GET':
        if query.get('view', 'plaza') != 'plaza':
            return None
        def project(value, ctx):
            items, total = page([skill(row) for row in rows(value) if row.get('status') == 'published'], ctx['query'])
            return envelope(items, total)
        return Plan(target='/api/enterprise/general-skills', key='shared_skill_list', response=project)
    match = re.fullmatch(r'/api/skills/skills/framework/([^/]+)', path)
    if match and method in {'GET', 'DELETE'}:
        return Plan(target='/api/enterprise/general-skills/' + quote(match[1], safe=''),
                    key='shared_skill', response=(lambda value, ctx: envelope(skill(value, detail=True))) if method == 'GET' else None)
    if path == '/api/agent-sops/skills/available' and method == 'GET':
        def project(value, ctx):
            items, total = page([{**row, 'frontend_contract': {'actions': ['view']}}
                                 for row in rows(value) if row.get('status') == 'published'], ctx['query'])
            result = collection_page(items)
            result['total'] = total
            return result
        return Plan(target='/api/enterprise/skills', key='shared_sop_list', response=project)
    if path.startswith('/api/agent-sops/skills'):
        suffix = path.removeprefix('/api/agent-sops/skills')
        if suffix == '' or re.fullmatch(r'/[^/]+(?:/(?:versions|publish|archive|validate))?', suffix):
            if suffix.split('/')[-1] in {'shared', 'claims', 'available'}:
                return None
            return Plan(target='/api/enterprise/skills' + suffix, key='shared_sop', tenant_in_body=method in {'POST', 'PUT', 'PATCH'})
    if path == '/api/knowledge/v1/knowledge-bases/square' and method == 'GET':
        return Plan(target='/api/enterprise/knowledge-bases', key='shared_knowledge_list',
                    response=lambda value, ctx: page([knowledge(row) for row in rows(value) if row.get('status') == 'active'], ctx['query'])[0])
    match = re.fullmatch(r'/api/knowledge/v1/knowledge-bases/([^/]+)', path)
    if match and match[1] not in {'square', 'shared', 'claimed'} and method in {'GET', 'DELETE'}:
        return Plan(target='/api/enterprise/knowledge-bases/' + quote(match[1], safe=''), key='shared_knowledge',
                    response=(lambda value, ctx: knowledge(value)) if method == 'GET' else None)
    if path == '/api/knowledge/v1/documents' and method == 'GET':
        return Plan(target='/api/enterprise/knowledge/documents', key='shared_documents', rename_query={'kbId': 'knowledge_base_id'},
                    response=lambda value, ctx: [document(row) for row in rows(value)])
    if path == '/api/agent-tools/connectors' and method == 'GET' and query.get('view', 'plaza') == 'plaza':
        return Plan(key='shared_connectors', composite=connector_catalog)
    return None


async def connector_catalog(middleware, scope, receive, send, ctx):
    """Compose already-authorized server/tool lists; never read a resource DB directly."""
    from app.api_enterprise.compat import Plan
    groups = []
    for target in ('/api/enterprise/tools', '/api/enterprise/mcp-servers'):
        inner = dict(scope)
        inner['path'] = (scope.get('root_path') or '') + target
        inner['raw_path'] = inner['path'].encode()
        inner['query_string'] = middleware._rewrite_query(ctx['query'], Plan(target=target), ctx['tenant_id']).encode()
        messages = []
        async def collect(message):
            messages.append(message)
        await middleware.app(inner, middleware._replay(b'', receive), collect)
        start = next(m for m in messages if m['type'] == 'http.response.start')
        body = b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')
        if not 200 <= start['status'] < 300:
            return await Response(body, status_code=start['status'], media_type='application/json')(scope, receive, send)
        try:
            groups.append(rows(json.loads(body)))
        except (ValueError, TypeError):
            return await JSONResponse({'detail': {'code': 'FRONTEND_RESPONSE_INVALID', 'message': '连接器列表响应格式不正确'}}, 502)(scope, receive, send)
    items = []
    for kind, group in zip(('http', 'mcp'), groups):
        for row in group:
            if not row.get('enabled', True) or kind == 'http' and (row.get('tool_type') != 'http' or row.get('mcp_server_id')):
                continue
            meta = metadata(row)
            items.append({'id': row['id'], 'kind': kind, 'name': row.get('display_name') or row.get('name'),
                          'description': row.get('description'), 'category': meta.get('gallery_category'),
                          'logoUrl': meta.get('logo_url'), 'ownerSubjectId': meta.get('owner_user_id'),
                          'connStatus': None, 'installCount': count(meta.get('install_count')),
                          'updatedAt': row.get('updated_at'), 'contract': {'actions': ['view']}})
    filtered, _ = page(items, ctx['query'])
    categories = {'__all__': len(filtered)}
    for item in filtered:
        category = item.get('category')
        if category:
            categories[category] = categories.get(category, 0) + 1
    return await JSONResponse({'items': filtered, 'categories': categories})(scope, receive, send)
