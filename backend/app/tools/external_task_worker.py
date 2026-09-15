from __future__ import annotations

import threading
import logging
import math

from app.config import get_settings
from app.tools.external_tasks import poll_due_external_tasks
from app.tools.external_task_dispatch import ExternalTaskDispatcher

_stop_event = threading.Event()
_thread: threading.Thread | None = None
_dispatcher: ExternalTaskDispatcher | None = None
_lifecycle_lock = threading.RLock()


def run_external_task_worker() -> None:
    global _dispatcher
    dispatcher = ExternalTaskDispatcher()
    with _lifecycle_lock:
        _dispatcher = dispatcher
    try:
        while not _stop_event.is_set():
            interval = 5.0
            try:
                from staffdeck_harness.runtime.services import maintenance_sessions
                configured = float(get_settings().external_task_poll_seconds)
                if math.isfinite(configured):
                    interval = max(0.5, configured)
                for db in maintenance_sessions():
                    if _stop_event.is_set():
                        break
                    try:
                        poll_due_external_tasks(db, dispatch=dispatcher.dispatch)
                    except Exception:
                        # Do not clear task leases or resubmit uncertain POSTs. The
                        # existing tracker owns safe recovery on the next pass.
                        logging.getLogger(__name__).exception("external task session maintenance failed")
                        db.rollback()
            except Exception:
                logging.getLogger(__name__).exception("external task maintenance failed")
            _stop_event.wait(interval)
    finally:
        dispatcher.close()
        with _lifecycle_lock:
            if _dispatcher is dispatcher:
                _dispatcher = None


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


def stop_external_task_worker() -> None:
    _stop_event.set()
    with _lifecycle_lock:
        dispatcher, thread = _dispatcher, _thread
    if dispatcher is not None:
        dispatcher.close()
    if thread is not None and thread is not threading.current_thread():
        thread.join()
