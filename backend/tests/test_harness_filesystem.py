from __future__ import annotations

import hashlib
from pathlib import Path

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


@pytest.mark.parametrize(
    ("path", "expected_code"),
    [
        ("../outside.txt", "INVALID_PATH"),
        ("/tmp/outside.txt", "INVALID_PATH"),
        ("C:\\outside.txt", "INVALID_PATH"),
        (".harness-trash/secret", "RESERVED_PATH"),
    ],
)
def test_all_paths_are_confined_to_workspace(
    tmp_path: Path,
    path: str,
    expected_code: str,
) -> None:
    executor, context = _harness(tmp_path)

    code = _execute_failure(
        executor,
        context,
        "write_file",
        {"path": path, "content": "denied", "create_parents": True},
    )

    assert code == expected_code
    assert not (tmp_path / "outside.txt").exists()


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
    second = _execute(
        executor,
        context,
        "read_file",
        {"path": "unicode.txt", "offset": first["next_offset"], "max_bytes": 6},
    )
    assert second["content"] == "好吗"
    assert second["truncated"] is False

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
