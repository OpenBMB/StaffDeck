from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.core.harness_session_lease import (
    HarnessSessionLeaseLost,
    HarnessSessionLeaseStore,
)
from app.core.harness_session_lock import HarnessSessionBusy
from app.db.models import (
    ChatSession,
    HarnessSessionLeaseRecord,
    utc_now,
)


def _engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def test_session_lease_blocks_parallel_worker_and_releases_cleanly() -> None:
    engine = _engine()
    with Session(engine) as first_db, Session(engine) as second_db:
        session = ChatSession(id="session-1", tenant_id="tenant-demo")
        first_db.add(session)
        first_db.commit()
        first = HarnessSessionLeaseStore(first_db)
        second = HarnessSessionLeaseStore(second_db)

        lease = first.acquire(session)
        with pytest.raises(HarnessSessionBusy):
            second.acquire(session)

        first.release(lease)
        replacement = second.acquire(session)
        assert replacement.lease_owner != lease.lease_owner
        second.release(replacement)


def test_expired_session_lease_is_taken_over_and_old_owner_is_fenced() -> None:
    engine = _engine()
    with Session(engine) as first_db, Session(engine) as second_db:
        session = ChatSession(id="session-1", tenant_id="tenant-demo")
        first_db.add(session)
        first_db.commit()
        first = HarnessSessionLeaseStore(first_db)
        second = HarnessSessionLeaseStore(second_db)

        lease = first.acquire(session)
        lease_row = first_db.get(
            HarnessSessionLeaseRecord,
            lease.record_id,
        )
        assert lease_row is not None
        lease_row.lease_expires_at = utc_now() - timedelta(seconds=1)
        first_db.add(lease_row)
        first_db.commit()
        old_owner = lease.lease_owner

        replacement = second.acquire(session)
        assert replacement.lease_owner != old_owner
        with pytest.raises(HarnessSessionLeaseLost):
            first.renew(lease)
        second.release(replacement)
