from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.chat import delete_chat_session
from app.core.engine.session.cleanup import (
    harness_path_segment,
    harness_session_workspace_path,
    remove_harness_session_workspace,
    stage_harness_session_record_deletion,
)
from app.db.models import (
    ChatSession,
    HarnessAgentLoopRecord,
    HarnessInvocationRecord,
    HarnessRunRecord,
    HarnessSessionLeaseRecord,
    HarnessTaskFrameRecord,
    HarnessTurnRecord,
    Team,
    Tenant,
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
) -> tuple[str, str, str, str]:
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
    # 曾经被漏删的表：会话删除后留下 active 孤儿 loop，这里纳入覆盖。
    # loop_key 必须带 suffix：唯一约束是 (session_id, loop_key)，不含 tenant_id，
    # 而本测试特意构造了「同 session_id 跨租户」的行。
    agent_loop = HarnessAgentLoopRecord(
        id=f"hloop_{suffix}",
        tenant_id=tenant_id,
        session_id=session_id,
        loop_key=f"general:{suffix}",
        kind="general",
        status="active",
        owner_task_frame_record_id=task_frame.id,
    )
    db.add_all([task_frame, run, invocation, agent_loop])
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
    return invocation.id, run.id, task_frame.id, agent_loop.id


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
        assert result.agent_loop_count == 1
        assert db.get(HarnessInvocationRecord, target_ids[0]) is None
        assert db.get(HarnessRunRecord, target_ids[1]) is None
        assert db.get(HarnessTaskFrameRecord, target_ids[2]) is None
        # 回归点：harness_agent_loops 曾经完全不参与清理，会话删掉后留下 active 孤儿行。
        assert db.get(HarnessAgentLoopRecord, target_ids[3]) is None
        for record_ids in (same_tenant_ids, same_session_ids):
            assert db.get(HarnessInvocationRecord, record_ids[0]) is not None
            assert db.get(HarnessRunRecord, record_ids[1]) is not None
            assert db.get(HarnessTaskFrameRecord, record_ids[2]) is not None
            assert db.get(HarnessAgentLoopRecord, record_ids[3]) is not None


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
        assert db.get(HarnessAgentLoopRecord, target_ids[3]) is None
        assert db.get(HarnessInvocationRecord, survivor_ids[0]) is not None
        assert db.get(HarnessRunRecord, survivor_ids[1]) is not None
        assert db.get(HarnessTaskFrameRecord, survivor_ids[2]) is not None
        assert db.get(HarnessAgentLoopRecord, survivor_ids[3]) is not None
        assert db.exec(select(HarnessTaskFrameRecord)).all()


def test_delete_team_cleans_team_session_harness_state_and_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    engine = _test_engine()
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
        )
        db.commit()

        workspace = harness_session_workspace_path(
            tenant_id="tenant_demo",
            session_id="session_team",
        )
        workspace.mkdir(parents=True)
        (workspace / "artifact.txt").write_text("artifact", encoding="utf-8")

        delete_team(db, team)

        assert db.get(ChatSession, "session_team") is None
        assert not workspace.exists()
        assert db.get(HarnessInvocationRecord, target_ids[0]) is None
        assert db.get(HarnessRunRecord, target_ids[1]) is None
        assert db.get(HarnessTaskFrameRecord, target_ids[2]) is None
        assert db.get(HarnessAgentLoopRecord, target_ids[3]) is None
