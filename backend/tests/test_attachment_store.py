import json
from pathlib import Path

import pytest

from app import paths
from app.session.attachment_store import (
    _attachment_directory,
    _attachment_storage_key,
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
    relative_parts = directory.relative_to(root).parts
    storage_key = _attachment_storage_key(
        tenant_id="../../outside/tenant",
        user_id="..\\..\\outside-user",
        attachment_id=attachment.id,
    )

    assert directory.is_relative_to(root)
    assert relative_parts == (storage_key[:2], storage_key)
    assert all(part and set(part) <= set("0123456789abcdef") for part in relative_parts)
    assert all(
        raw not in str(directory)
        for raw in (attachment.id, "outside/tenant", "outside-user")
    )
    assert read_staged_chat_attachment(
        staged,
        tenant_id="../../outside/tenant",
        user_id="..\\..\\outside-user",
    ) == raw
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["tenant_id"] == "../../outside/tenant"
    assert metadata["user_id"] == "..\\..\\outside-user"
    assert metadata["attachment_id"] == attachment.id

    metadata["user_id"] = "different-user"
    (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    assert read_staged_chat_attachment(
        staged,
        tenant_id="../../outside/tenant",
        user_id="..\\..\\outside-user",
    ) is None


def test_attachment_directory_rejects_intermediate_symlink_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    root = paths.user_data_dir().resolve() / "harness_uploads"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    key = _attachment_storage_key(
        tenant_id="tenant",
        user_id="user",
        attachment_id="attachment",
    )
    (root / key[:2]).symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes storage root"):
        _attachment_directory(
            tenant_id="tenant",
            user_id="user",
            attachment_id="attachment",
        )
