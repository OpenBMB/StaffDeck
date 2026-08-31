import logging
import queue
import sys
from pathlib import Path

from app import runtime_logging


def test_runtime_log_path_uses_platform_user_data_directory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))

    assert runtime_logging.runtime_log_path() == tmp_path / "logs" / "staffdeck.log"


def test_runtime_log_path_does_not_follow_the_harness_workspace_root(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Runtime logs must remain in application data when Harness uses a separate home workspace."""

    home = tmp_path / "home"
    app_data = tmp_path / "app-data"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(app_data))

    assert runtime_logging.runtime_log_path() == app_data / "logs" / "staffdeck.log"
    assert not (home / ".staffdeck" / "workspaces").exists()


def test_runtime_queue_drops_instead_of_blocking_when_full() -> None:
    record_queue: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=1)
    handler = runtime_logging._DroppingQueueHandler(record_queue)
    record = logging.makeLogRecord({"msg": "diagnostic"})

    handler.enqueue(record)
    handler.enqueue(record)

    assert record_queue.qsize() == 1
    assert handler.dropped_records == 1


def test_runtime_logging_writes_and_rotates_diagnostics(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(runtime_logging, "_configured_path", None)
    monkeypatch.setattr(runtime_logging, "MAX_LOG_BYTES", 256)

    try:
        log_path = runtime_logging.configure_runtime_logging()
        logger = logging.getLogger("staffdeck.runtime")
        for index in range(20):
            logger.warning("diagnostic-test-message-%s-%s", index, "x" * 40)
        logging.getLogger("app.api.chat").error("user-asset-must-not-be-persisted")
        runtime_logging.shutdown_runtime_logging()

        assert log_path.exists()
        assert (log_path.parent / "staffdeck.log.1").exists()
        combined_logs = "".join(
            path.read_text(encoding="utf-8")
            for path in sorted(log_path.parent.glob("staffdeck.log*"))
        )
        assert "diagnostic-test-message" in combined_logs
        assert "user-asset-must-not-be-persisted" not in combined_logs
    finally:
        runtime_logging.shutdown_runtime_logging()


def test_runtime_logging_does_not_attach_to_root_logger(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(runtime_logging, "_configured_path", None)

    try:
        runtime_logging.configure_runtime_logging()

        assert not any(
            isinstance(handler, runtime_logging._DroppingQueueHandler)
            for handler in logging.getLogger().handlers
        )
    finally:
        runtime_logging.shutdown_runtime_logging()


def test_uncaught_exception_log_excludes_exception_message(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(runtime_logging, "_configured_path", None)
    sensitive_value = "customer-secret-value-must-not-be-logged"

    try:
        log_path = runtime_logging.configure_runtime_logging()
        try:
            raise ValueError(sensitive_value)
        except ValueError:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            sys.excepthook(exc_type, exc_value, exc_traceback)
        runtime_logging.shutdown_runtime_logging()

        log_content = log_path.read_text(encoding="utf-8")
        assert "type=ValueError" in log_content
        assert sensitive_value not in log_content
        assert str(Path(__file__).resolve().parent) not in log_content
    finally:
        runtime_logging.shutdown_runtime_logging()
