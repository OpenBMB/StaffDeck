from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from app.harness import (
    HarnessExecutor,
    HarnessLimits,
    HarnessToolCall,
    HarnessToolContext,
    build_file_tool_registry,
)


def test_write_read_edit_and_info_are_hash_guarded_and_atomic(tmp_path: Path) -> None:
    executor, context = _harness(tmp_path)

    written = _execute(
        executor,
        context,
        "write_file",
        {"path": "notes/todo.txt", "content": "alpha\nbeta\n", "create_parents": True},
    )
    first_hash = written["sha256"]
    assert first_hash == hashlib.sha256(b"alpha\nbeta\n").hexdigest()
    assert written["created"] is True

    read = _execute(executor, context, "read_file", {"path": "notes/todo.txt"})
    assert read["content"] == "alpha\nbeta\n"
    assert read["sha256"] == first_hash
    assert read["truncated"] is False
    overwritten = _execute(
        executor,
        context,
        "write_file",
        {
            "path": "notes/todo.txt",
            "content": "alpha\nbeta\n",
            "expected_sha256": first_hash,
        },
    )
    assert overwritten["created"] is False
    assert overwritten["previous_sha256"] == first_hash

    mismatch = _execute_failure(
        executor,
        context,
        "edit_file",
        {
            "path": "notes/todo.txt",
            "old_text": "beta",
            "new_text": "done",
            "expected_sha256": "0" * 64,
        },
    )
    assert mismatch == "HASH_MISMATCH"
    assert (context.workspace_root / "notes" / "todo.txt").read_text() == "alpha\nbeta\n"

    edited = _execute(
        executor,
        context,
        "edit_file",
        {
            "path": "notes/todo.txt",
            "old_text": "beta",
            "new_text": "done",
            "expected_sha256": first_hash,
        },
    )
    assert edited["previous_sha256"] == first_hash
    assert edited["replacements"] == 1
    assert not list((context.workspace_root / "notes").glob(".*.tmp"))

    info = _execute(executor, context, "file_info", {"path": "notes/todo.txt"})
    assert info["type"] == "file"
    assert info["size"] == len("alpha\ndone\n")
    assert info["sha256"] == edited["sha256"]


def test_artifacts_are_published_explicitly_with_safe_metadata(tmp_path: Path) -> None:
    executor, context = _harness(tmp_path)
    _execute(
        executor,
        context,
        "write_file",
        {"path": "work/intermediate.txt", "content": "internal", "create_parents": True},
    )
    final = _execute(
        executor,
        context,
        "write_file",
        {"path": "reports/result.txt", "content": "ready", "create_parents": True},
    )

    published = _execute(
        executor,
        context,
        "publish_artifact",
        {
            "path": "reports/result.txt",
            "display_name": "最终报告\n.txt",
            "description": "给用户下载的结果",
        },
    )

    assert published == {
        "path": "reports/result.txt",
        "display_name": "最终报告.txt",
        "description": "给用户下载的结果",
        "size": 5,
        "sha256": final["sha256"],
        "content_type": "text/plain",
    }
    assert _execute_failure(
        executor,
        context,
        "publish_artifact",
        {"path": "reports/missing.txt"},
    ) == "NOT_FOUND"


