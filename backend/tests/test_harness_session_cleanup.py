from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.chat import delete_chat_session
from app.core.harness_session_cleanup import (
    harness_path_segment,
    harness_session_workspace_path,
    remove_harness_session_workspace,
    stage_harness_session_record_deletion,
)
from app.db.models import (
    ChatSession,
    HarnessInvocationRecord,
    HarnessRunRecord,
    HarnessSessionLeaseRecord,
    HarnessTaskFrameRecord,
    HarnessTurnRecord,
    Tenant,
    User,
)


def _test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _add_harness_records(
    db: Session,
    *,
    tenant_id: str,
    session_id: str,
    suffix: str,
) -> tuple[str, str, str]:
    task_frame = HarnessTaskFrameRecord(
        id=f"htask_{suffix}",
        tenant_id=tenant_id,
        session_id=session_id,
        source_turn_id=f"turn_{suffix}",
        task_id=f"task_{suffix}",
    )
    run = HarnessRunRecord(
        id=f"hrun_{suffix}",
        tenant_id=tenant_id,
        session_id=session_id,
        task_frame_record_id=task_frame.id,
        task_id=task_frame.task_id,
        source_turn_id=task_frame.source_turn_id,
    )
    invocation = HarnessInvocationRecord(
        id=f"hinvoke_{suffix}",
        tenant_id=tenant_id,
        session_id=session_id,
        task_id=task_frame.task_id,
        run_id=run.id,
        call_id=f"call_{suffix}",
        tool_name="read_file",
        request_digest=f"digest_{suffix}",
    )
    db.add_all([task_frame, run, invocation])
    db.add(
        HarnessTurnRecord(
            id=f"hturn_{suffix}",
            tenant_id=tenant_id,
            session_id=session_id,
            client_turn_id=f"client_turn_{suffix}",
            request_digest=f"request_digest_{suffix}",
            lease_owner=f"lease_{suffix}",
            lease_expires_at=task_frame.created_at,
        )
    )
    db.add(
        HarnessSessionLeaseRecord(
            id=f"hslease_{suffix}",
            tenant_id=tenant_id,
            session_id=session_id,
            lease_owner=f"session_lease_{suffix}",
            lease_expires_at=task_frame.created_at,
        )
    )
    return invocation.id, run.id, task_frame.id


def test_stage_harness_record_deletion_is_tenant_and_session_scoped() -> None:
    engine = _test_engine()
    with Session(engine) as db:
        target_ids = _add_harness_records(
            db,
            tenant_id="tenant_target",
            session_id="session_target",
            suffix="target",
        )
        same_tenant_ids = _add_harness_records(
            db,
            tenant_id="tenant_target",
            session_id="session_other",
            suffix="same_tenant",
        )
        same_session_ids = _add_harness_records(
            db,
            tenant_id="tenant_other",
            session_id="session_target",
            suffix="same_session",
        )
        db.commit()

        result = stage_harness_session_record_deletion(
            db,
            tenant_id="tenant_target",
            session_id="session_target",
        )
        db.commit()

        assert result.invocation_count == 1
        assert result.run_count == 1
        assert result.task_frame_count == 1
        assert result.turn_count == 1
        assert result.session_lease_count == 1
        assert db.get(HarnessInvocationRecord, target_ids[0]) is None
        assert db.get(HarnessRunRecord, target_ids[1]) is None
        assert db.get(HarnessTaskFrameRecord, target_ids[2]) is None
        for record_ids in (same_tenant_ids, same_session_ids):
            assert db.get(HarnessInvocationRecord, record_ids[0]) is not None
            assert db.get(HarnessRunRecord, record_ids[1]) is not None
            assert db.get(HarnessTaskFrameRecord, record_ids[2]) is not None


def test_workspace_cleanup_uses_invoker_segment_and_removes_only_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    segments = {
        harness_path_segment(value)
        for value in ("tenant_demo", "session/../../target", "", "名字 with spaces")
    }
    assert len(segments) == 4
    assert "/" not in harness_path_segment("session/../../target")

    target = harness_session_workspace_path(
        tenant_id="tenant_demo",
        session_id="session/../../target",
    )
    sibling = harness_session_workspace_path(
        tenant_id="tenant_demo",
        session_id="session_other",
    )
    target.mkdir(parents=True)
    sibling.mkdir(parents=True)
    (target / "target.txt").write_text("target", encoding="utf-8")
    (sibling / "keep.txt").write_text("keep", encoding="utf-8")

    assert remove_harness_session_workspace(
        tenant_id="tenant_demo",
        session_id="session/../../target",
    )
    assert not target.exists()
    assert (sibling / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_workspace_cleanup_unlinks_exact_symlink_without_following_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    target = harness_session_workspace_path(
        tenant_id="tenant_demo",
        session_id="session_target",
    )
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep.txt").write_text("keep", encoding="utf-8")
    target.parent.mkdir(parents=True)
    target.symlink_to(external, target_is_directory=True)

    assert remove_harness_session_workspace(
        tenant_id="tenant_demo",
        session_id="session_target",
    )
    assert not target.is_symlink()
    assert (external / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_delete_chat_session_cleans_harness_state_and_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    engine = _test_engine()
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        user = User(
            id="user_demo",
            tenant_id="tenant_demo",
            username="demo",
            password_hash="test",
        )
        db.add(user)
        db.add(
            ChatSession(
                id="session_target",
                tenant_id="tenant_demo",
                user_id=user.id,
            )
        )
        target_ids = _add_harness_records(
            db,
            tenant_id="tenant_demo",
            session_id="session_target",
            suffix="target",
        )
        survivor_ids = _add_harness_records(
            db,
            tenant_id="tenant_demo",
            session_id="session_other",
            suffix="other",
        )
        db.commit()

        workspace = harness_session_workspace_path(
            tenant_id="tenant_demo",
            session_id="session_target",
        )
        workspace.mkdir(parents=True)
        (workspace / "artifact.txt").write_text("artifact", encoding="utf-8")

        result = delete_chat_session(
            "session_target",
            tenant_id="tenant_demo",
            current_user=user,
            db=db,
        )

        assert result == {"status": "deleted"}
        assert db.get(ChatSession, "session_target") is None
        assert not workspace.exists()
        assert db.get(HarnessInvocationRecord, target_ids[0]) is None
        assert db.get(HarnessRunRecord, target_ids[1]) is None
        assert db.get(HarnessTaskFrameRecord, target_ids[2]) is None
        assert db.get(HarnessInvocationRecord, survivor_ids[0]) is not None
        assert db.get(HarnessRunRecord, survivor_ids[1]) is not None
        assert db.get(HarnessTaskFrameRecord, survivor_ids[2]) is not None
        assert db.exec(select(HarnessTaskFrameRecord)).all()
