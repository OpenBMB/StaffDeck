from pathlib import Path

import pytest

from app import paths
from app.core.harness_session_cleanup import harness_path_segment
from app.session.attachment_store import (
    _attachment_directory,
    read_staged_chat_attachment,
    stage_chat_attachment,
)
from app.session.session_schema import ChatAttachmentRead


def test_malicious_attachment_identifiers_remain_inside_storage_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    raw = b"attachment data"
    attachment = ChatAttachmentRead(
        id="../../outside/attachment",
        filename="report.txt",
        content_type="text/plain",
        size=len(raw),
        kind="text",
    )

    staged = stage_chat_attachment(
        attachment,
        raw,
        tenant_id="../../outside/tenant",
        user_id="..\\..\\outside-user",
    )
    directory = _attachment_directory(
        tenant_id="../../outside/tenant",
        user_id="..\\..\\outside-user",
        attachment_id=attachment.id,
    )
    root = (paths.user_data_dir().resolve() / "harness_uploads").resolve()

    assert directory.is_relative_to(root)
    assert read_staged_chat_attachment(
        staged,
        tenant_id="../../outside/tenant",
        user_id="..\\..\\outside-user",
    ) == raw


def test_attachment_directory_rejects_intermediate_symlink_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    root = paths.user_data_dir().resolve() / "harness_uploads"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / harness_path_segment("tenant")).symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes storage root"):
        _attachment_directory(
            tenant_id="tenant",
            user_id="user",
            attachment_id="attachment",
        )
