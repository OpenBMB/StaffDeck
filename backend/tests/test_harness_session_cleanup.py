from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.chat import delete_chat_session
from app.core import harness_session_cleanup
from app.core.harness_session_cleanup import (
    harness_path_segment,
    harness_session_workspace_path,
    harness_task_workspace_candidates,
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
    Team,
    Tenant,
    UIConfig,
    User,
)
from app.teams.service import delete_team


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
    workspace_root: str | None = None,
) -> tuple[str, str, str]:
    """Create one complete Harness record set, optionally pinned to a historical workspace root."""

    task_frame = HarnessTaskFrameRecord(
        id=f"htask_{suffix}",
        tenant_id=tenant_id,
        session_id=session_id,
        source_turn_id=f"turn_{suffix}",
        task_id=f"task_{suffix}",
        workspace_root=workspace_root,
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
    """Cleanup must remove only the requested default-root session path."""

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
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
    """Cleanup must unlink an exact session symlink without touching its target."""

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
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


def test_task_workspace_candidates_prefer_snapshot_and_reject_symlinked_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task-root snapshot must win over the legacy root without permitting symlink escapes."""

    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "app-data"))
    engine = _test_engine()
    snapshot_root = tmp_path / "snapshot-workspace"
    legacy_root = tmp_path / "app-data" / "harness_workspaces"

    with Session(engine) as db:
        db.add(
            HarnessTaskFrameRecord(
                id="htask_snapshot",
                tenant_id="tenant_demo",
                session_id="session_demo",
                source_turn_id="turn_snapshot",
                task_id="task_snapshot",
                workspace_root=str(snapshot_root),
            )
        )
        db.add(
            HarnessTaskFrameRecord(
                id="htask_legacy",
                tenant_id="tenant_demo",
                session_id="session_demo",
                source_turn_id="turn_legacy",
                task_id="task_legacy",
            )
        )
        db.commit()

        snapshot_candidates = harness_task_workspace_candidates(
            tenant_id="tenant_demo",
            session_id="session_demo",
            task_frame_id="task_snapshot",
            db=db,
        )
        legacy_candidates = harness_task_workspace_candidates(
            tenant_id="tenant_demo",
            session_id="session_demo",
            task_frame_id="task_legacy",
            db=db,
        )

        assert len(snapshot_candidates) == 2
        assert snapshot_candidates[0].is_relative_to(snapshot_root)
        assert snapshot_candidates[1].is_relative_to(legacy_root)
        assert len(legacy_candidates) == 2
        assert legacy_candidates[0].is_relative_to(legacy_root)
        assert harness_session_cleanup.harness_task_workspace_path(
            tenant_id="tenant_demo",
            session_id="session_demo",
            task_frame_id="task_snapshot",
            db=db,
        ) == snapshot_candidates[0]

        unsafe_root = tmp_path / "unsafe-workspace"
        external_root = tmp_path / "external-workspace"
        external_root.mkdir()
        unsafe_root.symlink_to(external_root, target_is_directory=True)
        db.add(
            HarnessTaskFrameRecord(
                id="htask_unsafe",
                tenant_id="tenant_demo",
                session_id="session_demo",
                source_turn_id="turn_unsafe",
                task_id="task_unsafe",
                workspace_root=str(unsafe_root),
            )
        )
        db.commit()

        with pytest.raises(OSError, match="symlink"):
            harness_task_workspace_candidates(
                tenant_id="tenant_demo",
                session_id="session_demo",
                task_frame_id="task_unsafe",
                db=db,
            )


def test_task_workspace_candidates_keep_custom_root_for_frames_without_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-snapshot TaskFrames must probe the configured root after the old default root."""

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "app-data"))
    engine = _test_engine()
    configured_root = tmp_path / "configured-workspaces"
    legacy_root = tmp_path / "app-data" / "harness_workspaces"

    with Session(engine) as db:
        db.add(
            UIConfig(
                tenant_id="tenant_demo",
                sandbox_enabled=False,
                harness_storage_path=str(configured_root),
            )
        )
        db.add(
            HarnessTaskFrameRecord(
                id="htask_legacy_configured",
                tenant_id="tenant_demo",
                session_id="session_demo",
                source_turn_id="turn_legacy_configured",
                task_id="task_legacy_configured",
            )
        )
        db.commit()

        legacy_candidate, configured_candidate = harness_task_workspace_candidates(
            tenant_id="tenant_demo",
            session_id="session_demo",
            task_frame_id="task_legacy_configured",
            db=db,
        )

    assert configured_candidate.is_relative_to(configured_root)
    assert legacy_candidate.is_relative_to(legacy_root)


