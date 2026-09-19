"""Optional native resource-management surfaces owned by the selected modules.

Runtime execution/history never leave this process. No enterprise URLs or credentials
are known here; adapters expose only a declared domain and verified user requests.
"""
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from staffdeck_harness.contracts.manifest import SlotName
from staffdeck_harness.contracts.runtime_services import StreamingServiceResponse
from staffdeck_harness.runtime.management import ManagementRequest, ManagedStream


def frontend_domain(path):
    prefixes = {
        'staff': ('/api/agent-control-plane', '/api/admin/chat-models', '/api/admin/plaza-categories', '/api/organization'),
        'sop': ('/api/agent-sops',), 'tool': ('/api/agent-tools',),
        'skill': ('/api/skills',), 'knowledge': ('/api/knowledge/v1',),
    }
    return next((domain for domain, paths in prefixes.items()
                 if any(path == prefix or path.startswith(prefix + '/') for prefix in paths)), None)


def shared_target(path, method):
    from staffdeck_harness.runtime.management import is_shared_management_route
    if path == '/api/agent-control-plane/chat/ui-config':
        return '/api/chat/ui-config'
    for external, internal in (
        ('/api/agent-control-plane', '/api/enterprise'),
        ('/api/agent-tools', '/api/enterprise'),
    ):
        if path == external or path.startswith(external + '/'):
            target = internal + path[len(external):]
            # All directory views share the selected Staff source and Runtime
            # statistics, including aliases absent from an older enterprise CP.
            if is_shared_management_route(target, method):
                return target
    return None


async def dispatch(scope, receive, send, path):
    """Return True only when an enabled native management provider handled the request."""
    domain = frontend_domain(path)
    if not domain or shared_target(path, scope['method']):
        return False
    from fastapi import HTTPException
    from staffdeck_harness.modules.registry import peek_registry
    from staffdeck_harness.runtime.control_auth import provider

    registry = peek_registry()
    candidates = [item for item in registry.providers(SlotName.MODULE_MANAGEMENT)
                  if item.manifest.metadata.get('management_domain') == domain] if registry else []
    if not candidates:
        return False
    try:
        if len(candidates) != 1:
            raise HTTPException(503, {'code': 'FRONTEND_PROVIDER_CONFLICT', 'message': '资源管理模块装配冲突'})
        handler = getattr(candidates[0].provider, 'handle_frontend', None)
        if not callable(handler):
            raise HTTPException(503, {'code': 'FRONTEND_CONTRACT_UNAVAILABLE', 'message': '当前管理模块尚未提供此接口契约'})
        request = Request(scope, receive)
        control = provider()
        authorization = request.headers.get('authorization', '')
        if control is None or not authorization.startswith('Bearer '):
            raise HTTPException(401, 'Not authenticated')
        subject = await run_in_threadpool(control.current, authorization[7:])
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 32 * 1024 * 1024:
                raise HTTPException(413, {'code': 'REQUEST_TOO_LARGE', 'message': '上传内容超过当前接口限制'})
        headers = {key: request.headers[key] for key in
                   ('authorization', 'content-type', 'accept', 'idempotency-key', 'last-event-id', 'if-match', 'if-none-match', 'range')
                   if key in request.headers}
        command = ManagementRequest(request.method, path, tuple(request.query_params.multi_items()), bytes(body), headers, subject,
                                    'frontend.' + domain + '/v1')
        def invoke():
            lease = registry.turn_lease()
            lease.__enter__()
            try:
                reply = handler(command)
                if isinstance(reply, StreamingServiceResponse):
                    return reply, lambda: lease.__exit__(None, None, None)
                lease.__exit__(None, None, None)
                return reply, None
            except BaseException:
                lease.__exit__(None, None, None)
                raise
        reply, release = await run_in_threadpool(invoke)
        if release:
            try:
                response = ManagedStream(reply, release)
            except BaseException:
                reply.close()
                release()
                raise
        else:
            response = Response(reply.body, status_code=reply.status, headers=dict(reply.headers))
    except HTTPException as exc:
        response = JSONResponse({'detail': exc.detail}, exc.status_code)
    await response(scope, receive, send)
    return True
