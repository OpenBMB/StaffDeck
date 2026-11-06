"""Frontend resource contracts belong to the selected management module.

Enterprise resources retain native pagination, authorization and lifecycle semantics.
Common Runtime operations use existing local routes; optional OSS spelling adapters
never manufacture successful empty catalogs for missing enterprise capabilities.
Request replay preserves the real receive/disconnect channel. SSE is not buffered.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl, urlencode

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.api_enterprise import projections as P
from app.api_enterprise.projections import Actor

logger = logging.getLogger("staffdeck.business_compat")

_ACTOR_TTL_SECONDS = 60
_MAX_BUFFERED_BODY = 32 * 1024 * 1024

NOT_AVAILABLE = "当前后端未提供此功能"


@dataclass
class Plan:
    """What to do with one business request."""

    target: str | None = None  # framework path (relative to root_path)
    key: str = "passthrough"
    method: str | None = None  # method override
    add_query: dict[str, str] = field(default_factory=dict)
    drop_query: tuple[str, ...] = ()
    rename_query: dict[str, str] = field(default_factory=dict)
    stub: Any = None  # synthetic JSON answered without touching downstream
    stub_status: int = 200
    request: Callable[[Any, dict[str, Any]], Any] | None = None
    response: Callable[[Any, dict[str, Any]], Any] | None = None
    multipart_to_json: Callable[[dict[str, Any], dict[str, Any]], Any] | None = None
    composite: Callable[["BusinessContractMiddleware", dict, Callable, Callable, dict[str, Any]], Awaitable[None]] | None = None
    tenant_in_query: bool = True
    tenant_in_body: bool = False


_JSON_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

_AGENTS = "/api/agent-control-plane/agents"
_SOPS = "/api/agent-sops/skills"
_TOOLS = "/api/agent-tools"
_PLATFORM = "/api/agent-platform"
_KNOWLEDGE = "/api/knowledge/v1"
_SKILLS = "/api/skills/skills"

_PLATFORM_ALIASES = {
    "/chat": "/api/chat",
    "/sessions": "/api/enterprise/sessions",
    "/feedback": "/api/enterprise/feedback",
    "/scheduled-tasks": "/api/enterprise/scheduled-tasks",
    "/memories": "/api/enterprise/memories",
    "/traces": "/api/enterprise/traces",
}
_CONTROL_PLANE_ALIASES = {
    "/api/agent-control-plane/agent-scope": "/api/enterprise/agent-scope",
    "/api/agent-control-plane/model-configs": "/api/enterprise/model-configs",
    "/api/agent-control-plane/persona": "/api/enterprise/persona",
    "/api/agent-control-plane/ui-config": "/api/enterprise/ui-config",
    "/api/agent-control-plane/chat/agents": "/api/chat/agents",
    "/api/agent-control-plane/chat/ui-config": "/api/chat/ui-config",
}


def _split(prefix: str, rel: str) -> str | None:
    if rel == prefix:
        return ""
    if rel.startswith(prefix + "/"):
        return rel[len(prefix):]
    return None


def _segments(suffix: str) -> list[str]:
    return [item for item in suffix.split("/") if item]


def _agent_out(body: Any, ctx: dict[str, Any]) -> Any:
    return P.agent_to_business(body, ctx.get("actor"))


def _agent_in(body: Any, ctx: dict[str, Any]) -> Any:
    return P.agent_write_to_oss(body, ctx.get("tenant_id"))


def _tenant_in(body: Any, ctx: dict[str, Any]) -> Any:
    return P.with_tenant(body, ctx.get("tenant_id"))


def resolve(method: str, rel: str, query: dict[str, str]) -> Plan | None:  # noqa: C901 - one branch per prefix
    """Map a business SPA request onto the framework. ``None`` means "not ours"."""
    method = method.upper()

    # ---- digital employees -------------------------------------------------
    suffix = _split(_AGENTS, rel)
    if suffix is not None:
        parts = _segments(suffix)
        if parts[:1] == ["shared-with-me"]:
            return Plan(stub=[])
        if len(parts) == 2 and parts[1] == "shares":
            if method == "GET":
                return Plan(stub=P.agent_shares_page(parts[0]))
            return Plan(stub={"detail": NOT_AVAILABLE}, stub_status=501)
        if len(parts) == 2 and parts[1] == "tenant-public":
            if method == "DELETE":
                return Plan(target=f"/api/enterprise/agents/{parts[0]}/gallery:unpublish", method="POST", key="agents", response=_agent_out)
            if method == "POST":
                return Plan(stub={"detail": NOT_AVAILABLE}, stub_status=501)
            return Plan(stub={"detail": NOT_AVAILABLE}, stub_status=501)
        if len(parts) == 2 and parts[1] == "resources":
            return Plan(
                target="/api/enterprise/agents" + suffix,
                key="agents",
                request=_agent_in,
                response=lambda body, ctx: P.resources_to_business(body),
            )
        return Plan(
            target="/api/enterprise/agents" + suffix,
            key="agents",
            request=_agent_in if method in {"POST", "PUT"} else None,
            response=_agent_out,
        )

    for business, framework in _CONTROL_PLANE_ALIASES.items():
        suffix = _split(business, rel)
        if suffix is not None:
            response = _agent_out if business.endswith("/chat/agents") else None
            return Plan(target=framework + suffix, key="control_plane", response=response, tenant_in_body=method in _JSON_METHODS)

    # ---- chat / operations -------------------------------------------------
    suffix = _split(_PLATFORM, rel)
    if suffix is not None:
        for business, framework in _PLATFORM_ALIASES.items():
            rest = _split(business, suffix)
            if rest is None:
                continue
            if business == "/chat" and rest.endswith("/handoffs") and method == "POST":
                return Plan(stub={"detail": NOT_AVAILABLE}, stub_status=501)
            return Plan(target=framework + rest, key="platform", tenant_in_body=method in _JSON_METHODS)
        return None

    # ---- SOPs --------------------------------------------------------------
    suffix = _split(_SOPS, rel)
    if suffix is not None:
        parts = _segments(suffix)
        if parts[:1] == ["available"] and method == "GET":
            return Plan(target="/api/enterprise/skills", key="sops", drop_query=("max_results",), response=lambda body, ctx: P.sops_available_page(body))
        if parts[:1] in (["shared"], ["claims"]) and method == "GET":
            return Plan(stub=P.collection_page([]))
        if len(parts) == 2 and parts[1] in {"claim", "shares", "tenant-public"}:
            if method == "GET":
                return Plan(stub=P.collection_page([]))
            return Plan(stub={"detail": NOT_AVAILABLE}, stub_status=501)
        return Plan(target="/api/enterprise/skills" + suffix, key="sops", tenant_in_body=method in _JSON_METHODS)

    # ---- tools / MCP / connectors -----------------------------------------
    suffix = _split(_TOOLS, rel)
    if suffix is not None:
        if suffix.startswith("/connectors"):
            if method == "GET" and suffix == "/connectors":
                return Plan(stub={"items": [], "categories": {"__all__": 0}})
            return Plan(stub={"detail": NOT_AVAILABLE}, stub_status=501)
        if suffix.startswith("/tools") or suffix.startswith("/mcp-servers"):
            return Plan(target="/api/enterprise" + suffix, key="tools", tenant_in_body=method in _JSON_METHODS)
        return None

    # ---- admin / organization ---------------------------------------------
    if rel == "/api/admin/chat-models":
        return Plan(target="/api/enterprise/model-configs", key="chat_models", response=lambda body, ctx: P.model_configs_to_chat_models(body))
    if rel == "/api/admin/plaza-categories":
        return Plan(stub={"items": []})
    if rel == "/api/organization/tree":
        return Plan(stub={"detail": NOT_AVAILABLE}, stub_status=501)
    if rel == "/api/organization/virtual-organizations":
        return Plan(stub=[])

    # Enterprise-only contracts have no OSS fallback.
    if rel.startswith(("/api/skills/", "/api/knowledge/v1/", "/api/organization/")):
        return Plan(stub={"detail": NOT_AVAILABLE}, stub_status=501)
    return None


# --------------------------------------------------------------------------- actor resolution

_actor_cache: dict[str, tuple[float, Actor]] = {}
_actor_lock = threading.Lock()


def _resolve_actor(token: str) -> Actor | None:
    """Who is calling, from the bearer token. Cached briefly; never trusted beyond the TTL."""
    key = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = time.monotonic()
    with _actor_lock:
        cached = _actor_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
    actor = _actor_from_local_token(token)
    if actor is None:
        actor = _actor_from_control_provider(token)
    if actor is not None:
        with _actor_lock:
            if len(_actor_cache) > 4096:
                _actor_cache.clear()
            _actor_cache[key] = (now + _ACTOR_TTL_SECONDS, actor)
    return actor


def _actor_from_local_token(token: str) -> Actor | None:
    from fastapi import HTTPException

    from app.security.auth import _decode_token

    try:
        payload = _decode_token(token)
    except HTTPException:
        return None
    user_id, tenant_id = payload.get("user_id"), payload.get("tenant_id")
    if not user_id or not tenant_id:
        return None
    role = "member"
    try:
        from sqlmodel import Session

        from app.db import engine
        from app.db.models import User

        with Session(engine) as db:
            row = db.get(User, user_id)
            if row is None or row.tenant_id != tenant_id:
                return None
            role = row.role or role
    except Exception:  # noqa: BLE001 - role only refines allowed_actions
        logger.debug("business-compat: could not load user role", exc_info=True)
    return Actor(str(user_id), str(tenant_id), str(role))


def _actor_from_control_provider(token: str) -> Actor | None:
    try:
        from staffdeck_harness.runtime.control_auth import provider
    except ImportError:
        return None
    control = provider()
    if control is None:
        return None
    try:
        subject = control.current(token)
    except Exception:  # noqa: BLE001 - downstream route reports the auth failure
        return None
    user_id = getattr(subject, "user_id", None)
    tenant_id = getattr(subject, "tenant_id", None)
    if not user_id or not tenant_id:
        return None
    return Actor(str(user_id), str(tenant_id), str(getattr(subject, "role", "member") or "member"))


# --------------------------------------------------------------------------- middleware


class BusinessContractMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        root = scope.get("root_path", "") or ""
        path = scope.get("path", "")
        rel = path[len(root):] if root and path.startswith(root + "/") else path
        from staffdeck_harness.runtime.frontend_management import dispatch, frontend_domain, shared_target
        if await dispatch(scope, receive, send, rel):
            return
        query = dict(parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True))
        from fastapi import HTTPException
        from app.api_enterprise.resource_contracts import resolve_common
        try:
            common = resolve_common(scope['method'], rel, query)
        except HTTPException as exc:
            return await JSONResponse({'detail': exc.detail}, exc.status_code)(scope, receive, send)
        # Enterprise-only resource contracts are owned by an installed provider.
        # An OSS deployment must not synthesize empty catalogs, permissions,
        # versions or lifecycles in their place.
        domain = frontend_domain(rel)
        special = (domain in {'knowledge', 'skill', 'sop'} or rel.startswith('/api/organization')
                   or rel.startswith('/api/agent-tools/connectors')
                   or rel == '/api/admin/plaza-categories')
        if special and common is None:
            return await JSONResponse({'detail': {'code': 'FEATURE_UNAVAILABLE', 'message': NOT_AVAILABLE}}, 501)(scope, receive, send)
        plan = common or resolve(scope["method"], rel, query)
        runtime_path = shared_target(rel, scope['method'])
        if runtime_path:
            plan = Plan(target=runtime_path, tenant_in_body=scope['method'] in _JSON_METHODS)
            from staffdeck_harness.contracts.directory import DIRECTORY_ALIASES
            if runtime_path == '/api/enterprise/agents' or runtime_path in DIRECTORY_ALIASES:
                plan.response = _agent_out
                if runtime_path == '/api/enterprise/agents':
                    plan.add_query['scope'] = 'mine'
        if plan is None:
            return await self.app(scope, receive, send)
        if plan.stub is not None or (plan.target is None and plan.composite is None):
            return await JSONResponse({'detail': {'code': 'FEATURE_UNAVAILABLE', 'message': NOT_AVAILABLE}}, 501)(scope, receive, send)

        actor = await self._actor(scope)
        tenant_id = query.get("tenant_id") or (actor.tenant_id if actor else None)
        ctx: dict[str, Any] = {"actor": actor, "tenant_id": tenant_id, "query": query, "rel": rel, "method": scope["method"]}
        if plan.composite is not None:
            return await plan.composite(self, scope, receive, send, ctx)

        new_scope = dict(scope)
        new_scope["path"] = root + plan.target
        new_scope["raw_path"] = new_scope["path"].encode("utf-8")
        if plan.method:
            new_scope["method"] = plan.method
        new_scope["query_string"] = self._rewrite_query(query, plan, tenant_id).encode("latin-1")
        new_scope["state"] = {**(scope.get("state") or {}), "business_contract": plan.key}

        headers = Headers(scope=scope)
        content_type = headers.get("content-type", "")
        if plan.multipart_to_json is not None and content_type.startswith("multipart/form-data"):
            form = await self._read_form(scope, receive)
            payload = plan.multipart_to_json(form, ctx)
            receive = self._replay(json.dumps(payload, ensure_ascii=False).encode("utf-8"), receive)
            new_scope["headers"] = self._replace_header(scope["headers"], b"content-type", b"application/json")
            ctx["request_body"] = payload
        elif plan.request is not None or plan.tenant_in_body:
            raw = await self._read_body(receive)
            payload: Any = None
            if raw and "application/json" in content_type:
                try:
                    payload = json.loads(raw)
                except ValueError:
                    payload = None
            if plan.request is not None or isinstance(payload, dict):
                if raw or plan.request is not None:
                    transformed = plan.request(payload, ctx) if plan.request is not None else P.with_tenant(payload, tenant_id)
                    ctx["request_body"] = payload
                    raw = json.dumps(transformed, ensure_ascii=False).encode("utf-8")
                    new_scope["headers"] = self._replace_header(scope["headers"], b"content-type", b"application/json")
            receive = self._replay(raw, receive)

        if plan.response is None:
            return await self.app(new_scope, receive, send)
        return await self.app(new_scope, receive, self._projecting_send(send, plan, ctx))

    # ---- helpers ---------------------------------------------------------

    async def _actor(self, scope) -> Actor | None:
        authorization = Headers(scope=scope).get("authorization", "")
        if not authorization.lower().startswith("bearer "):
            return None
        token = authorization[7:].strip()
        if not token:
            return None
        return await run_in_threadpool(_resolve_actor, token)

    @staticmethod
    def _rewrite_query(query: dict[str, str], plan: Plan, tenant_id: str | None) -> str:
        pairs: list[tuple[str, str]] = []
        for key, value in query.items():
            if key in plan.drop_query:
                continue
            pairs.append((plan.rename_query.get(key, key), value))
        keys = {key for key, _ in pairs}
        if plan.tenant_in_query and tenant_id and "tenant_id" not in keys:
            pairs.append(("tenant_id", tenant_id))
        for key, value in plan.add_query.items():
            if key not in keys:
                pairs.append((key, value))
        return urlencode(pairs)

    @staticmethod
    def _replace_header(headers: list[tuple[bytes, bytes]], name: bytes, value: bytes) -> list[tuple[bytes, bytes]]:
        kept = [(key, val) for key, val in headers if key.lower() not in {name, b"content-length"}]
        return kept + [(name, value)]

    @staticmethod
    async def _read_body(receive) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                total += len(body)
                if total > _MAX_BUFFERED_BODY:
                    raise ValueError("request body too large for business compat layer")
                chunks.append(body)
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                break
        return b"".join(chunks)

    async def _read_json(self, receive) -> Any:
        raw = await self._read_body(receive)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    async def _read_form(self, scope, receive) -> dict[str, Any]:
        request = Request(scope, receive)
        form = await request.form()
        out: dict[str, Any] = {}
        for key, value in form.multi_items():
            if hasattr(value, "read"):
                out[key] = value
                out["_file_bytes"] = await value.read()
                out.setdefault("filename", getattr(value, "filename", None))
            else:
                out[key] = value
        return out

    @staticmethod
    def _replay(body: bytes, original_receive):
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return await original_receive()
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        return receive

    @staticmethod
    def _projecting_send(send, plan: Plan, ctx: dict[str, Any]):
        state: dict[str, Any] = {"start": None, "buffer": [], "buffering": False}

        async def wrapped(message):
            if message["type"] == "http.response.start":
                headers = Headers(raw=message.get("headers", []))
                media = headers.get("content-type", "").split(";", 1)[0].strip().lower()
                state["start"] = message
                state["buffering"] = media == "application/json" and 200 <= message["status"] < 300
                if not state["buffering"]:
                    await send(message)
                return
            if message["type"] != "http.response.body":
                return await send(message)
            if not state["buffering"]:
                return await send(message)
            state["buffer"].append(message.get("body", b""))
            if message.get("more_body", False):
                return
            raw = b"".join(state["buffer"])
            start = state["start"]
            try:
                parsed = json.loads(raw) if raw else None
                projected = plan.response(parsed, ctx)
            except Exception:
                logger.warning("business-compat: projection failed for %s", ctx.get("rel"), exc_info=True)
                body = json.dumps({'detail': {'code': 'FRONTEND_RESPONSE_INVALID',
                    'message': '接口响应不符合当前界面契约，请查询资源确认结果，勿重复提交'}}).encode()
                await send({'type': 'http.response.start', 'status': 502,
                            'headers': [(b'content-type', b'application/json'), (b'content-length', str(len(body)).encode())]})
                await send({'type': 'http.response.body', 'body': body, 'more_body': False})
                return
            if isinstance(projected, Response):
                status, body = projected.status_code, projected.body
                headers = [(key.encode("latin-1"), value.encode("latin-1")) for key, value in projected.headers.items() if key.lower() != "content-length"]
                headers.append((b"content-length", str(len(body)).encode()))
                await send({"type": "http.response.start", "status": status, "headers": headers})
                await send({"type": "http.response.body", "body": body, "more_body": False})
                return
            body = b"" if projected is None and raw == b"" else json.dumps(projected, ensure_ascii=False).encode("utf-8")
            headers = [(key, value) for key, value in start.get("headers", []) if key.lower() != b"content-length"]
            headers.append((b"content-length", str(len(body)).encode()))
            await send({"type": "http.response.start", "status": start["status"], "headers": headers})
            await send({"type": "http.response.body", "body": body, "more_body": False})

        return wrapped

from starlette.datastructures import Headers  # noqa: E402 - keep the dataclass block readable above
