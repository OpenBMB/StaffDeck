from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.chat import download_harness_artifact
from app.core.harness_session_cleanup import (
    default_harness_storage_root,
    harness_task_workspace_candidates,
    harness_task_workspace_path,
)
from app.db.models import (
    APIJob,
    ChatSession,
    HarnessTaskFrameRecord,
    Message,
    Tenant,
    User,
)
from app.harness import (
    HarnessArtifactAccessError,
    normalize_harness_artifact_path,
    open_harness_artifact,
    publish_changed_harness_artifacts,
    publish_harness_artifacts,
    snapshot_harness_workspace,
)
from app.public_api.auth import PublicPrincipal
from app.public_api.errors import PublicAPIError
from app.public_api.runs import download_run_artifact


def _test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _seed_artifact(
    db: Session,
    *,
    tenant_id: str = "tenant_demo",
    session_id: str = "session_demo",
    task_frame_id: str = "task_demo",
    artifact_path: str = "reports/result.txt",
    workspace_root: str | None = None,
) -> User:
    db.add(Tenant(id=tenant_id, name="Demo"))
    user = User(
        id="user_owner",
        tenant_id=tenant_id,
        username="owner",
        password_hash="test",
    )
    db.add(user)
    db.add(
        ChatSession(
            id=session_id,
            tenant_id=tenant_id,
            user_id=user.id,
        )
    )
    db.add(
        HarnessTaskFrameRecord(
            id="htask_demo",
            tenant_id=tenant_id,
            session_id=session_id,
            source_turn_id="turn_demo",
            task_id=task_frame_id,
            workspace_root=workspace_root,
        )
    )
    db.add(
        Message(
            id="msg_assistant",
            tenant_id=tenant_id,
            session_id=session_id,
            role="assistant",
            content="文件已生成。",
            metadata_json={
                "harness_artifacts": [
                    {
                        "type": "workspace_file",
                        "task_frame_id": task_frame_id,
                        "path": artifact_path,
                        "display_name": "最终结果.txt",
                        "size": 13,
                    }
                ]
            },
        )
    )
    db.commit()
    return user


async def _read_response_body(response) -> bytes:
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    if response.background is not None:
        await response.background()
    return b"".join(chunks)


def test_downloads_only_a_published_file_from_the_exact_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    engine = _test_engine()
    with Session(engine) as db:
        user = _seed_artifact(db, workspace_root=str(default_harness_storage_root()))
        workspace = harness_task_workspace_path(
            tenant_id="tenant_demo",
            session_id="session_demo",
            task_frame_id="task_demo",
        )
        file_path = workspace / "reports" / "result.txt"
        file_path.parent.mkdir(parents=True)
        file_path.write_text("artifact body", encoding="utf-8")
        published = publish_harness_artifacts(
            workspace,
            "task_demo",
            [{"path": "reports/result.txt"}],
            operation="general_skill",
        )
        assistant = db.get(Message, "msg_assistant")
        assert assistant is not None
        assistant.metadata_json = {"harness_artifacts": published}
        db.add(assistant)
        db.commit()

        response = download_harness_artifact(
            "session_demo",
            "task_demo",
            tenant_id="tenant_demo",
            path="reports/result.txt",
            current_user=user,
            db=db,
        )

        assert asyncio.run(_read_response_body(response)) == b"artifact body"
        assert response.headers["content-length"] == "13"
        assert response.headers["content-type"].startswith("text/plain")
        assert response.headers["content-disposition"].startswith(
            'attachment; filename="result.txt"; filename*=UTF-8'
        )
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["etag"] == f'"sha256:{published[0]["sha256"]}"'


