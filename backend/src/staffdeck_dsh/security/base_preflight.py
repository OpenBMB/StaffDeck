"""Side-effect-free connection test for the enterprise permission centre (Base).

Used by the admin "测试连接" button and by the restart preflight, so an operator
never restarts into BUSINESS_BASE with a URL or token that cannot work. Every
call here is a read on Base:

- ``GET /health`` / ``GET /ready``            (unauthenticated liveness)
- ``POST /internal/v1/authz/agents/public/list``  (decision-scope read; proves the decision token)
- ``POST /internal/v1/identity/oauth/token``  (client credentials; only if identity is configured)
- ``POST /internal/v1/authz/check``           (optional: is *this* admin known to Base at all)
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

import httpx

from staffdeck_dsh.modules.config import BaseConnection
from staffdeck_dsh.security.business_base import canonical_id


@dataclass
class PreflightCheck:
    name: str
    ok: bool | None           # None = skipped / informational
    message: str
    status: int | None = None
    latency_ms: float | None = None
    fatal: bool = True
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class PreflightReport:
    ok: bool
    checks: list[PreflightCheck]
    authz_revision: str | None = None
    tested_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": [asdict(c) for c in self.checks], "authz_revision": self.authz_revision, "tested_at": self.tested_at}

    def first_failure(self) -> str | None:
        for c in self.checks:
            if c.fatal and c.ok is False:
                return c.message
        return None


def _new_client(timeout: float) -> httpx.Client:
    """Factory (patched in tests)."""

    return httpx.Client(timeout=timeout, follow_redirects=False)


def _timed(fn):
    started = perf_counter()
    try:
        return fn(), round((perf_counter() - started) * 1000, 1), None
    except httpx.TimeoutException as exc:
        return None, round((perf_counter() - started) * 1000, 1), f"连接超时（{exc.__class__.__name__}）"
    except httpx.HTTPError as exc:
        return None, round((perf_counter() - started) * 1000, 1), f"无法连接（{exc.__class__.__name__}: {exc}）"


def preflight_base(conn: BaseConnection, *, tenant_id: str | None = None, principal_id: str | None = None, client: httpx.Client | None = None) -> PreflightReport:
    checks: list[PreflightCheck] = []
    url = (conn.authz_url or "").rstrip("/")
    timeout = float(conn.timeout_seconds or 3.0)
    http = client or _new_client(timeout)
    revision: str | None = None

    if not url or not url.startswith(("http://", "https://")):
        checks.append(PreflightCheck("权限中心地址", False, "权限中心地址未填写或不是 http(s) 地址"))
        return PreflightReport(ok=False, checks=checks, tested_at=_now())
    checks.append(PreflightCheck("权限中心地址", True, url))
    if not conn.decision_token:
        checks.append(PreflightCheck("决策令牌", False, "决策令牌未填写"))
        return PreflightReport(ok=False, checks=checks, tested_at=_now())

    # 1. liveness
    resp, ms, err = _timed(lambda: http.get(f"{url}/health"))
    if err or resp is None:
        checks.append(PreflightCheck("权限中心健康检查", False, f"权限中心 {url} {err}", latency_ms=ms))
        return PreflightReport(ok=False, checks=checks, tested_at=_now())
    body = _json(resp)
    if resp.status_code == 200 and (not isinstance(body, dict) or body.get("status") in (None, "ok")):
        checks.append(PreflightCheck("权限中心健康检查", True, f"服务正常（{body.get('service') if isinstance(body, dict) and body.get('service') else 'health ok'}）", status=200, latency_ms=ms))
    else:
        checks.append(PreflightCheck("权限中心健康检查", False, f"健康检查返回 {resp.status_code}", status=resp.status_code, latency_ms=ms))
        return PreflightReport(ok=False, checks=checks, tested_at=_now())

    # 2. readiness (non-fatal — some gateways do not expose it)
    resp, ms, err = _timed(lambda: http.get(f"{url}/ready"))
    if err or resp is None:
        checks.append(PreflightCheck("权限中心就绪检查", None, f"未能检查（{err}）", latency_ms=ms, fatal=False))
    elif resp.status_code == 200:
        checks.append(PreflightCheck("权限中心就绪检查", True, "已就绪", status=200, latency_ms=ms, fatal=False))
    elif resp.status_code == 404:
        checks.append(PreflightCheck("权限中心就绪检查", None, "该部署未提供 /ready，跳过", status=404, latency_ms=ms, fatal=False))
    else:
        checks.append(PreflightCheck("权限中心就绪检查", False, f"尚未就绪（{resp.status_code}）", status=resp.status_code, latency_ms=ms, fatal=False))

    # 3. decision token — a read-only decision-scope endpoint
    probe_tenant = canonical_id(tenant_id or "preflight")
    resp, ms, err = _timed(lambda: http.post(
        f"{url}/internal/v1/authz/agents/public/list",
        json={"tenant_id": probe_tenant, "request_id": f"preflight-{uuid.uuid4().hex}"},
        headers={"X-Base-Service-Token": conn.decision_token, "X-Request-ID": uuid.uuid4().hex},
    ))
    if err or resp is None:
        checks.append(PreflightCheck("决策令牌", False, f"权限中心 {err}", latency_ms=ms))
        return PreflightReport(ok=False, checks=checks, tested_at=_now())
    body = _json(resp)
    if resp.status_code == 200:
        revision = str(body.get("revision")) if isinstance(body, dict) and body.get("revision") is not None else None
        checks.append(PreflightCheck("决策令牌", True, f"令牌有效（策略版本 {revision or '未知'}）", status=200, latency_ms=ms, detail={"revision": revision}))
    elif resp.status_code == 401:
        checks.append(PreflightCheck("决策令牌", False, "决策令牌被权限中心拒绝（401），请核对令牌", status=401, latency_ms=ms))
    elif resp.status_code == 403:
        checks.append(PreflightCheck("决策令牌", False, "填写的是控制令牌而不是决策令牌（403）", status=403, latency_ms=ms))
    elif resp.status_code in (502, 503, 504):
        checks.append(PreflightCheck("决策令牌", False, f"权限中心暂时不可用（{resp.status_code}）", status=resp.status_code, latency_ms=ms))
    else:
        checks.append(PreflightCheck("决策令牌", False, f"权限中心返回了意外状态 {resp.status_code}", status=resp.status_code, latency_ms=ms))
    if checks[-1].ok is False:
        return PreflightReport(ok=False, checks=checks, tested_at=_now())

    # 4. identity (optional)
    if conn.identity_internal_url and conn.runtime_client_id and conn.runtime_client_secret:
        iurl = conn.identity_internal_url.rstrip("/")
        resp, ms, err = _timed(lambda: http.post(
            f"{iurl}/internal/v1/identity/oauth/token",
            data={"grant_type": "client_credentials", "client_id": conn.runtime_client_id, "client_secret": conn.runtime_client_secret},
            headers={"X-Request-ID": uuid.uuid4().hex},
        ))
        if err or resp is None:
            checks.append(PreflightCheck("身份中心（工作负载凭证）", False, f"身份中心 {err}", latency_ms=ms, fatal=False))
        elif resp.status_code == 200 and isinstance(_json(resp), dict) and _json(resp).get("access_token"):
            checks.append(PreflightCheck("身份中心（工作负载凭证）", True, "运行时客户端凭证有效", status=200, latency_ms=ms, fatal=False))
        elif resp.status_code == 401:
            checks.append(PreflightCheck("身份中心（工作负载凭证）", False, "运行时客户端 ID / 密钥无效（401）", status=401, latency_ms=ms, fatal=False))
        else:
            checks.append(PreflightCheck("身份中心（工作负载凭证）", False, f"身份中心返回 {resp.status_code}", status=resp.status_code, latency_ms=ms, fatal=False))
    else:
        checks.append(PreflightCheck("身份中心（工作负载凭证）", None, "未配置（可选：只做权限判定时不需要）", fatal=False))

    # 5. is this admin known to Base at all (informational)
    if tenant_id and principal_id:
        resp, ms, err = _timed(lambda: http.post(
            f"{url}/internal/v1/authz/check",
            json={
                "principal": {"type": "user", "id": canonical_id(principal_id), "tenant_id": canonical_id(tenant_id)},
                "action": "view",
                "resource": {"type": "tenant", "id": canonical_id(tenant_id), "tenant_id": canonical_id(tenant_id)},
                "request_id": uuid.uuid4().hex, "consistency": "minimize_latency",
            },
            headers={"X-Base-Service-Token": conn.decision_token, "X-Request-ID": uuid.uuid4().hex},
        ))
        body = _json(resp) if resp is not None else None
        if err or resp is None:
            checks.append(PreflightCheck("本租户同步状态", None, f"未能检查（{err}）", latency_ms=ms, fatal=False))
        elif resp.status_code == 200 and isinstance(body, dict) and body.get("allowed") is True:
            checks.append(PreflightCheck("本租户同步状态", True, "当前管理员已同步到权限中心", status=200, latency_ms=ms, fatal=False))
        elif resp.status_code == 200 and isinstance(body, dict):
            reason = str(body.get("reason") or "denied")
            hint = {"unknown_principal": "权限中心尚未同步本租户或当前用户", "inactive_principal": "当前用户在权限中心处于停用状态", "unknown_resource": "权限中心尚未同步本租户", "authorization_pending": "权限中心正在同步，稍后再试"}.get(reason, f"权限中心拒绝（{reason}）")
            checks.append(PreflightCheck("本租户同步状态", False, f"{hint}。切换后本租户的员工与资源在同步前会被全部拒绝", status=200, latency_ms=ms, fatal=False, detail={"reason": reason}))
        else:
            checks.append(PreflightCheck("本租户同步状态", None, f"权限中心返回 {resp.status_code}", status=resp.status_code, latency_ms=ms, fatal=False))

    ok = all(c.ok is not False for c in checks if c.fatal)
    return PreflightReport(ok=ok, checks=checks, authz_revision=revision, tested_at=_now())


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
