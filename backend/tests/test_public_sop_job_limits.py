from threading import BoundedSemaphore, Event
from types import SimpleNamespace

import pytest

from app.public_api import sops
from app.public_api.errors import PublicAPIError
from app.public_api.jobs import JobCancelled, _require_job_scope


def test_public_sop_times_out_without_releasing_the_distiller_slot(monkeypatch):
    slot = BoundedSemaphore()
    release = Event()
    monkeypatch.setattr(sops, "_DISTILL_SLOT", slot)
    monkeypatch.setattr(sops, "_DISTILL_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(sops, "ensure_not_cancelled", lambda db, job: None)
    monkeypatch.setattr(sops.SkillDistiller, "distill", lambda *args: release.wait(2))
    try:
        with pytest.raises(PublicAPIError) as timeout:
            sops._run_public_distillation(None, None, None, None)
        assert timeout.value.code == "SOP_GENERATION_TIMEOUT"
        with pytest.raises(PublicAPIError) as busy:
            sops._run_public_distillation(None, None, None, None)
        assert busy.value.code == "SOP_GENERATION_BUSY"
    finally:
        release.set()
        assert slot.acquire(timeout=2)
        slot.release()


def test_public_sop_cancellation_ignores_the_provider_result(monkeypatch):
    def cancelled(db, job):
        raise JobCancelled()

    monkeypatch.setattr(sops, "_DISTILL_SLOT", BoundedSemaphore())
    monkeypatch.setattr(sops, "ensure_not_cancelled", cancelled)
    monkeypatch.setattr(sops.SkillDistiller, "distill", lambda *args: object())
    with pytest.raises(JobCancelled):
        sops._run_public_distillation(None, None, None, None)


def test_sop_write_only_cancels_the_creating_credentials_jobs():
    principal = SimpleNamespace(
        credential_id="actor_key", can=lambda scope: scope == "sops:write",
    )
    own_job = SimpleNamespace(kind="sop.generate", credential_id="actor_key")
    other_job = SimpleNamespace(kind="sop.generate", credential_id="another_key")
    _require_job_scope(principal, own_job, "cancel")
    with pytest.raises(PublicAPIError) as denied:
        _require_job_scope(principal, other_job, "cancel")
    assert denied.value.code == "INSUFFICIENT_SCOPE"
