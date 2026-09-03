"""BUSINESS_BASE security profile.

Delegates decisions on *shared* resources to the Base authorization service
(OpenFGA-backed on the Business side; StaffDeck only ever sees one provider:
Base). The wire shapes below were checked against the enterprise deployment
(``staffdeck-base/services/authz-service`` and the business runtime's
``app/trust/base_client.py``), not just the local contracts module.

What Base governs and what stays local
--------------------------------------
Base only knows tenant-level shared resources: ``agent``, ``sop``, ``skill``,
``tool`` (incl. ``mcp_server``), ``knowledge_base``, ``team``. Runtime-internal
resources (``session``, ``handoff``, ``channel``, ``model_config``,
``runtime``, the synthetic ``capability`` refs used for sandbox/artifact) are
rejected by Base with 422, so they are evaluated **locally** with the same
tenant/owner/binding rules as OSS_LOCAL. The decision still carries
``source="BUSINESS_BASE"`` — the profile is the unit of policy, not the wire.

Fail-closed rules
-----------------
- Transport failure, 5xx, non-JSON, contract mismatch → **deny** with a reason
  starting with ``authorization unavailable`` (``Guard.require`` turns that into
  ``AuthorizationUnavailable``). 401/403 mean a misconfigured token and get a
  distinct reason so operators can tell config errors from outages.
- Base signals "projection not settled" as HTTP 200 with
  ``allowed=false, reason="authorization_pending"`` (never 409). That is
  retried with a short backoff until ``pending_timeout_seconds`` and then denied
  with ``pending=True``.
- ``filter`` uses ``batch_check`` (≤100 per call) and drops anything that is
  not an explicit allow.

Identity comes from the gateway-issued subject; workload context is minted via
Base Identity's client-credentials + workload-delegation + workload-session
flow (``BaseWorkload``).
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Sequence

import httpx

from staffdeck_harness.contracts.security import (
    Decision,
    IdentityPort,
    PepPort,
    ResourceRef,
    SecurityContext,
    SecurityProfile,
    WorkloadContextPort,
)

_RETRYABLE = {502, 503, 504}
PENDING_REASON = "authorization_pending"
_UNAVAILABLE_REASONS = {"openfga_unavailable", "authz_unavailable"}

# Resource types Base has a vocabulary for (shared tenant resources) …
BASE_GOVERNED_TYPES = {"agent", "sop", "skill", "general_skill", "tool", "mcp_server", "knowledge_base", "team"}
_BASE_RESOURCE_TYPE = {"mcp_server": "tool", "general_skill": "skill"}
# … runtime-internal types Base rejects with 422, evaluated with the local rules …
LOCAL_TYPES = {"session", "handoff", "channel", "model_config", "runtime", "capability", "tenant"}
# … and anything else is unknown → deny (fail closed).

# Base's action vocabulary per resource type is narrower than ours.
_BASE_ACTION = {
    "execute": "use", "advance": "use", "resume": "use", "receive": "use", "send": "use",
    "read": "view", "write": "edit", "delegate": "view",
}
# knowledge_base distinguishes can_use from can_retrieve; runtime search is a retrieval.
_TYPE_ACTION = {("knowledge_base", "use"): "retrieve"}

_ID_ALLOWED = re.compile(r"[^A-Za-z0-9._-]")


def canonical_id(value: str) -> str:
    """Base ids: ``^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$``.

    Ids are encoded injectively (each disallowed byte → ``_xx`` hex) so distinct
    StaffDeck ids can never collide on the Base side; empty ids stay empty and
    are rejected by Base (fail closed) rather than mapped to a shared name.
    """

    raw = str(value or "")
    if not raw:
        return ""
    out = _ID_ALLOWED.sub(lambda m: "_%02x" % ord(m.group(0)) if ord(m.group(0)) < 256 else "_u%04x" % ord(m.group(0)), raw)
    if not out[0].isalnum():
        out = "x" + out
    return out[:255]


@dataclass(frozen=True)
class BaseAuthzConfig:
    url: str
    decision_token: str
    control_token: str = ""
    timeout_seconds: float = 3.0
    pending_timeout_seconds: float = 3.0
    pending_poll_seconds: float = 0.05
    pending_poll_max_seconds: float = 0.25


class BaseAuthzClient:
    """Minimal fail-closed client for ``/internal/v1/authz/check`` and ``/batch-check``."""

    def __init__(self, config: BaseAuthzConfig, client: httpx.Client | None = None):
        self.config = config
        self.base_url = config.url.rstrip("/")
        self.client = client or httpx.Client(timeout=config.timeout_seconds, follow_redirects=False)

    def check(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post("/internal/v1/authz/check", payload)

    def batch_check(self, payloads: list[dict[str, Any]]) -> dict[str, Any]:
        if not payloads or len(payloads) > 100:
            raise ValueError("batch_check accepts 1..100 requests")
        return self._post("/internal/v1/authz/batch-check", {"requests": payloads})

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in (1, 2):   # one retry on transport errors / 5xx, like the business client
            try:
                response = self.client.post(
                    f"{self.base_url}{path}",
                    headers={"X-Base-Service-Token": self.config.decision_token, "X-Request-ID": uuid.uuid4().hex},
                    json=body,
                    timeout=self.config.timeout_seconds,
                    follow_redirects=False,
                )
            except httpx.HTTPError as exc:
                last_error = _Unavailable(str(exc))
                continue
            if response.status_code in _RETRYABLE:
                last_error = _Unavailable(f"base authz {response.status_code}")
                continue
            if response.status_code == 401:
                raise _Misconfigured("Base rejected the decision token (401)")
            if response.status_code == 403:
                raise _Misconfigured("credential lacks decision scope (403) — a control token was supplied where a decision token is required")
            if response.status_code != 200:
                raise _Contract(f"unexpected status {response.status_code}")
            try:
                data = response.json()
            except ValueError as exc:
                raise _Contract("non-JSON body") from exc
            if not isinstance(data, dict):
                raise _Contract("body is not an object")
            return data
        raise last_error or _Unavailable("base authz unreachable")


class _Unavailable(RuntimeError):
    pass


class _Contract(RuntimeError):
    pass


class _Misconfigured(RuntimeError):
    pass


class _Pending(RuntimeError):
    """Kept for callers/tests that model pending as an exception; Base itself answers 200 + reason."""


class BasePep(PepPort):
    profile = "BUSINESS_BASE"

    def __init__(self, client: BaseAuthzClient, *, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep, local: PepPort | None = None):
        self.client = client
        self._clock = clock
        self._sleep = sleep
        if local is None:
            from staffdeck_harness.security.oss_local import LocalPep

            local = LocalPep()
        self._local = local

    # -- PepPort ---------------------------------------------------------------

    def authorize(self, ctx: SecurityContext, module: str, action: str, resource: ResourceRef) -> Decision:
        if ctx.tenant_id != resource.tenant_id:
            return Decision.deny("tenant boundary", source="BUSINESS_BASE")
        if resource.type in LOCAL_TYPES:
            return self._local_decision(ctx, module, action, resource)
        if resource.type not in BASE_GOVERNED_TYPES:
            return Decision.deny(f"unknown resource type {resource.type!r} under BUSINESS_BASE", source="BUSINESS_BASE")
        request = self._request(ctx, action, resource)
        deadline = self._clock() + self.client.config.pending_timeout_seconds
        backoff = self.client.config.pending_poll_seconds
        while True:
            try:
                data = self.client.check(request)
            except _Pending:
                data = {"request_id": request["request_id"], "allowed": False, "reason": PENDING_REASON}
            except _Misconfigured as exc:
                return Decision.deny(f"authorization unavailable: misconfigured — {exc}", source="BUSINESS_BASE")
            except (_Unavailable, _Contract) as exc:
                return Decision.deny(f"authorization unavailable: {exc}", source="BUSINESS_BASE")
            decision = self._decision(data, request["request_id"])
            if decision.pending:
                if self._clock() >= deadline:
                    return decision
                self._sleep(backoff)
                backoff = min(backoff * 2, self.client.config.pending_poll_max_seconds)
                request = {**request, "request_id": uuid.uuid4().hex}
                continue
            return decision

    def filter(
        self, ctx: SecurityContext, module: str, action: str, resources: Sequence[ResourceRef]
    ) -> list[ResourceRef]:
        allowed: list[ResourceRef] = []
        same_tenant = [r for r in resources if r.tenant_id == ctx.tenant_id]
        local_items = [r for r in same_tenant if r.type in LOCAL_TYPES]
        allowed.extend(self._local.filter(ctx, module, action, local_items))
        items = [r for r in same_tenant if r.type in BASE_GOVERNED_TYPES]
        for start in range(0, len(items), 100):
            chunk = items[start : start + 100]
            requests = [self._request(ctx, action, r) for r in chunk]
            try:
                data = self.client.batch_check(requests)
            except (_Unavailable, _Contract, _Pending, _Misconfigured):
                # Fail closed: an unanswerable batch grants nothing.
                continue
            decisions = data.get("decisions") if isinstance(data, dict) else None
            if not isinstance(decisions, list):
                continue
            by_request = {d.get("request_id"): d for d in decisions if isinstance(d, dict)}
            for req, res in zip(requests, chunk, strict=False):
                d = by_request.get(req["request_id"])
                if isinstance(d, dict) and d.get("allowed") is True:
                    allowed.append(res)
        # keep the caller's order
        keep = {id(r) for r in allowed}
        return [r for r in resources if id(r) in keep]

    # -- helpers ----------------------------------------------------------------

    def _local_decision(self, ctx: SecurityContext, module: str, action: str, resource: ResourceRef) -> Decision:
        d = self._local.authorize(ctx, module, action, resource)
        return Decision(allowed=d.allowed, reason=f"local policy: {d.reason}", decision_id=d.decision_id, revision=d.revision, source="BUSINESS_BASE", pending=False)

    @staticmethod
    def _request(ctx: SecurityContext, action: str, resource: ResourceRef) -> dict[str, Any]:
        rtype = _BASE_RESOURCE_TYPE.get(resource.type, resource.type)
        base_action = _TYPE_ACTION.get((rtype, action)) or _BASE_ACTION.get(action, action)
        return {
            "principal": {
                "type": "user",
                "id": canonical_id(ctx.principal_id),
                "tenant_id": canonical_id(ctx.tenant_id),
            },
            "action": base_action,
            "resource": {
                "type": rtype,
                "id": canonical_id(resource.id),
                "tenant_id": canonical_id(resource.tenant_id),
            },
            "request_id": uuid.uuid4().hex,
            "consistency": "minimize_latency",
        }

    @staticmethod
    def _decision(data: dict[str, Any], request_id: str) -> Decision:
        if data.get("request_id") != request_id or not isinstance(data.get("allowed"), bool):
            return Decision.deny("authorization unavailable: contract error", source="BUSINESS_BASE")
        reason = str(data.get("reason") or "")
        if not data["allowed"] and reason == PENDING_REASON:
            return Decision.deny("authorization pending", source="BUSINESS_BASE", pending=True, decision_id=str(data.get("decision_id") or "") or None, revision=str(data.get("revision") or "") or None)
        if not data["allowed"] and reason in _UNAVAILABLE_REASONS:
            return Decision.deny(f"authorization unavailable: {reason}", source="BUSINESS_BASE")
        return Decision(
            allowed=bool(data["allowed"]),
            reason=reason or ("allowed" if data["allowed"] else "denied"),
            decision_id=str(data.get("decision_id") or "") or None,
            revision=str(data.get("revision") or "") or None,
            source="BUSINESS_BASE",
        )


class BaseIdentity(IdentityPort):
    """Subjects arrive already verified by the gateway; we only reshape them."""

    def from_user(self, user: Any, *, channel: str | None = None) -> SecurityContext:
        role = getattr(user, "tenant_role", None) or getattr(user, "role", "member")
        return SecurityContext(
            principal_id=str(getattr(user, "subject_id", None) or user.id),
            tenant_id=str(user.tenant_id),
            principal_type="user",
            tenant_role="admin" if role == "admin" else "member",
            username=getattr(user, "username", None),
            display_name=getattr(user, "display_name", None),
            provider=str(getattr(user, "provider", "base_identity")),
            session_id=getattr(user, "session_id", None),
            token_id=getattr(user, "token_id", None),
            channel=channel,
        )

    def from_service(self, service_id: str, tenant_id: str, *, workload: Any = None) -> SecurityContext:
        return SecurityContext(
            principal_id=service_id,
            tenant_id=tenant_id,
            principal_type="workload",
            tenant_role="service",
            provider="base_identity",
            workload=dict(workload or {}),
        )


@dataclass(frozen=True)
class BaseWorkloadConfig:
    internal_url: str
    client_id: str
    client_secret: str
    audience: str = "staffdeck-gateway"
    timeout_seconds: float = 3.0


class BaseWorkload(WorkloadContextPort):
    """Base Identity workload flow, as the enterprise runtime does it.

    1. ``POST /internal/v1/identity/oauth/token`` (form, client_credentials) → cached service token
    2. ``POST {authz}/internal/v1/authz/workload-delegation-check`` → decision proof
    3. ``POST /internal/v1/identity/workload-delegations`` → delegation token
    4. ``POST /internal/v1/identity/workload-sessions`` → short-lived access token for the run
    """

    DEFAULT_SCOPES = ("knowledge:retrieve", "sop:execute", "skill:package.read", "tool:config.read")

    def __init__(self, config: BaseWorkloadConfig, client: httpx.Client | None = None, *, authz: BaseAuthzClient | None = None, clock: Callable[[], float] = time.time):
        self.config = config
        self.client = client or httpx.Client(timeout=config.timeout_seconds)
        self.authz = authz
        self._clock = clock
        self._service_token: tuple[str, float] | None = None

    def service_token(self) -> str:
        now = self._clock()
        if self._service_token and self._service_token[1] > now + 30:
            return self._service_token[0]
        response = self.client.post(
            f"{self.config.internal_url.rstrip('/')}/internal/v1/identity/oauth/token",
            data={"grant_type": "client_credentials", "client_id": self.config.client_id, "client_secret": self.config.client_secret},
            headers={"X-Request-ID": uuid.uuid4().hex},
        )
        if response.status_code == 401:
            raise RuntimeError("Base identity rejected the runtime client credentials (401)")
        response.raise_for_status()
        data = response.json()
        token = data.get("access_token") if isinstance(data, dict) else None
        if not token:
            raise RuntimeError("Base identity oauth/token response is malformed")
        self._service_token = (str(token), now + float(data.get("expires_in") or 300))
        return str(token)

    def mint(self, ctx: SecurityContext, *, audience: str, ttl_seconds: int) -> dict[str, Any]:
        wl = dict(ctx.workload or {})
        run_id = str(wl.get("run_id") or ctx.session_id or uuid.uuid4().hex)
        attempt = int(wl.get("run_attempt") or 1)
        idem = str(wl.get("run_idempotency_key") or f"{ctx.tenant_id}:{run_id}:{attempt}")
        digest = str(wl.get("request_digest") or uuid.uuid4().hex)
        agent_id = str(wl.get("agent_id") or "")
        headers = {"Authorization": f"Bearer {self.service_token()}", "X-Request-ID": uuid.uuid4().hex, "Idempotency-Key": idem}
        identity = self.config.internal_url.rstrip("/")

        proof: dict[str, Any] = {}
        if self.authz is not None:
            proof = self.authz._post("/internal/v1/authz/workload-delegation-check", {
                "principal": {"type": "user", "id": canonical_id(ctx.principal_id), "tenant_id": canonical_id(ctx.tenant_id)},
                "agent_id": canonical_id(agent_id) if agent_id else None,
                "request_id": uuid.uuid4().hex,
            })
            if proof.get("allowed") is not True:
                raise RuntimeError(f"workload delegation refused: {proof.get('reason') or 'denied'}")

        delegation = self._post_json(f"{identity}/internal/v1/identity/workload-delegations", headers, {
            "runtime_client_id": self.config.client_id,
            "tenant_id": ctx.tenant_id,
            "actor_user_id": ctx.principal_id,
            "agent_id": agent_id or None,
            "authorization_decision_id": proof.get("decision_id"),
            "authorization_request_id": proof.get("request_id"),
            "authorization_revision": proof.get("revision"),
            "authorization_proof": proof.get("proof"),
            "run_idempotency_key": idem,
            "run_attempt": attempt,
            "request_digest": digest,
            "requested_scopes": list(wl.get("requested_scopes") or self.DEFAULT_SCOPES),
        })
        session = self._post_json(f"{identity}/internal/v1/identity/workload-sessions", headers, {
            "delegation_token": delegation.get("delegation_token"),
            "run_id": run_id,
            "run_idempotency_key": idem,
            "run_attempt": attempt,
            "request_digest": digest,
        })
        return {
            "kind": "base_workload",
            "audience": audience or self.config.audience,
            "delegation_id": delegation.get("delegation_id"),
            "session_id": session.get("session_id"),
            "access_token": session.get("access_token"),
            "token_type": session.get("token_type", "Bearer"),
            "expires_in": session.get("expires_in"),
            "scope": session.get("scope"),
        }

    def _post_json(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post(url, json=body, headers=headers)
        if response.status_code not in (200, 201):
            raise RuntimeError(f"Base identity {url.rsplit('/', 1)[-1]} failed with {response.status_code}")
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("Base identity response is malformed")
        return data


def build_business_base_profile(
    *,
    authz: BaseAuthzConfig,
    workload: BaseWorkloadConfig | None = None,
    authz_client: BaseAuthzClient | None = None,
) -> SecurityProfile:
    if not authz.url or not authz.decision_token:
        raise ValueError("BUSINESS_BASE requires base_authz_url and base_authz_decision_token")
    client = authz_client or BaseAuthzClient(authz)
    pep = BasePep(client)
    wl: WorkloadContextPort
    if workload is not None:
        wl = BaseWorkload(workload, authz=client)
    else:
        # A Business deployment without token exchange still must not silently
        # mint local tokens; make the gap explicit at mint time.
        class _Unconfigured(WorkloadContextPort):
            def mint(self, ctx: SecurityContext, *, audience: str, ttl_seconds: int) -> dict[str, Any]:
                raise RuntimeError("BUSINESS_BASE workload token exchange is not configured")
        wl = _Unconfigured()
    return SecurityProfile(name="BUSINESS_BASE", identity=BaseIdentity(), pep=pep, workload=wl)