def test_default_task_workspace_uses_home_without_writing_to_app_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new task must provision its workspace below the user's StaffDeck home directory."""

    home = tmp_path / "home"
    app_data = tmp_path / "app-data"
    expected_root = home / ".staffdeck" / "workspaces"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(app_data))

    workspace = harness_session_cleanup.harness_task_workspace_path(
        tenant_id="tenant_demo",
        session_id="session_demo",
        task_frame_id="task_default",
    )
    workspace.mkdir(parents=True)

    assert workspace.is_relative_to(expected_root)
    assert expected_root.is_dir()
    assert not (app_data / "harness_workspaces").exists()


def test_workspace_cleanup_removes_snapshot_and_legacy_roots_without_touching_siblings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup removes only exact snapshot and legacy paths without following links."""

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "app-data"))
    engine = _test_engine()
    snapshot_root = tmp_path / "snapshot-workspace"
    with Session(engine) as db:
        db.add(
            HarnessTaskFrameRecord(
                id="htask_target",
                tenant_id="tenant_demo",
                session_id="session_target",
                source_turn_id="turn_target",
                task_id="task_target",
                workspace_root=str(snapshot_root),
            )
        )
        db.commit()

        snapshot_task, legacy_task = harness_task_workspace_candidates(
            tenant_id="tenant_demo",
            session_id="session_target",
            task_frame_id="task_target",
            db=db,
        )
        snapshot_session = snapshot_task.parent
        legacy_session = legacy_task.parent
        snapshot_sibling = snapshot_task.parents[1] / harness_path_segment("session_sibling")
        legacy_sibling = legacy_task.parents[1] / harness_path_segment("session_sibling")
        snapshot_session.mkdir(parents=True)
        snapshot_sibling.mkdir(parents=True)
        (snapshot_sibling / "keep.txt").write_text("snapshot sibling", encoding="utf-8")
        legacy_sibling.mkdir(parents=True)
        (legacy_sibling / "keep.txt").write_text("legacy sibling", encoding="utf-8")
        external = tmp_path / "external-legacy-session"
        external.mkdir()
        (external / "keep.txt").write_text("external", encoding="utf-8")
        legacy_session.parent.mkdir(parents=True, exist_ok=True)
        legacy_session.symlink_to(external, target_is_directory=True)

        assert remove_harness_session_workspace(
            tenant_id="tenant_demo",
            session_id="session_target",
            db=db,
        )

        assert not snapshot_session.exists()
        assert not legacy_session.is_symlink()
        assert (snapshot_sibling / "keep.txt").read_text(encoding="utf-8") == "snapshot sibling"
        assert (legacy_sibling / "keep.txt").read_text(encoding="utf-8") == "legacy sibling"
        assert (external / "keep.txt").read_text(encoding="utf-8") == "external"


def test_delete_chat_session_cleans_harness_state_and_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting a chat must remove the root snapshot before the TaskFrame record disappears."""

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    engine = _test_engine()
    snapshot_root = tmp_path / "snapshot-workspaces"
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
            workspace_root=str(snapshot_root),
        )
        survivor_ids = _add_harness_records(
            db,
            tenant_id="tenant_demo",
            session_id="session_other",
            suffix="other",
        )
        db.commit()

        workspace = (
            snapshot_root
            / harness_path_segment("tenant_demo")
            / harness_path_segment("session_target")
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


def test_delete_team_cleans_team_session_harness_state_and_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting a team must remove its session Harness state and default workspace."""

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    engine = _test_engine()
    snapshot_root = tmp_path / "team-snapshot-workspaces"
    with Session(engine) as db:
        db.add(Tenant(id="tenant_demo", name="Demo"))
        team = Team(
            tenant_id="tenant_demo",
            name="增长团队",
            owner_user_id="user_demo",
            status="active",
        )
        db.add(team)
        db.add(
            ChatSession(
                id="session_team",
                tenant_id="tenant_demo",
                user_id="user_demo",
                team_id=team.id,
                title="团队 增长团队 · TL 对话",
            )
        )
        target_ids = _add_harness_records(
            db,
            tenant_id="tenant_demo",
            session_id="session_team",
            suffix="team",
            workspace_root=str(snapshot_root),
        )
        db.commit()

        workspace = (
            snapshot_root
            / harness_path_segment("tenant_demo")
            / harness_path_segment("session_team")
        )
        workspace.mkdir(parents=True)
        (workspace / "artifact.txt").write_text("artifact", encoding="utf-8")

        delete_team(db, team)

        assert db.get(ChatSession, "session_team") is None
        assert not workspace.exists()
        assert db.get(HarnessInvocationRecord, target_ids[0]) is None
        assert db.get(HarnessRunRecord, target_ids[1]) is None
        assert db.get(HarnessTaskFrameRecord, target_ids[2]) is None
