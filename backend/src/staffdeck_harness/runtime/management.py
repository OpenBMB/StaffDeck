"""Existing resource routes can be owned by modules without forwarding execution to another app."""
from dataclasses import dataclass
from typing import Any
import re
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
from staffdeck_harness.contracts.manifest import SlotName
from staffdeck_harness.contracts.runtime_services import StreamingServiceResponse


class ManagedStream(StreamingResponse):
    def __init__(self, reply, release):
        self.reply, self.release = reply, release
        super().__init__(reply.body, status_code=reply.status,
                         headers={**reply.headers, "cache-control": "no-cache, no-transform", "x-accel-buffering": "no"})

    async def __call__(self, scope, receive, send):
        import anyio
        async def observed_receive():
            message = await receive()
            if message["type"] == "http.disconnect":
                with anyio.CancelScope(shield=True):
                    await run_in_threadpool(self.reply.close)
            return message
        try:
            await super().__call__(scope, observed_receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await run_in_threadpool(self.reply.close)
                finally:
                    self.release()


@dataclass(frozen=True)
class ManagementRequest:
    method: str
    path: str
    query: tuple[tuple[str, str], ...]
    body: bytes
    headers: dict[str, str]
    subject: Any
    operation_id: str | None = None


def is_shared_management_route(path: str, method: str | None = None) -> bool:
    """Execution/history belongs to the shared Runtime, never a resource CRUD proxy."""
    path = path.rstrip("/")
    from staffdeck_harness.contracts.directory import DIRECTORY_ALIASES
    return bool(
        re.fullmatch(r'/api/chat/agents/[^/]+/avatar/[a-f0-9]{64}', path)
        or path in {'/api/enterprise/ui-config', '/api/chat/ui-config'}
        or (method == 'GET' and path in {'/api/enterprise/agents', '/api/chat/agents', '/api/enterprise/agent-scope', *DIRECTORY_ALIASES})
        or re.fullmatch(r"/api/enterprise/tools/[^/]+/test", path)
        or path.startswith("/api/enterprise/external-business-tasks/")
        or re.fullmatch(r"/api/enterprise/agents/[^/]+/work-record", path)
        or re.fullmatch(r"/api/enterprise/agents/[^/]+/evolution(?:/.*|:.*)", path)
        or re.fullmatch(r"/api/enterprise/agents/[^/]+/api-credentials(?:/[^/]+/(?:rotate|revoke))?", path)
        or re.fullmatch(r"/api/enterprise/general-skills/[^/]+/run(?:/stream)?", path)
        or (path.startswith("/api/chat/") and not path.startswith(("/api/chat/agents", "/api/chat/ui-config")))
    )


class ModuleManagementMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        root_path = scope.get("root_path", "")
        if root_path and path.startswith(root_path + "/"):
            path = path[len(root_path):]
        from staffdeck_harness.modules.registry import peek_registry
        registry = peek_registry()
        from staffdeck_harness.runtime.management_contracts import resolve_operation
        operation = resolve_operation(scope['method'], path)
        if is_shared_management_route(path, scope['method']):
            return await self.app(scope, receive, send)
        candidates = [item for item in registry.providers(SlotName.MODULE_MANAGEMENT)
                      if operation and item.manifest.metadata.get('management_domain') == operation.domain] if registry else []
        if not candidates:
            claimed = [prefix for item in registry.installed() if item.enabled
                       for prefix in item.manifest.metadata.get("managed_route_prefixes", ())] if registry else []
            if any(path == prefix or path.startswith(prefix + "/") for prefix in claimed):
                return await JSONResponse({"detail": "该来源对应的资源管理模块未启用"}, 503)(scope, receive, send)
            return await self.app(scope, receive, send)
        if len(candidates) != 1:
            return await JSONResponse({"detail": "资源管理模块装配冲突"}, 503)(scope, receive, send)
        request = Request(scope, receive)
        from fastapi import HTTPException
        try:
            from staffdeck_harness.runtime.control_auth import provider, project_subject
            from staffdeck_harness.runtime.services import bind_authenticated_session
            from app.db import engine
            from sqlmodel import Session
            control = provider()
            authorization = request.headers.get("authorization", "")
            if control is None or not authorization.startswith("Bearer "):
                raise HTTPException(401, "Not authenticated")
            subject = await run_in_threadpool(control.current, authorization[7:])
            body = await request.body()
            if len(body) > 32 * 1024 * 1024:
                raise HTTPException(413, "Resource upload exceeds the supported size")
            headers = {key: request.headers[key] for key in ("authorization", "content-type", "accept", "idempotency-key", "last-event-id") if key in request.headers}
            command = ManagementRequest(request.method, path, tuple(request.query_params.multi_items()), body, headers, subject, operation.id)
            operation.validate_request(command)
            def invoke():
                lease = registry.turn_lease()
                lease.__enter__()
                try:
                    with Session(engine) as db:
                        project_subject(db, subject)
                        bind_authenticated_session(db)
                        reply = candidates[0].provider.handle(command, db)
                        reply = operation.validate_response(reply)
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
                    try:
                        await run_in_threadpool(reply.close)
                    finally:
                        release()
                    raise
            else:
                if reply.status >= 500:
                    import logging
                    module_id = candidates[0].manifest.module_id
                    logging.getLogger(__name__).error("resource management upstream failed module=%s path=%s status=%s",
                                                      module_id, path, reply.status)
                    response = JSONResponse({"detail": {"code": "RESOURCE_SERVICE_UNAVAILABLE",
                        "message": "资源管理服务暂不可用", "module_id": module_id, "upstream_status": reply.status}}, 503)
                else:
                    response = Response(reply.body, status_code=reply.status, headers=dict(reply.headers))
        except HTTPException as exc:
            response = JSONResponse({"detail": exc.detail}, exc.status_code)
        except Exception as exc:
            from staffdeck_harness.contracts.errors import ModuleSdkError
            if isinstance(exc, ModuleSdkError):
                response = JSONResponse({"detail": exc.to_dict()}, 400)
            else:
                import logging
                logging.getLogger(__name__).exception("resource management module failed")
                response = JSONResponse({"detail": "资源管理服务暂不可用"}, 503)
        return await response(scope, receive, send)
