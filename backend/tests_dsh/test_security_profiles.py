from __future__ import annotations

import pytest

from staffdeck_dsh.contracts import PermissionDenied, ResourceRef, SecurityContext
from staffdeck_dsh.security import Guard, LocalPep, build_oss_local_profile
from staffdeck_dsh.security.business_base import BaseAuthzClient, BaseAuthzConfig, BasePep, _Contract, _Pending, _Unavailable


def _ctx(role: str = "member", uid: str = "u1", tenant: str = "t1") -> SecurityContext:
    return SecurityContext(principal_id=uid, tenant_id=tenant, tenant_role=role)  # type: ignore[arg-type]


def test_oss_local_is_not_a_noop_denies_by_default() -> None:
    pep = LocalPep()
    kb = ResourceRef(type="knowledge_base", id="kb1", tenant_id="t1", attributes={})
    assert pep.authorize(_ctx(), "knowledge", "use", kb).allowed is False


def test_oss_local_tenant_boundary() -> None:
    pep = LocalPep()
    kb = ResourceRef(type="knowledge_base", id="kb1", tenant_id="t2", attributes={"binding_status": "active", "private_to_agent": True})
    d = pep.authorize(_ctx(role="admin"), "knowledge", "use", kb)
    assert d.allowed is False and "tenant" in d.reason


def test_oss_local_bound_resource_and_admin_rules() -> None:
    pep = LocalPep()
    bound = ResourceRef(type="tool", id="tool1", tenant_id="t1", attributes={"binding_status": "active", "open_gallery": True})
    assert pep.authorize(_ctx(), "tool", "use", bound).allowed
    assert pep.authorize(_ctx(), "tool", "manage", bound).allowed is False
    assert pep.authorize(_ctx(role="admin"), "tool", "manage", bound).allowed
    owner = ResourceRef(type="tool", id="tool2", tenant_id="t1", attributes={"owner_user_id": "u1"})
    assert pep.authorize(_ctx(), "tool", "manage", owner).allowed


def test_oss_local_agent_visibility_matrix() -> None:
    pep = LocalPep()
    private = ResourceRef(type="agent", id="a1", tenant_id="t1", attributes={"owner_user_id": "u9"})
    assert pep.authorize(_ctx(), "staff", "use", private).allowed is False
    published = ResourceRef(type="agent", id="a2", tenant_id="t1", attributes={"owner_user_id": "u9", "published_to_gallery": True})
    assert pep.authorize(_ctx(), "staff", "use", published).allowed
    assert pep.authorize(_ctx(), "staff", "manage", published).allowed is False
    overall = ResourceRef(type="agent", id="a3", tenant_id="t1", attributes={"is_overall": True, "owner_user_id": "u1"})
    assert pep.authorize(_ctx(), "staff", "use", overall).allowed
    assert pep.authorize(_ctx(), "staff", "manage", overall).allowed is False  # owner but overall -> admin only
    assert pep.authorize(_ctx(role="admin"), "staff", "manage", overall).allowed


def test_guard_maps_operation_and_raises_typed_denial() -> None:
    guard = Guard("knowledge", build_oss_local_profile())
    kb = ResourceRef(type="knowledge_base", id="kb1", tenant_id="t1", attributes={})
    with pytest.raises(PermissionDenied) as exc:
        guard.require(_ctx(), "knowledge.search/v1", kb)
    assert exc.value.code == "PERMISSION_DENIED"
    assert exc.value.details["profile"] == "OSS_LOCAL"


def test_guard_filter_uses_profile() -> None:
    guard = Guard("tool", build_oss_local_profile())
    ok = ResourceRef(type="tool", id="ok", tenant_id="t1", attributes={"binding_status": "active", "private_to_agent": True})
    bad = ResourceRef(type="tool", id="bad", tenant_id="t1", attributes={"binding_status": "inactive"})
    assert [r.id for r in guard.filter(_ctx(), "tool.invoke/v1", [ok, bad])] == ["ok"]


class _FakeClient(BaseAuthzClient):
    def __init__(self, behaviour):
        self.config = BaseAuthzConfig(url="http://base", decision_token="tok", pending_timeout_seconds=0.05, pending_poll_seconds=0.001)
        self._behaviour = behaviour
        self.calls = 0

    def check(self, payload):
        self.calls += 1
        b = self._behaviour
        if isinstance(b, Exception):
            raise b
        if callable(b):
            return b(payload)
        return {**b, "request_id": payload["request_id"]}

    def batch_check(self, payloads):
        return {"decisions": [{"request_id": p["request_id"], "allowed": p["resource"]["id"] == "ok"} for p in payloads], "revision": "r1"}


def test_business_base_allow_and_deny_follow_base() -> None:
    kb = ResourceRef(type="knowledge_base", id="kb1", tenant_id="t1")
    assert BasePep(_FakeClient({"allowed": True, "reason": "ok", "decision_id": "d", "revision": "r"})).authorize(_ctx(), "knowledge", "use", kb).allowed
    d = BasePep(_FakeClient({"allowed": False, "reason": "nope"})).authorize(_ctx(), "knowledge", "use", kb)
    assert d.allowed is False and d.source == "BUSINESS_BASE"


@pytest.mark.parametrize("err", [_Unavailable("down"), _Contract("bad body")])
def test_business_base_fails_closed_no_local_fallback(err) -> None:
    kb = ResourceRef(type="knowledge_base", id="kb1", tenant_id="t1", attributes={"binding_status": "active", "private_to_agent": True})
    d = BasePep(_FakeClient(err)).authorize(_ctx(role="admin"), "knowledge", "use", kb)
    assert d.allowed is False and d.source == "BUSINESS_BASE"


def test_business_base_pending_settles_then_denies() -> None:
    kb = ResourceRef(type="knowledge_base", id="kb1", tenant_id="t1")
    client = _FakeClient(_Pending())
    d = BasePep(client, clock=__import__("time").monotonic, sleep=lambda s: None).authorize(_ctx(), "knowledge", "use", kb)
    assert d.allowed is False and d.pending is True and client.calls > 1


def test_business_base_contract_mismatch_is_denied() -> None:
    kb = ResourceRef(type="knowledge_base", id="kb1", tenant_id="t1")
    d = BasePep(_FakeClient(lambda p: {"allowed": True, "request_id": "different"})).authorize(_ctx(), "knowledge", "use", kb)
    assert d.allowed is False


def test_business_base_filter_uses_batch_and_keeps_only_allowed() -> None:
    pep = BasePep(_FakeClient({"allowed": True}))
    ok = ResourceRef(type="tool", id="ok", tenant_id="t1")
    bad = ResourceRef(type="tool", id="bad", tenant_id="t1")
    other = ResourceRef(type="tool", id="ok", tenant_id="t2")
    assert [r.id for r in pep.filter(_ctx(), "tool", "use", [ok, bad, other])] == ["ok"]
