from __future__ import annotations

import threading

from app.public_api.jobs import cleanup_public_api_records
from app.public_api.webhooks import enqueue_due_webhook_deliveries


_stop = threading.Event()
_thread: threading.Thread | None = None


def _loop() -> None:
    cleanup_tick = 0
    while not _stop.wait(30):
        enqueue_due_webhook_deliveries()
        cleanup_tick += 1
        if cleanup_tick >= 120:
            cleanup_public_api_records()
            cleanup_tick = 0


def start_public_api_maintenance() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="public-api-maintenance", daemon=True)
    _thread.start()


def stop_public_api_maintenance() -> None:
    global _thread
    _stop.set()
    if _thread:
        _thread.join(timeout=2)
    _thread = None
