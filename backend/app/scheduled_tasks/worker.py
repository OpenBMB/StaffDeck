from __future__ import annotations

import argparse
import signal
import threading
from time import sleep

from sqlmodel import Session

from app.db import engine, init_db
from app.db.seed import seed_demo_data
from app.redis_client import get_redis
from app.redis_lock import try_lock
from app.scheduled_tasks.service import (
    LEASE_SECONDS,
    WORKER_SLEEP_SECONDS,
    due_scheduled_tasks,
    execute_scheduled_task,
)


_stopped = False
_background_thread: threading.Thread | None = None


def _handle_stop(_signum: int, _frame: object) -> None:
    global _stopped
    _stopped = True


def run_worker(*, once: bool = False, poll_seconds: float = WORKER_SLEEP_SECONDS) -> None:
    init_db()
    with Session(engine) as db:
        seed_demo_data(db)
    while not _stopped:
        with Session(engine) as db:
            due = due_scheduled_tasks(db)
            for task in due:
                # 多副本防重执行：DB 租约（due_scheduled_tasks 内的条件更新）解决
                # 领取竞争；这里的 Redis 执行锁兜底「执行超过租期 / 崩溃后租约
                # 过期被其他副本重复领取」。Redis 不可用 → 直接执行（单副本语义）。
                lock = try_lock(f"scheduled-task:{task.id}", ttl_seconds=LEASE_SECONDS)
                if lock is None and get_redis() is not None:
                    # 其他副本正在执行该任务，跳过
                    continue
                try:
                    execute_scheduled_task(db, task)
                finally:
                    if lock is not None:
                        lock.release()
        if once:
            return
        sleep(max(1.0, poll_seconds))


def start_background_worker(*, poll_seconds: float = WORKER_SLEEP_SECONDS) -> None:
    global _background_thread, _stopped
    if _background_thread and _background_thread.is_alive():
        return
    _stopped = False
    _background_thread = threading.Thread(
        target=run_worker,
        kwargs={"once": False, "poll_seconds": poll_seconds},
        name="ultrarag-scheduled-task-worker",
        daemon=True,
    )
    _background_thread.start()


def stop_background_worker() -> None:
    global _stopped
    _stopped = True


def main() -> None:
    parser = argparse.ArgumentParser(description="Run StaffDeck scheduled task worker")
    parser.add_argument("--once", action="store_true", help="scan and execute due tasks once, then exit")
    parser.add_argument("--poll-seconds", type=float, default=WORKER_SLEEP_SECONDS)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    run_worker(once=args.once, poll_seconds=args.poll_seconds)


if __name__ == "__main__":
    main()
