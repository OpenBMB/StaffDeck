"""BUSINESS_BASE security profile.

Delegates every decision to the Base authorization service (OpenFGA-backed on
the Business side, but StaffDeck only ever sees one provider: Base). The wire
shapes below mirror ``app.contracts.trust`` from the Business tree so a
Business deployment can drop its existing ``BaseTrustClient`` in without an
adapter.

Fail-closed rules:

- Any transport failure, contract mismatch, or timeout is a **deny**
  (``AuthorizationUnavailable`` surfaces as ``Decision.deny`` with
  ``pending=False``); there is no fallback to OSS_LOCAL.
- ``authorization_pending`` (Base has not projected a freshly created resource
  yet) is retried up to ``pending_timeout_seconds`` and then denied.
- ``filter`` uses ``batch_check`` (<=100 per call) and drops anything that is
  not an explicit allow.

Identity comes from the gateway-issued subject (Base Identity JWT already
verified upstream); workload context is minted by Base Identity's internal
token exchange.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Sequence

import httpx

from staffdeck_dsh.contracts.security import (
    Decision,
    IdentityPort,
    PepPort,
    ResourceRef,
    SecurityContext,
    SecurityProfile,
    WorkloadContextPort,
)

_RETRYABLE = {502, 503, 504}

# Base's authorization vocabulary is narrower than ours; collapse runtime verbs.
_BASE_ACTION = {
    "execute": "use", "advance": "use", "resume": "use", "receive": "use", "send": "use",
    "read": "view", "write": "edit", "delegate": "use",
}
_BASE_RESOURCE_TYPE = {
    "mcp_server": "tool", "channel": "capability", "handoff": "capability",
    "model_config": "capability", "session": "capability", "runtime": "capability",
}


@dataclass(frozen=True)
class BaseAuthzConfig:
    url: str
    decision_token: str
    control_token: str = ""
    timeout_seconds: float = 3.0
    pending_timeout_seconds: float = 3.0
    pending_poll_seconds: float = 0.25


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
        try:
            response = self.client.post(
                f"{self.base_url}{path}",
                headers={"X-Base-Service-Token": self.config.decision_token},
                json=body,
                timeout=self.config.timeout_seconds,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise _Unavailable(str(exc)) from exc
        if response.status_code in _RETRYABLE:
            raise _Unavailable(f"base authz {response.status_code}")
        if response.status_code == 409:
            raise _Pending()
        if response.status_code != 200:
            raise _Contract(f"unexpected status {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise _Contract("non-JSON body") from exc
        if not isinstance(data, dict):
            raise _Contract("body is not an object")
        return data


class _Unavailable(RuntimeError):
    pass


class _Contract(RuntimeError):
    pass


class _Pending(RuntimeError):
    pass


class BasePep(PepPort):
    profile = "BUSINESS_BASE"

    def __init__(self, client: BaseAuthzClient, *, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.client = client
        self._clock = clock
        self._sleep = sleep

    def authorize(self, ctx: SecurityContext, module: str, action: str, resource: ResourceRef) -> Decision:
        if ctx.tenant_id != resource.tenant_id:
            return Decision.deny("tenant boundary", source="BUSINESS_BASE")
        request = self._request(ctx, action, resource)
        deadline = self._clock() + self.client.config.pending_timeout_seconds
        while True:
            try:
                data = self.client.check(request)
            except _Pending:
                if self._clock() >= deadline:
                    return Decision.deny("authorization pending", source="BUSINESS_BASE", pending=True)
                self._sleep(self.client.config.pending_poll_seconds)
                continue
            except (_Unavailable, _Contract) as exc:
                return Decision.deny(f"authorization unavailable: {exc}", source="BUSINESS_BASE")
            return self._decision(data, request["request_id"])

    def filter(
        self, ctx: SecurityContext, module: str, action: str, resources: Sequence[ResourceRef]
    ) -> list[ResourceRef]:
        allowed: list[ResourceRef] = []
        items = [r for r in resources if r.tenant_id == ctx.tenant_id]
        for start in range(0, len(items), 100):
            chunk = items[start : start + 100]
            requests = [self._request(ctx, action, r) for r in chunk]
            try:
                data = self.client.batch_check(requests)
            except (_Unavailable, _Contract, _Pending):
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
        return allowed

    # -- helpers ----------------------------------------------------------------

    @staticmethod
    def _request(ctx: SecurityContext, action: str, resource: ResourceRef) -> dict[str, Any]:
        return {
            "principal": {
                "type": "user" if ctx.principal_type == "user" else "user",
                "id": ctx.principal_id,
                "tenant_id": ctx.tenant_id,
            },
            "action": _BASE_ACTION.get(action, action),
            "resource": {
                "type": _BASE_RESOURCE_TYPE.get(resource.type, resource.type),
                "id": resource.id,
                "tenant_id": resource.tenant_id,
            },
            "request_id": uuid.uuid4().hex,
            "consistency": "minimize_latency",
        }

    @staticmethod
    def _decision(data: dict[str, Any], request_id: str) -> Decision:
        if data.get("request_id") != request_id or not isinstance(data.get("allowed"), bool):
            return Decision.deny("authorization contract error", source="BUSINESS_BASE")
        return Decision(
            allowed=bool(data["allowed"]),
            reason=str(data.get("reason") or ("allowed" if data["allowed"] else "denied")),
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
    def __init__(self, config: BaseWorkloadConfig, client: httpx.Client | None = None):
        self.config = config
        self.client = client or httpx.Client(timeout=config.timeout_seconds)

    def mint(self, ctx: SecurityContext, *, audience: str, ttl_seconds: int) -> dict[str, Any]:
        response = self.client.post(
            f"{self.config.internal_url.rstrip('/')}/internal/v1/workload/token",
            json={
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "audience": audience or self.config.audience,
                "subject": ctx.principal_id,
                "tenant_id": ctx.tenant_id,
                "ttl_seconds": int(ttl_seconds),
            },
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or "token" not in data:
            raise RuntimeError("Base workload token response is malformed")
        return {"kind": "base_workload", **data}


def build_business_base_profile(
    *,
    authz: BaseAuthzConfig,
    workload: BaseWorkloadConfig | None = None,
    authz_client: BaseAuthzClient | None = None,
) -> SecurityProfile:
    if not authz.url or not authz.decision_token:
        raise ValueError("BUSINESS_BASE requires base_authz_url and base_authz_decision_token")
    pep = BasePep(authz_client or BaseAuthzClient(authz))
    wl: WorkloadContextPort
    if workload is not None:
        wl = BaseWorkload(workload)
    else:
        # A Business deployment without token exchange still must not silently
        # mint local tokens; make the gap explicit at mint time.
        class _Unconfigured(WorkloadContextPort):
            def mint(self, ctx: SecurityContext, *, audience: str, ttl_seconds: int) -> dict[str, Any]:
                raise RuntimeError("BUSINESS_BASE workload token exchange is not configured")
        wl = _Unconfigured()
    return SecurityProfile(name="BUSINESS_BASE", identity=BaseIdentity(), pep=pep, workload=wl)
