from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.chat import download_harness_artifact
from app.core.harness_session_cleanup import harness_task_workspace_path
from app.db.models import (
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
)


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
                        "size": 12,
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
        user = _seed_artifact(db)
        workspace = harness_task_workspace_path(
            tenant_id="tenant_demo",
            session_id="session_demo",
            task_frame_id="task_demo",
        )
        file_path = workspace / "reports" / "result.txt"
        file_path.parent.mkdir(parents=True)
        file_path.write_text("artifact body", encoding="utf-8")

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
            'attachment; filename="txt"; filename*=UTF-8'
        )
        assert "%E6%9C%80%E7%BB%88%E7%BB%93%E6%9E%9C.txt" in response.headers[
            "content-disposition"
        ]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["etag"].startswith('"sha256:')


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