def test_extract_document_text_persists_docx_for_bounded_reading(tmp_path: Path) -> None:
    executor, context = _harness(tmp_path)
    context.workspace_root.mkdir(parents=True)
    document = BytesIO()
    with ZipFile(document, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
              <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
              <Default Extension="xml" ContentType="application/xml"/>
              <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
            </Types>""",
        )
        archive.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
            </Relationships>""",
        )
        archive.writestr(
            "word/document.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
            <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
              <w:body><w:p><w:r><w:t>合同审查内容</w:t></w:r></w:p></w:body>
            </w:document>""",
        )
    source = context.workspace_root / "attachments" / "contract.docx"
    source.parent.mkdir(parents=True)
    source.write_bytes(document.getvalue())

    extracted = _execute(
        executor,
        context,
        "extract_document_text",
        {"path": "/workspace/attachments/contract.docx"},
    )

    assert extracted["source_path"] == "attachments/contract.docx"
    assert extracted["extracted_text_path"] == (
        "attachments/contract.docx.extracted.txt"
    )
    assert extracted["format"] == "docx"
    read = _execute(
        executor,
        context,
        "read_file",
        {"path": extracted["extracted_text_path"]},
    )
    assert "合同审查内容" in str(read["content"])


def test_extract_document_text_rejects_unsupported_binary(tmp_path: Path) -> None:
    executor, context = _harness(tmp_path)
    context.workspace_root.mkdir(parents=True)
    (context.workspace_root / "unknown.bin").write_bytes(b"\x00\x01")

    error = _execute_failure(
        executor,
        context,
        "extract_document_text",
        {"path": "unknown.bin"},
    )

    assert error == "DOCUMENT_EXTRACTION_FAILED"


def test_harness_internal_trash_remains_reserved(tmp_path: Path) -> None:
    executor, context = _harness(tmp_path)

    code = _execute_failure(
        executor,
        context,
        "write_file",
        {"path": ".harness-trash/secret", "content": "denied", "create_parents": True},
    )

    assert code == "RESERVED_PATH"


def test_typed_file_tools_accept_absolute_and_parent_paths(tmp_path: Path) -> None:
    executor, context = _harness(tmp_path)
    context.workspace_root.mkdir(parents=True)
    outside = tmp_path / "outside.txt"

    written = _execute(
        executor,
        context,
        "write_file",
        {"path": "../outside.txt", "content": "alpha\n", "create_parents": True},
    )
    assert written["path"] == str(outside)
    assert outside.read_text(encoding="utf-8") == "alpha\n"

    read = _execute(executor, context, "read_file", {"path": str(outside)})
    assert read["content"] == "alpha\n"
    edited = _execute(
        executor,
        context,
        "edit_file",
        {"path": str(outside), "old_text": "alpha", "new_text": "beta"},
    )
    assert edited["path"] == str(outside)

    external_directory = tmp_path / "external"
    _execute(executor, context, "mkdir", {"path": str(external_directory)})
    copied_path = external_directory / "copied.txt"
    _execute(
        executor,
        context,
        "copy_file",
        {"source_path": str(outside), "destination_path": str(copied_path)},
    )
    moved_path = external_directory / "moved.txt"
    _execute(
        executor,
        context,
        "move_file",
        {"source_path": str(copied_path), "destination_path": str(moved_path)},
    )

    listing = _execute(
        executor,
        context,
        "list_directory",
        {"path": str(external_directory)},
    )
    assert [entry["path"] for entry in listing["entries"]] == [str(moved_path)]
    matches = _execute(
        executor,
        context,
        "glob",
        {"path": str(external_directory), "pattern": "*.txt"},
    )
    assert [entry["path"] for entry in matches["matches"]] == [str(moved_path)]
    grep = _execute(
        executor,
        context,
        "grep",
        {"path": str(external_directory), "pattern": "beta"},
    )
    assert grep["matches"][0]["path"] == str(moved_path)
    info = _execute(executor, context, "file_info", {"path": str(moved_path)})
    assert info["type"] == "file"

    deleted = _execute(executor, context, "delete_file", {"path": str(moved_path)})
    assert deleted["path"] == str(moved_path)
    assert deleted["recoverable"] is True
    assert not moved_path.exists()


def test_external_file_can_be_staged_as_downloadable_artifact(tmp_path: Path) -> None:
    executor, context = _harness(tmp_path)
    external = tmp_path / "outside-report.txt"
    external.write_text("ready", encoding="utf-8")

    published = _execute(
        executor,
        context,
        "publish_artifact",
        {"path": str(external), "display_name": "report.txt"},
    )

    staged = context.workspace_root / str(published["path"])
    assert staged.read_text(encoding="utf-8") == "ready"
    assert published["display_name"] == "report.txt"


def test_symlink_targets_and_symlink_ancestors_are_rejected(tmp_path: Path) -> None:
    executor, context = _harness(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    context.workspace_root.mkdir()
    try:
        (context.workspace_root / "linked-file").symlink_to(outside / "secret.txt")
        (context.workspace_root / "linked-dir").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable on this platform")

    direct = _execute_failure(
        executor,
        context,
        "read_file",
        {"path": "linked-file"},
    )
    ancestor = _execute_failure(
        executor,
        context,
        "write_file",
        {"path": "linked-dir/new.txt", "content": "denied"},
    )

    assert direct == "SYMLINK_NOT_ALLOWED"
    assert ancestor == "SYMLINK_NOT_ALLOWED"
    assert not (outside / "new.txt").exists()


def test_soft_delete_rejects_a_symlinked_internal_trash(tmp_path: Path) -> None:
    executor, context = _harness(tmp_path)
    _execute(
        executor,
        context,
        "write_file",
        {"path": "delete-me.txt", "content": "keep inside"},
    )
    outside = tmp_path / "outside-trash"
    outside.mkdir()
    try:
        (context.workspace_root / ".harness-trash").symlink_to(
            outside,
            target_is_directory=True,
        )
    except OSError:
        pytest.skip("symbolic links are unavailable on this platform")

    code = _execute_failure(
        executor,
        context,
        "delete_file",
        {"path": "delete-me.txt"},
    )

    assert code == "SYMLINK_NOT_ALLOWED"
    assert (context.workspace_root / "delete-me.txt").read_text() == "keep inside"
    assert not list(outside.rglob("*"))


def test_list_glob_and_grep_are_bounded_and_do_not_follow_symlinks(tmp_path: Path) -> None:
    executor, context = _harness(tmp_path)
    _execute(executor, context, "mkdir", {"path": "src/nested"})
    _execute(
        executor,
        context,
        "write_file",
        {"path": "src/a.py", "content": "print('needle')\n"},
    )
    _execute(
        executor,
        context,
        "write_file",
        {"path": "src/nested/b.py", "content": "# needle\n"},
    )
    _execute(
        executor,
        context,
        "write_file",
        {"path": "src/nested/c.txt", "content": "ignore\n"},
    )

    listed = _execute(
        executor,
        context,
        "list_directory",
        {"path": "src", "recursive": True, "max_entries": 2},
    )
    assert len(listed["entries"]) == 2
    assert listed["truncated"] is True

    globbed = _execute(
        executor,
        context,
        "glob",
        {"path": "src", "pattern": "**/*.py"},
    )
    assert [item["path"] for item in globbed["matches"]] == [
        "src/a.py",
        "src/nested/b.py",
    ]

    grepped = _execute(
        executor,
        context,
        "grep",
        {"path": "src", "pattern": "needle", "file_glob": "**/*.py"},
    )
    assert [(item["path"], item["line"]) for item in grepped["matches"]] == [
        ("src/a.py", 1),
        ("src/nested/b.py", 1),
    ]
    assert grepped["scanned_files"] == 2


def test_grep_rejects_untrusted_regular_expressions(tmp_path: Path) -> None:
    executor, context = _harness(tmp_path)
    error_code = _execute_failure(
        executor,
        context,
        "grep",
        {
            "path": ".",
            "pattern": "(a+)+$",
            "regex": True,
        },
    )

    assert error_code == "INVALID_ARGUMENTS"


def test_copy_move_and_soft_delete_preserve_recovery_and_hashes(tmp_path: Path) -> None:
    executor, context = _harness(tmp_path)
    source = _execute(
        executor,
        context,
        "write_file",
        {"path": "source.txt", "content": "payload"},
    )

    copied = _execute(
        executor,
        context,
        "copy_file",
        {
            "source_path": "source.txt",
            "destination_path": "archive/copy.txt",
            "create_parents": True,
            "expected_sha256": source["sha256"],
        },
    )
    assert copied["sha256"] == source["sha256"]

    moved = _execute(
        executor,
        context,
        "move_file",
        {
            "source_path": "archive/copy.txt",
            "destination_path": "moved.txt",
            "expected_sha256": source["sha256"],
        },
    )
    assert moved["sha256"] == source["sha256"]
    assert not (context.workspace_root / "archive" / "copy.txt").exists()

    deleted = _execute(
        executor,
        context,
        "delete_file",
        {"path": "moved.txt", "expected_sha256": source["sha256"]},
    )
    assert deleted["recoverable"] is True
    assert not (context.workspace_root / "moved.txt").exists()
    trash_matches = list(
        (context.workspace_root / ".harness-trash" / deleted["trash_id"]).rglob("moved.txt")
    )
    assert len(trash_matches) == 1
    assert trash_matches[0].read_text() == "payload"

    root_listing = _execute(executor, context, "list_directory", {"path": "."})
    assert ".harness-trash" not in {item["path"] for item in root_listing["entries"]}
    reserved = _execute_failure(
        executor,
        context,
        "read_file",
        {"path": f".harness-trash/{deleted['trash_id']}/moved.txt"},
    )
    assert reserved == "RESERVED_PATH"


def test_workspace_and_file_quotas_are_enforced_before_write(tmp_path: Path) -> None:
    limits = HarnessLimits(
        max_read_bytes=64,
        max_file_bytes=8,
        max_workspace_bytes=12,
        max_entries=10,
        max_result_bytes=1024,
    )
    executor, context = _harness(tmp_path, limits=limits)

    too_large = _execute_failure(
        executor,
        context,
        "write_file",
        {"path": "large.txt", "content": "x" * 9},
    )
    assert too_large == "FILE_TOO_LARGE"
    _execute(executor, context, "write_file", {"path": "a.txt", "content": "12345678"})
    no_capacity = _execute_failure(
        executor,
        context,
        "copy_file",
        {"source_path": "a.txt", "destination_path": "b.txt"},
    )
    assert no_capacity == "WORKSPACE_QUOTA_EXCEEDED"
    assert not (context.workspace_root / "b.txt").exists()


def test_deleted_files_do_not_consume_workspace_quota(tmp_path: Path) -> None:
    limits = HarnessLimits(
        max_read_bytes=64,
        max_file_bytes=8,
        max_workspace_bytes=8,
        max_entries=10,
        max_result_bytes=1024,
    )
    executor, context = _harness(tmp_path, limits=limits)
    _execute(executor, context, "write_file", {"path": "a.txt", "content": "12345678"})
    _execute(executor, context, "delete_file", {"path": "a.txt"})
    _execute(executor, context, "write_file", {"path": "b.txt", "content": "abcdefgh"})
    assert (context.workspace_root / ".harness-trash").exists()
    assert (context.workspace_root / "b.txt").read_text() == "abcdefgh"


def test_read_chunks_on_utf8_boundaries_and_rejects_binary(tmp_path: Path) -> None:
    limits = HarnessLimits(
        max_read_bytes=8,
        max_file_bytes=32,
        max_workspace_bytes=128,
        max_entries=10,
        max_result_bytes=1024,
    )
    executor, context = _harness(tmp_path, limits=limits)
    _execute(
        executor,
        context,
        "write_file",
        {"path": "unicode.txt", "content": "你好吗"},
    )

    first = _execute(
        executor,
        context,
        "read_file",
        {"path": "unicode.txt", "max_bytes": 4},
    )
    assert first["content"] == "你"
    assert first["next_offset"] == 3
    assert first["continuation_token"]
    second = _execute(
        executor,
        context,
        "read_file",
        {
            "path": "unicode.txt",
            "continuation_token": first["continuation_token"],
            "max_bytes": 6,
        },
    )
    assert second["content"] == "好吗"
    assert second["truncated"] is False
    assert second["eof"] is True

    # Legacy callers may still provide a byte offset. Align a mid-codepoint
    # offset forward rather than misclassifying a valid UTF-8 file as binary.
    aligned = _execute(
        executor,
        context,
        "read_file",
        {"path": "unicode.txt", "offset": 1, "max_bytes": 6},
    )
    assert aligned["offset"] == 3
    assert aligned["content"] == "好吗"

    eof = _execute(
        executor,
        context,
        "read_file",
        {"path": "unicode.txt", "offset": 999},
    )
    assert eof["content"] == ""
    assert eof["eof"] is True
    assert eof["offset"] == eof["size"]

    context.workspace_root.joinpath("binary.bin").write_bytes(b"\xff\xfe")
    binary = _execute_failure(
        executor,
        context,
        "read_file",
        {"path": "binary.bin"},
    )
    assert binary == "UNSUPPORTED_ENCODING"


def test_ambiguous_edit_requires_explicit_replace_all(tmp_path: Path) -> None:
    executor, context = _harness(tmp_path)
    _execute(
        executor,
        context,
        "write_file",
        {"path": "repeat.txt", "content": "same same"},
    )

    ambiguous = _execute_failure(
        executor,
        context,
        "edit_file",
        {"path": "repeat.txt", "old_text": "same", "new_text": "new"},
    )
    assert ambiguous == "AMBIGUOUS_EDIT"
    edited = _execute(
        executor,
        context,
        "edit_file",
        {
            "path": "repeat.txt",
            "old_text": "same",
            "new_text": "new",
            "replace_all": True,
        },
    )
    assert edited["replacements"] == 2
    assert (context.workspace_root / "repeat.txt").read_text() == "new new"


def _harness(
    tmp_path: Path,
    *,
    limits: HarnessLimits | None = None,
) -> tuple[HarnessExecutor, HarnessToolContext]:
    context = HarnessToolContext(
        run_id="run",
        task_frame_id="frame",
        workspace_root=(tmp_path / "workspace").resolve(),
        limits=limits or HarnessLimits(),
    )
    return HarnessExecutor(build_file_tool_registry()), context


def _execute(
    executor: HarnessExecutor,
    context: HarnessToolContext,
    name: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    result = executor.execute(
        context,
        HarnessToolCall(call_id=f"call-{name}", name=name, arguments=arguments),
    )
    assert result.success, result.error
    assert result.data is not None
    return dict(result.data)


def _execute_failure(
    executor: HarnessExecutor,
    context: HarnessToolContext,
    name: str,
    arguments: dict[str, object],
) -> str:
    result = executor.execute(
        context,
        HarnessToolCall(call_id=f"call-{name}", name=name, arguments=arguments),
    )
    assert result.success is False
    assert result.error is not None
    return result.error.code
