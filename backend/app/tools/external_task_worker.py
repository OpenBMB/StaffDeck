from __future__ import annotations

import threading
import logging

from sqlmodel import Session

from app.config import get_settings
from app.db import engine
from app.tools.external_tasks import poll_due_external_tasks

_stop_event = threading.Event()
_thread: threading.Thread | None = None
_lifecycle_lock = threading.Lock()
logger = logging.getLogger(__name__)


def run_external_task_worker() -> None:
    while not _stop_event.is_set():
        try:
            with Session(engine) as db:
                poll_due_external_tasks(db)
        except Exception:
            # A DB/receipt error must not permanently disable all subsequent tools.
            # Session closes/rolls back here; the next pass uses a fresh transaction.
            logger.exception("External task worker pass failed; retrying next pass")
        _stop_event.wait(max(0.5, get_settings().external_task_poll_seconds))


def start_external_task_worker() -> None:
    global _thread
    with _lifecycle_lock:
        if _thread and _thread.is_alive():
            return
        _stop_event.clear()
        _thread = threading.Thread(
            target=run_external_task_worker,
            name="staffdeck-external-task-worker",
            daemon=True,
        )
        _thread.start()


def stop_external_task_worker(timeout_seconds: float = 5.0) -> bool:
    global _thread
    with _lifecycle_lock:
        _stop_event.set()
        if _thread and _thread.is_alive():
            _thread.join(timeout=max(0.0, timeout_seconds))
        if _thread and _thread.is_alive():
            return False
        _thread = None
        return True
