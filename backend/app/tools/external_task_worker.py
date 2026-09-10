from __future__ import annotations

import threading

from sqlmodel import Session

from app.config import get_settings
from app.db import engine
from app.tools.external_tasks import poll_due_external_tasks

_stop_event = threading.Event()
_thread: threading.Thread | None = None


def run_external_task_worker() -> None:
    while not _stop_event.is_set():
        with Session(engine) as db:
            poll_due_external_tasks(db)
        _stop_event.wait(max(0.5, get_settings().external_task_poll_seconds))


def start_external_task_worker() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(
        target=run_external_task_worker,
        name="staffdeck-external-task-worker",
        daemon=True,
    )
    _thread.start()


def stop_external_task_worker() -> None:
    _stop_event.set()
