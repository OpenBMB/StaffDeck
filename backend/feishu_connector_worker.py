from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.metadata
import json
import os
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

WATCHDOG_EXIT_CODE = 71
PARENT_GONE_EXIT_CODE = 72
LOCKED_EXIT_CODE = 73
SDK_CONTRACT_VERSION = "1.2.0"
PRODUCTION_RUNTIME = "app.channels.feishu_runtime:run_feishu_runtime"
_RUNTIME_ALLOWLIST = {
    PRODUCTION_RUNTIME,
    "feishu_connector_worker:run_sdk_contract_runtime",
    "feishu_connector_worker:run_idle_contract_runtime",
    "feishu_connector_worker:run_parent_watchdog_contract_runtime",
    "feishu_connector_worker:run_unstoppable_contract_runtime",
}


def _user_data_dir() -> Path:
    override = os.environ.get("ULTRARAG_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "StaffDeck"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "StaffDeck"
    return Path.home() / ".local" / "share" / "StaffDeck"


def binding_lock_path(binding_id: str, database_path: Path) -> Path:
    database = database_path.expanduser().resolve()
    database_fingerprint = hashlib.sha256(str(database).encode("utf-8")).hexdigest()[:16]
    binding_fingerprint = hashlib.sha256(binding_id.encode("utf-8")).hexdigest()[:16]
    return (
        database.parent
        / "connector-locks"
        / f"feishu-{database_fingerprint}-{binding_fingerprint}.lock"
    )


class BindingProcessLock:
    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def acquire(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                if handle.read(1) == b"":
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            # A stale/read-only lock file is indistinguishable from an active
            # connector to callers. Treat it as busy so shutdown/reconcile can
            # continue without taking down the application lifecycle.
            if "handle" in locals():
                handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None


@dataclass(frozen=True)
class ConnectorChildSpec:
    binding_id: str
    config_revision: int
    child_nonce: str
    runtime_path: str
    binding_lock_path: str
    database_path: str = ""
    watchdog_seconds: float = 2.5


class ChildControl:
    def __init__(self, spec: ConnectorChildSpec, connection):
        self.spec = spec
        self.connection = connection
        self.stop_requested = threading.Event()
        self.parent_gone = threading.Event()
        self._send_lock = threading.Lock()
        self._callback_lock = threading.Lock()
        self._stop_callbacks: list[Callable[[], None]] = []
        self._thread = threading.Thread(
            target=self._read_commands,
            name=f"feishu-control-{spec.binding_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def add_stop_callback(self, callback: Callable[[], None]) -> None:
        with self._callback_lock:
            if not self.stop_requested.is_set():
                self._stop_callbacks.append(callback)
                return
        if self.stop_requested.is_set():
            callback()

    def join(self, timeout: float = 1.0) -> bool:
        if self._thread is threading.current_thread():
            return False
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    def emit(self, event: str, **payload: Any) -> None:
        message = {
            "event": event,
            "binding_id": self.spec.binding_id,
            "config_revision": self.spec.config_revision,
            "child_nonce": self.spec.child_nonce,
            "pid": os.getpid(),
            "monotonic": time.monotonic(),
            **payload,
        }
        try:
            with self._send_lock:
                self.connection.send(message)
        except (BrokenPipeError, EOFError, OSError):
            self.parent_gone.set()

    def _read_commands(self) -> None:
        while True:
            try:
                message = self.connection.recv()
            except (EOFError, OSError):
                self.parent_gone.set()
                os._exit(PARENT_GONE_EXIT_CODE)
            if not isinstance(message, dict):
                continue
            if message.get("command") != "STOP":
                continue
            if (
                message.get("binding_id") != self.spec.binding_id
                or message.get("config_revision") != self.spec.config_revision
                or message.get("child_nonce") != self.spec.child_nonce
            ):
                continue
            with self._callback_lock:
                if self.stop_requested.is_set():
                    return
                self.stop_requested.set()
                callbacks = list(self._stop_callbacks)
                self._stop_callbacks.clear()
            for callback in callbacks:
                try:
                    callback()
                except Exception:
                    pass
            return


class FrameWatchdog:
    def __init__(
        self,
        control: ChildControl,
        timeout_seconds: float,
        *,
        exit_fn: Callable[[int], None] = os._exit,
    ):
        self.control = control
        self.timeout_seconds = timeout_seconds
        self.exit_fn = exit_fn
        self._condition = threading.Condition()
        self._deadlines: dict[str, float] = {}
        self._stopped = False
        self._thread = threading.Thread(
            target=self._run,
            name=f"feishu-watchdog-{control.spec.binding_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def arm(self, token: str) -> None:
        with self._condition:
            deadline = time.monotonic() + self.timeout_seconds
            self._deadlines[token] = deadline
            self._condition.notify_all()
        self.control.emit(
            "DISPATCH_STARTED",
            frame_token=token,
            deadline_monotonic=deadline,
        )

    def disarm(self, token: str) -> bool:
        with self._condition:
            if token not in self._deadlines:
                return False
            self._deadlines.pop(token, None)
            self._condition.notify_all()
        self.control.emit("DISPATCH_FINISHED", frame_token=token)
        return True

    def stop(self) -> None:
        with self._condition:
            self._stopped = True
            self._condition.notify_all()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._stopped:
                    return
                if not self._deadlines:
                    self._condition.wait()
                    continue
                token, deadline = min(self._deadlines.items(), key=lambda item: item[1])
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self._condition.wait(timeout=remaining)
                    continue
            self.control.emit("WATCHDOG_TIMEOUT", frame_token=token)
            self.exit_fn(WATCHDOG_EXIT_CODE)
            return


def _load_runtime(path: str) -> Callable[[ConnectorChildSpec, ChildControl, FrameWatchdog], None]:
    if path not in _RUNTIME_ALLOWLIST:
        raise ValueError(f"Feishu connector runtime is not allowed: {path!r}")
    module_name, separator, attribute = path.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError(f"invalid Feishu connector runtime: {path!r}")
    runtime = getattr(importlib.import_module(module_name), attribute)
    if not callable(runtime):
        raise TypeError(f"Feishu connector runtime is not callable: {path!r}")
    return runtime


def connector_child_entry(spec: ConnectorChildSpec, connection) -> None:
    lock = BindingProcessLock(Path(spec.binding_lock_path))
    if not lock.acquire():
        try:
            connection.send(
                {
                    "event": "LOCKED",
                    "binding_id": spec.binding_id,
                    "config_revision": spec.config_revision,
                    "child_nonce": spec.child_nonce,
                    "pid": os.getpid(),
                }
            )
        finally:
            connection.close()
        raise SystemExit(LOCKED_EXIT_CODE)

    control = ChildControl(spec, connection)
    watchdog = FrameWatchdog(control, spec.watchdog_seconds)
    try:
        control.start()
        watchdog.start()
        control.emit("READY")
        _load_runtime(spec.runtime_path)(spec, control, watchdog)
        control.emit("STOPPED")
    except BaseException as exc:
        control.emit("CRASHED", error_type=type(exc).__name__)
        raise
    finally:
        watchdog.stop()
        control.join(timeout=1.0)
        lock.release()
        connection.close()


def _contract_config(spec: ConnectorChildSpec) -> dict[str, Any]:
    path = _user_data_dir() / "feishu-spike" / f"{spec.child_nonce}.json"
    deadline = time.monotonic() + 5.0
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Feishu spike config not found: {path}")
        time.sleep(0.01)
    return json.loads(path.read_text(encoding="utf-8"))


def _fault_point(config: dict[str, Any], name: str) -> None:
    if config.get("fault_point") != name:
        return
    root = Path(config["fault_dir"])
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.reached").write_text("1", encoding="ascii")
    release = root / f"{name}.release"
    while not release.exists():
        time.sleep(0.01)


def run_sdk_contract_runtime(
    spec: ConnectorChildSpec,
    control: ChildControl,
    watchdog: FrameWatchdog,
) -> None:
    """Real SDK runtime used by the process spike against a local WebSocket endpoint."""
    if importlib.metadata.version("lark-channel-sdk") != SDK_CONTRACT_VERSION:
        raise RuntimeError(f"lark-channel-sdk must be exactly {SDK_CONTRACT_VERSION}")
    config = _contract_config(spec)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Import only after the child owns its event loop; SDK 1.2.0 binds module globals at import.
    from lark_channel import EventDispatcherHandler
    from lark_channel.ws.client import Client as WsClient
    from lark_channel.ws.client import _get_by_key
    from lark_channel.ws.const import HEADER_MESSAGE_ID, HEADER_TRACE_ID

    def stage(event) -> None:
        message_id = str(event.event.message.message_id or "")
        if not message_id:
            raise ValueError("missing message_id")
        database = str(config["database_path"])
        with sqlite3.connect(database, timeout=float(config.get("busy_timeout", 0.2))) as db:
            db.execute(f"PRAGMA busy_timeout={int(float(config.get('busy_timeout', 0.2)) * 1000)}")
            _fault_point(config, "BEFORE_COMMIT")
            db.execute("INSERT OR IGNORE INTO inbox(message_id) VALUES (?)", (message_id,))
            _fault_point(config, "AFTER_WRITE_BEFORE_COMMIT")

            def trace(statement: str) -> None:
                if statement.strip().upper() == "COMMIT":
                    _fault_point(config, "COMMIT_ENTERED")

            db.set_trace_callback(trace)
            db.commit()
            db.set_trace_callback(None)
            _fault_point(config, "AFTER_COMMIT_BEFORE_ACK")

    dispatcher = (
        EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(stage)
        .build()
    )

    class ContractClient(WsClient):
        async def _connect(self) -> None:
            await super()._connect()
            control.emit("CONNECTED")

        async def _handle_data_frame(self, frame) -> None:
            token = ":".join(
                (
                    _get_by_key(frame.headers, HEADER_MESSAGE_ID),
                    _get_by_key(frame.headers, HEADER_TRACE_ID),
                    str(id(frame)),
                )
            )
            watchdog.arm(token)
            await super()._handle_data_frame(frame)
            watchdog.disarm(token)
            _fault_point(config, "ACK_WRITTEN")

        async def _disconnect_and_reconnect(self, *, expected_conn=None):
            control.emit("DISCONNECTED")
            return await super()._disconnect_and_reconnect(expected_conn=expected_conn)

        async def _write_message(self, data: bytes) -> None:
            delay = float(config.get("ack_write_delay", 0.0))
            if delay > 0:
                from lark_channel.ws.pb.pbbp2_pb2 import Frame

                outbound = Frame()
                outbound.ParseFromString(data)
                if outbound.method == 1:  # DATA response; do not delay transport ping frames.
                    await asyncio.sleep(delay)
            await super()._write_message(data)

    client = ContractClient(
        "cli_contract",
        "contract-secret-not-production",
        event_handler=dispatcher,
        domain=str(config["endpoint_domain"]),
        auto_reconnect=bool(config.get("auto_reconnect", False)),
        handshake_timeout=1.0,
    )

    def request_stop() -> None:
        async def shutdown() -> None:
            client._auto_reconnect = False
            try:
                await client._disconnect()
            finally:
                loop.stop()

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), loop)
        except RuntimeError:
            pass

    control.add_stop_callback(request_stop)
    try:
        client.start()
    except RuntimeError as exc:
        if not control.stop_requested.is_set() or "Event loop stopped" not in str(exc):
            raise
    finally:
        tasks = asyncio.all_tasks(loop)
        for task in tasks:
            task.cancel()
        if tasks and not loop.is_closed():
            loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        loop.close()


def run_idle_contract_runtime(
    spec: ConnectorChildSpec,
    control: ChildControl,
    watchdog: FrameWatchdog,
) -> None:
    del spec, watchdog
    control.emit("CONNECTED")
    control.stop_requested.wait()


def run_parent_watchdog_contract_runtime(
    spec: ConnectorChildSpec,
    control: ChildControl,
    watchdog: FrameWatchdog,
) -> None:
    del watchdog
    import signal

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    control.emit("CONNECTED")
    token = f"parent-watchdog:{spec.child_nonce}"
    control.emit(
        "DISPATCH_STARTED",
        frame_token=token,
        deadline_monotonic=time.monotonic() + spec.watchdog_seconds,
    )
    while True:
        time.sleep(1.0)


def run_unstoppable_contract_runtime(
    spec: ConnectorChildSpec,
    control: ChildControl,
    watchdog: FrameWatchdog,
) -> None:
    del spec, watchdog
    import signal

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    control.emit("CONNECTED")
    while True:
        time.sleep(1.0)