def test_download_reads_legacy_workspace_artifact_after_default_root_switch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TaskFrame without a root snapshot must keep serving its old-default artifact."""

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "app-data"))
    engine = _test_engine()
    with Session(engine) as db:
        user = _seed_artifact(db)
        workspace = harness_task_workspace_candidates(
            tenant_id="tenant_demo",
            session_id="session_demo",
            task_frame_id="task_demo",
            db=db,
        )[0]
        assert workspace.is_relative_to(tmp_path / "app-data" / "harness_workspaces")
        file_path = workspace / "reports" / "result.txt"
        file_path.parent.mkdir(parents=True)
        file_path.write_text("legacy artifact", encoding="utf-8")
        published = publish_harness_artifacts(
            workspace,
            "task_demo",
            ["reports/result.txt"],
            operation="general_skill",
        )
        assistant = db.get(Message, "msg_assistant")
        assert assistant is not None
        assistant.metadata_json = {"harness_artifacts": published}
        db.add(assistant)
        db.commit()

        response = download_harness_artifact(
            "session_demo",
            "task_demo",
            tenant_id="tenant_demo",
            path="reports/result.txt",
            current_user=user,
            db=db,
        )

        assert asyncio.run(_read_response_body(response)) == b"legacy artifact"


def test_public_download_checks_legacy_artifact_integrity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public artifact endpoint must validate the same legacy file digest as chat downloads."""

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "app-data"))
    engine = _test_engine()
    with Session(engine) as db:
        user = _seed_artifact(db)
        db.add(
            APIJob(
                id="run_demo",
                tenant_id="tenant_demo",
                credential_id="credential_demo",
                kind="run",
                session_id="session_demo",
            )
        )
        workspace = harness_task_workspace_candidates(
            tenant_id="tenant_demo",
            session_id="session_demo",
            task_frame_id="task_demo",
            db=db,
        )[0]
        file_path = workspace / "reports" / "result.txt"
        file_path.parent.mkdir(parents=True)
        file_path.write_text("legacy artifact", encoding="utf-8")
        published = publish_harness_artifacts(
            workspace,
            "task_demo",
            ["reports/result.txt"],
            operation="general_skill",
        )
        assistant = db.get(Message, "msg_assistant")
        assert assistant is not None
        assistant.metadata_json = {
            "client_turn_id": "run_demo",
            "harness_artifacts": published,
        }
        db.add(assistant)
        db.commit()
        principal = PublicPrincipal(
            tenant_id="tenant_demo",
            actor_user=user,
            scopes=frozenset({"runs:read"}),
        )

        response = download_run_artifact(
            "run_demo",
            "task_demo",
            path="reports/result.txt",
            principal=principal,
            db=db,
        )
        assert asyncio.run(_read_response_body(response)) == b"legacy artifact"

        file_path.write_text("tampered", encoding="utf-8")
        with pytest.raises(PublicAPIError) as changed:
            download_run_artifact(
                "run_demo",
                "task_demo",
                path="reports/result.txt",
                principal=principal,
                db=db,
            )

        assert changed.value.status_code == 409
        assert changed.value.code == "ARTIFACT_CHANGED"


