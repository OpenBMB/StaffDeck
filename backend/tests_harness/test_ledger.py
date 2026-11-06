from __future__ import annotations

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from staffdeck_harness.capabilities import InvocationLedger
from staffdeck_harness.contracts import InvocationContext, ModuleInvocation, ModuleResult, OutcomeUnknown


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _ctx(**kw) -> InvocationContext:
    base = dict(tenant_id="t1", agent_id="a1", user_id="u1", session_id="s1", turn_id="turn1", channel="web", task_frame_id="tf1", step_id="n1", run_id="run1")
    base.update(kw)
    return InvocationContext(**base)


def _inv(iid: str, *, side_effecting=True, args=None) -> ModuleInvocation:
    return ModuleInvocation(invocation_id=iid, module_id="tool", operation="tool.invoke/v1", arguments=args or {"tool_id": "t1", "amount": 5}, context=_ctx(), binding_id="t1", side_effecting=side_effecting)


def test_completed_side_effect_replays(db) -> None:
    ledger = InvocationLedger(db)
    inv = _inv("c1")
    entry = ledger.start(inv)
    r = ledger.finish(entry, ModuleResult.ok({"order": "o1"}))
    assert r.status == "completed" and r.side_effect_key
    replay = ledger.replay_or_block(_inv("c2"))
    assert replay is not None
    result, receipt = replay
    assert result.success and result.data["order"] == "o1" and result.data["idempotent_replay"] is True
    assert receipt.replayed_from == entry.row.id


def test_not_sent_failure_releases_claim(db) -> None:
    ledger = InvocationLedger(db)
    entry = ledger.start(_inv("f1"))
    r = ledger.finish(entry, ModuleResult.fail("INVALID_ARGUMENTS", "bad"))
    assert r.status == "failed" and r.side_effect_key is None
    assert ledger.replay_or_block(_inv("f2")) is None  # retry allowed


def test_maybe_sent_failure_is_outcome_unknown_and_blocks(db) -> None:
    ledger = InvocationLedger(db)
    entry = ledger.start(_inv("u1"))
    r = ledger.finish(entry, ModuleResult.fail("TIMEOUT", "gateway timeout"))
    assert r.status == "outcome_unknown" and r.side_effect_key
    with pytest.raises(OutcomeUnknown):
        ledger.replay_or_block(_inv("u2"))
    rows = ledger.unknown_outcomes("t1")
    assert [x.id for x in rows] == [entry.row.id]
    ledger.reconcile(rows[0], status="failed")
    assert ledger.replay_or_block(_inv("u3")) is None


def test_read_only_invocations_get_receipts_but_never_dedupe(db) -> None:
    ledger = InvocationLedger(db)
    inv = _inv("r1", side_effecting=False)
    assert inv.side_effect_key() is None
    entry = ledger.start(inv)
    r = ledger.finish(entry, ModuleResult.fail("TIMEOUT", "x"))
    assert r.status == "failed"  # read-only can never be outcome_unknown
    assert ledger.replay_or_block(_inv("r2", side_effecting=False)) is None


def test_denied_and_cancelled_release_claim(db) -> None:
    ledger = InvocationLedger(db)
    e1 = ledger.start(_inv("d1"))
    assert ledger.deny(e1, {"code": "PERMISSION_DENIED", "message": "no"}).status == "denied"
    assert ledger.replay_or_block(_inv("d2")) is None
    e2 = ledger.start(_inv("d3"))
    assert ledger.cancel(e2).status == "cancelled"
    assert ledger.replay_or_block(_inv("d4")) is None


def test_side_effect_key_scoped_to_step_and_arguments() -> None:
    a = _inv("k1").side_effect_key()
    b = _inv("k2").side_effect_key()
    c = _inv("k3", args={"tool_id": "t1", "amount": 6}).side_effect_key()
    d = ModuleInvocation(invocation_id="k4", module_id="tool", operation="tool.invoke/v1", arguments={"tool_id": "t1", "amount": 5}, context=_ctx(step_id="n2"), binding_id="t1", side_effecting=True).side_effect_key()
    assert a == b and a != c and a != d