def test_download_rejects_conflicting_snapshot_and_legacy_artifact_locations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two candidate roots with the same published path must fail closed instead of choosing one."""

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "app-data"))
    engine = _test_engine()
    snapshot_root = tmp_path / "snapshot-workspace"
    with Session(engine) as db:
        user = _seed_artifact(db, workspace_root=str(snapshot_root))
        db.add(
            APIJob(
                id="run_conflict",
                tenant_id="tenant_demo",
                credential_id="credential_demo",
                kind="run",
                session_id="session_demo",
            )
        )
        snapshot_workspace, legacy_workspace = harness_task_workspace_candidates(
            tenant_id="tenant_demo",
            session_id="session_demo",
            task_frame_id="task_demo",
            db=db,
        )
        snapshot_file = snapshot_workspace / "reports" / "result.txt"
        snapshot_file.parent.mkdir(parents=True)
        snapshot_file.write_text("snapshot artifact", encoding="utf-8")
        published = publish_harness_artifacts(
            snapshot_workspace,
            "task_demo",
            ["reports/result.txt"],
            operation="general_skill",
        )
        legacy_file = legacy_workspace / "reports" / "result.txt"
        legacy_file.parent.mkdir(parents=True)
        legacy_file.write_text("legacy artifact", encoding="utf-8")
        assistant = db.get(Message, "msg_assistant")
        assert assistant is not None
        assistant.metadata_json = {
            "client_turn_id": "run_conflict",
            "harness_artifacts": published,
        }
        db.add(assistant)
        db.commit()
        principal = PublicPrincipal(
            tenant_id="tenant_demo",
            actor_user=user,
            scopes=frozenset({"runs:read"}),
        )

        with pytest.raises(HTTPException) as chat_conflict:
            download_harness_artifact(
                "session_demo",
                "task_demo",
                tenant_id="tenant_demo",
                path="reports/result.txt",
                current_user=user,
                db=db,
            )
        assert chat_conflict.value.status_code == 409

        with pytest.raises(PublicAPIError) as public_conflict:
            download_run_artifact(
                "run_conflict",
                "task_demo",
                path="reports/result.txt",
                principal=principal,
                db=db,
            )
        assert public_conflict.value.status_code == 409
        assert public_conflict.value.code == "ARTIFACT_LOCATION_CONFLICT"


def test_publisher_builds_relative_hashed_metadata(tmp_path: Path) -> None:
    workspace = tmp_path / "task"
    artifact = workspace / "outputs" / "report.csv"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("a,b\n1,2\n", encoding="utf-8")

    published = publish_harness_artifacts(
        workspace.resolve(),
        "task_demo",
        [{"path": "outputs/report.csv"}],
        operation="general_skill",
    )

    assert published == [
        {
            "type": "workspace_file",
            "task_frame_id": "task_demo",
            "path": "outputs/report.csv",
            "sha256": "492d5ea496056f1a6a6592241032fab764c321596317930b4fa0e1e8bc3b7470",
            "size": 8,
            "operation": "general_skill",
        }
    ]


def test_command_artifact_discovery_publishes_only_created_or_modified_files(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "task").resolve()
    workspace.mkdir()
    unchanged = workspace / "input.txt"
    changed = workspace / "changed.txt"
    unchanged.write_text("input", encoding="utf-8")
    changed.write_text("before", encoding="utf-8")
    before = snapshot_harness_workspace(workspace)

    changed.write_text("after command", encoding="utf-8")
    (workspace / "heart.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    published = publish_changed_harness_artifacts(
        workspace,
        "task_demo",
        before,
    )

    assert [item["path"] for item in published] == ["changed.txt", "heart.png"]
    assert {item["operation"] for item in published} == {"exec_command"}


def test_command_artifact_discovery_rejects_oversized_workspaces(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "task").resolve()
    workspace.mkdir()
    (workspace / "one.txt").write_text("1", encoding="utf-8")
    (workspace / "two.txt").write_text("2", encoding="utf-8")

    with pytest.raises(HarnessArtifactAccessError, match="entry limit"):
        snapshot_harness_workspace(workspace, max_entries=1)


def test_windows_artifact_open_does_not_require_posix_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = (tmp_path / "task").resolve()
    workspace.mkdir()
    (workspace / "result.txt").write_text("ok", encoding="utf-8")
    monkeypatch.setattr("app.harness.artifacts.sys.platform", "win32")

    opened = open_harness_artifact(workspace, "result.txt")
    try:
        assert opened.sha256() == "2689367b205c16ce32ed4200942b8b8b1e262dfc70d9bc9fbc77c49699a4f1df"
    finally:
        opened.close()


def test_opened_artifact_reader_preserves_descriptor_offset(tmp_path: Path) -> None:
    workspace = (tmp_path / "task").resolve()
    workspace.mkdir()
    payload = b"the original artifact bytes"
    (workspace / "result.txt").write_bytes(payload)

    opened = open_harness_artifact(workspace, "result.txt")
    try:
        with opened.open_reader() as reader:
            assert reader.read(3) == payload[:3]
        assert b"".join(opened.iter_bytes()) == payload
    finally:
        opened.close()


def test_publisher_rejects_hard_linked_files(tmp_path: Path) -> None:
    workspace = (tmp_path / "task").resolve()
    workspace.mkdir()
    source = tmp_path / "source.txt"
    source.write_text("private", encoding="utf-8")
    try:
        (workspace / "report.txt").hardlink_to(source)
    except OSError:
        pytest.skip("hard links are unavailable on this filesystem")

    with pytest.raises(HarnessArtifactAccessError, match="hard links"):
        publish_harness_artifacts(workspace, "task_demo", ["report.txt"])


def test_download_rejects_a_file_changed_after_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    engine = _test_engine()
    with Session(engine) as db:
        user = _seed_artifact(db, workspace_root=str(default_harness_storage_root()))
        workspace = harness_task_workspace_path(
            tenant_id="tenant_demo",
            session_id="session_demo",
            task_frame_id="task_demo",
        )
        file_path = workspace / "reports" / "result.txt"
        file_path.parent.mkdir(parents=True)
        file_path.write_text("changed body", encoding="utf-8")

        with pytest.raises(HTTPException) as changed:
            download_harness_artifact(
                "session_demo",
                "task_demo",
                tenant_id="tenant_demo",
                path="reports/result.txt",
                current_user=user,
                db=db,
            )
        assert changed.value.status_code == 409


def test_download_requires_session_owner_frame_scope_and_published_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    engine = _test_engine()
    with Session(engine) as db:
        user = _seed_artifact(db)
        intruder = User(
            id="user_intruder",
            tenant_id="tenant_demo",
            username="intruder",
            password_hash="test",
        )
        db.add(intruder)
        workspace = harness_task_workspace_path(
            tenant_id="tenant_demo",
            session_id="session_demo",
            task_frame_id="task_demo",
        )
        workspace.mkdir(parents=True)
        (workspace / "unpublished.txt").write_text("secret", encoding="utf-8")
        db.commit()

        with pytest.raises(HTTPException) as unreadable:
            download_harness_artifact(
                "session_demo",
                "task_demo",
                tenant_id="tenant_demo",
                path="reports/result.txt",
                current_user=intruder,
                db=db,
            )
        assert unreadable.value.status_code == 404

        with pytest.raises(HTTPException) as wrong_frame:
            download_harness_artifact(
                "session_demo",
                "task_other",
                tenant_id="tenant_demo",
                path="reports/result.txt",
                current_user=user,
                db=db,
            )
        assert wrong_frame.value.status_code == 404

        with pytest.raises(HTTPException) as unpublished:
            download_harness_artifact(
                "session_demo",
                "task_demo",
                tenant_id="tenant_demo",
                path="unpublished.txt",
                current_user=user,
                db=db,
            )
        assert unpublished.value.status_code == 404

        with pytest.raises(HTTPException) as tenant_mismatch:
            download_harness_artifact(
                "session_demo",
                "task_demo",
                tenant_id="tenant_other",
                path="reports/result.txt",
                current_user=user,
                db=db,
            )
        assert tenant_mismatch.value.status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        "/tmp/outside.txt",
        r"C:\outside.txt",
        "reports/../../outside.txt",
        ".harness-trash/deleted.txt",
        "\x00bad.txt",
    ],
)
def test_artifact_path_normalization_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(HarnessArtifactAccessError):
        normalize_harness_artifact_path(path)


def test_secure_open_rejects_file_and_parent_symlinks(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    (external / "secret.txt").write_text("outside", encoding="utf-8")
    (workspace / "linked-file.txt").symlink_to(external / "secret.txt")
    (workspace / "linked-directory").symlink_to(
        external,
        target_is_directory=True,
    )

    with pytest.raises(HarnessArtifactAccessError):
        open_harness_artifact(workspace, "linked-file.txt")
    with pytest.raises(HarnessArtifactAccessError):
        open_harness_artifact(workspace, "linked-directory/secret.txt")
    assert (external / "secret.txt").read_text(encoding="utf-8") == "outside"
