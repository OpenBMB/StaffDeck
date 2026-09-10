from __future__ import annotations

import asyncio
import json
import multiprocessing
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from multiprocessing.process import BaseProcess

import pytest
import websockets
from lark_channel.ws.const import (
    HEADER_BIZ_RT,
    HEADER_MESSAGE_ID,
    HEADER_SEQ,
    HEADER_SUM,
    HEADER_TRACE_ID,
    HEADER_TYPE,
)
from lark_channel.ws.enum import FrameType, MessageType
from lark_channel.ws.pb.pbbp2_pb2 import Frame

from app.channels.feishu_process import ConnectorState, FeishuProcessSupervisor
from feishu_connector_worker import BindingProcessLock, binding_lock_path

SDK_RUNTIME = "feishu_connector_worker:run_sdk_contract_runtime"
IDLE_RUNTIME = "feishu_connector_worker:run_idle_contract_runtime"
PARENT_WATCHDOG_RUNTIME = "feishu_connector_worker:run_parent_watchdog_contract_runtime"
UNSTOPPABLE_RUNTIME = "feishu_connector_worker:run_unstoppable_contract_runtime"


class _LocalFeishuServer:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.started = threading.Event()
        self.connections: list = []
        self.ws_server = None
        self.http_server = None
        self.http_thread = None
        self.ws_url = ""
        self.endpoint_domain = ""
        self.reconnect_count = 0
        self.reconnect_interval = 1
        self.reconnect_nonce = 0

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._start_ws())
        self.started.set()
        self.loop.run_forever()

    async def _start_ws(self) -> None:
        async def handler(connection) -> None:
            self.connections.append(connection)
            try:
                await connection.wait_closed()
            finally:
                pass

        self.ws_server = await websockets.serve(handler, "127.0.0.1", 0)
        port = self.ws_server.sockets[0].getsockname()[1]
        self.ws_url = f"ws://127.0.0.1:{port}/ws?device_id=device-1&service_id=1"

    def start(self) -> None:
        self.thread.start()
        assert self.started.wait(timeout=3.0)
        owner = self

        class EndpointHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                payload = json.dumps(
                    {
                        "code": 0,
                        "msg": "ok",
                        "data": {
                            "URL": owner.ws_url,
                            "ClientConfig": {
                                "ReconnectCount": owner.reconnect_count,
                                "ReconnectInterval": owner.reconnect_interval,
                                "ReconnectNonce": owner.reconnect_nonce,
                                "PingInterval": 60,
                            },
                        },
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format: str, *_args) -> None:
                return

        self.http_server = ThreadingHTTPServer(("127.0.0.1", 0), EndpointHandler)
        self.endpoint_domain = f"http://127.0.0.1:{self.http_server.server_port}"
        self.http_thread = threading.Thread(target=self.http_server.serve_forever, daemon=True)
        self.http_thread.start()

    def wait_for_connections(self, count: int, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            active = self.active_connection_count()
            if active >= count:
                return
            time.sleep(0.01)
        raise TimeoutError(f"expected {count} active WebSocket connections")

    def active_connection_count(self) -> int:
        async def count() -> int:
            return sum(not connection.state.name == "CLOSED" for connection in self.connections)

        return asyncio.run_coroutine_threadsafe(count(), self.loop).result(timeout=2.0)

    def round_trip(
        self,
        frame: Frame,
        timeout: float = 4.0,
        observe_response=None,
    ) -> tuple[Frame, float]:
        async def exchange() -> tuple[Frame, float]:
            active = [connection for connection in self.connections if connection.state.name != "CLOSED"]
            if not active:
                raise RuntimeError("no active Feishu WebSocket")
            started = time.monotonic()
            await active[-1].send(frame.SerializeToString())
            deadline = started + timeout
            while True:
                payload = await asyncio.wait_for(
                    active[-1].recv(), timeout=max(0.01, deadline - time.monotonic())
                )
                response = Frame()
                response.ParseFromString(payload)
                if (
                    response.method == FrameType.DATA.value
                    and _header(response, HEADER_MESSAGE_ID)
                    == _header(frame, HEADER_MESSAGE_ID)
                ):
                    if observe_response:
                        observe_response(response)
                    return response, time.monotonic() - started

        return asyncio.run_coroutine_threadsafe(exchange(), self.loop).result(timeout=timeout + 1)

    def round_trip_many(self, frames: list[Frame], timeout: float = 4.0) -> dict[str, Frame]:
        async def exchange() -> dict[str, Frame]:
            active = [connection for connection in self.connections if connection.state.name != "CLOSED"]
            if not active:
                raise RuntimeError("no active Feishu WebSocket")
            connection = active[-1]
            for frame in frames:
                await connection.send(frame.SerializeToString())
            expected = {_header(frame, HEADER_MESSAGE_ID) for frame in frames}
            responses: dict[str, Frame] = {}
            deadline = time.monotonic() + timeout
            while set(responses) != expected:
                payload = await asyncio.wait_for(
                    connection.recv(), timeout=max(0.01, deadline - time.monotonic())
                )
                response = Frame()
                response.ParseFromString(payload)
                message_id = _header(response, HEADER_MESSAGE_ID)
                if response.method == FrameType.DATA.value and message_id in expected:
                    responses[str(message_id)] = response
            return responses

        return asyncio.run_coroutine_threadsafe(exchange(), self.loop).result(timeout=timeout + 1)

    def close_latest_connection(self) -> None:
        async def close() -> None:
            active = [connection for connection in self.connections if connection.state.name != "CLOSED"]
            if active:
                await active[-1].close()

        asyncio.run_coroutine_threadsafe(close(), self.loop).result(timeout=2.0)

    def total_connection_count(self) -> int:
        async def count() -> int:
            return len(self.connections)

        return asyncio.run_coroutine_threadsafe(count(), self.loop).result(timeout=2.0)

    def stop(self) -> None:
        if self.http_server:
            self.http_server.shutdown()
            self.http_server.server_close()

        async def close_ws() -> None:
            for connection in list(self.connections):
                await connection.close()
            self.ws_server.close()
            await self.ws_server.wait_closed()

        if self.thread.is_alive():
            asyncio.run_coroutine_threadsafe(close_ws(), self.loop).result(timeout=3.0)
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=3.0)
            self.loop.close()


@pytest.fixture
def local_feishu_server():
    server = _LocalFeishuServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _create_inbox(database: Path) -> None:
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE inbox (message_id TEXT PRIMARY KEY)")


def _inbox_count(database: Path) -> int:
    with sqlite3.connect(database) as db:
        return int(db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0])


def _write_contract_config(
    root: Path,
    supervisor: FeishuProcessSupervisor,
    binding_id: str,
    endpoint_domain: str,
    database: Path,
    *,
    fault_point: str | None = None,
    ack_write_delay: float = 0.0,
    auto_reconnect: bool = False,
) -> Path:
    record = supervisor._records[binding_id]
    config_dir = root / "feishu-spike"
    config_dir.mkdir(parents=True, exist_ok=True)
    fault_dir = root / "faults" / record.spec.child_nonce
    config = {
        "endpoint_domain": endpoint_domain,
        "database_path": str(database),
        "busy_timeout": 0.1,
        "auto_reconnect": auto_reconnect,
        "fault_point": fault_point,
        "fault_dir": str(fault_dir),
        "ack_write_delay": ack_write_delay,
    }
    (config_dir / f"{record.spec.child_nonce}.json").write_text(
        json.dumps(config), encoding="utf-8"
    )
    return fault_dir


def _event_frame(message_id: str = "om_1") -> Frame:
    payload = {
        "schema": "2.0",
        "header": {
            "event_id": f"evt_{message_id}",
            "event_type": "im.message.receive_v1",
            "create_time": "1720000000000",
            "token": "",
            "app_id": "cli_contract",
            "tenant_key": "tenant-contract",
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_sender", "user_id": "u_sender"},
                "sender_type": "user",
                "tenant_key": "tenant-contract",
            },
            "message": {
                "message_id": message_id,
                "root_id": "",
                "parent_id": "",
                "create_time": "1720000000000",
                "chat_id": "oc_chat",
                "chat_type": "p2p",
                "message_type": "text",
                "content": json.dumps({"text": "hello"}),
                "mentions": [],
            },
        },
    }
    frame = Frame()
    frame.SeqID = 10
    frame.LogID = 20
    frame.service = 1
    frame.method = FrameType.DATA.value
    frame.payload = json.dumps(payload).encode()
    for key, value in (
        (HEADER_MESSAGE_ID, message_id),
        (HEADER_TRACE_ID, f"trace-{message_id}"),
        (HEADER_SUM, "1"),
        (HEADER_SEQ, "0"),
        (HEADER_TYPE, MessageType.EVENT.value),
    ):
        header = frame.headers.add()
        header.key = key
        header.value = value
    return frame


def _header(frame: Frame, key: str) -> str | None:
    return next((item.value for item in frame.headers if item.key == key), None)


def _wait_for_path(path: Path, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise TimeoutError(path)


def _wait_for_process_exit(supervisor: FeishuProcessSupervisor, binding_id: str) -> int:
    deadline = time.monotonic() + 4.0
    record = supervisor._records[binding_id]
    while time.monotonic() < deadline:
        supervisor.poll_once()
        if not record.process.is_alive():
            return int(record.process.exitcode)
        time.sleep(0.01)
    raise TimeoutError("connector did not exit")


def _parent_that_exits_without_cleanup(data_dir: str, report_connection) -> None:
    os_environ = __import__("os").environ
    os_environ["ULTRARAG_DATA_DIR"] = data_dir
    supervisor = FeishuProcessSupervisor(
        runtime_path=IDLE_RUNTIME,
        database_path=Path(data_dir) / "parent-death.db",
    )
    record = supervisor.start_binding("binding-parent-death", 1)
    supervisor.wait_for_event("binding-parent-death", "CONNECTED")
    report_connection.send(record.process.pid)
    report_connection.close()
    __import__("os")._exit(91)


def _reconfigure_parent_for_crash_window(
    data_dir: str,
    window: str,
    report_connection,
) -> None:
    os_module = __import__("os")
    os_module.environ["ULTRARAG_DATA_DIR"] = data_dir
    database = Path(data_dir) / "reconfigure-crash.db"
    supervisor = FeishuProcessSupervisor(
        runtime_path=IDLE_RUNTIME,
        database_path=database,
        lock_wait_seconds=4.0,
    )
    supervisor.start_binding("binding-reconfigure-crash", 1)
    supervisor.wait_for_event("binding-reconfigure-crash", "CONNECTED")
    if window == "old_running":
        report_connection.send("ready")
        while True:
            time.sleep(1.0)

    def commit_revision() -> bool:
        if window == "after_exit_before_cas":
            report_connection.send("ready")
            while True:
                time.sleep(1.0)
        with sqlite3.connect(database) as db:
            db.execute("UPDATE binding_revision SET revision = 2")
            db.commit()
        return True

    if window == "after_cas_before_spawn":
        def block_new_spawn(_binding_id: str, _revision: int):
            report_connection.send("ready")
            while True:
                time.sleep(1.0)

        supervisor._start_binding_without_operation = block_new_spawn

    supervisor.replace_binding(
        "binding-reconfigure-crash",
        expected_revision=1,
        new_revision=2,
        commit_revision=commit_revision,
        timeout=30.0,
    )


def _pid_alive(pid: int) -> bool:
    try:
        __import__("os").kill(pid, 0)
        return True
    except OSError:
        return False


def test_real_sdk_wire_ack_is_after_visible_commit(
    tmp_path: Path, monkeypatch, local_feishu_server: _LocalFeishuServer
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    database = tmp_path / "inbox.db"
    _create_inbox(database)
    supervisor = FeishuProcessSupervisor(
        runtime_path=SDK_RUNTIME, watchdog_seconds=1.0, database_path=database
    )
    try:
        supervisor.start_binding("binding-wire", 1)
        _write_contract_config(
            tmp_path, supervisor, "binding-wire", local_feishu_server.endpoint_domain, database
        )
        supervisor.wait_for_event("binding-wire", "CONNECTED")
        local_feishu_server.wait_for_connections(1)

        counts_at_wire_ack: list[int] = []
        response, elapsed = local_feishu_server.round_trip(
            _event_frame(),
            observe_response=lambda _response: counts_at_wire_ack.append(_inbox_count(database)),
        )

        assert elapsed < 1.0
        response_payload = json.loads(response.payload.decode())
        assert response_payload["code"] == 200, response_payload
        assert counts_at_wire_ack == [1]
        assert _inbox_count(database) == 1
        assert response.SeqID == 10
        assert response.LogID == 20
        assert response.service == 1
        assert _header(response, HEADER_MESSAGE_ID) == "om_1"
        assert _header(response, HEADER_TRACE_ID) == "trace-om_1"
        assert _header(response, HEADER_BIZ_RT) is not None
    finally:
        assert supervisor.stop(timeout=3.0)


def test_watchdog_kill_before_commit_rolls_back(
    tmp_path: Path, monkeypatch, local_feishu_server: _LocalFeishuServer
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    database = tmp_path / "before.db"
    _create_inbox(database)
    supervisor = FeishuProcessSupervisor(
        runtime_path=SDK_RUNTIME, watchdog_seconds=0.4, database_path=database
    )
    supervisor.start_binding("binding-before", 1)
    fault_dir = _write_contract_config(
        tmp_path,
        supervisor,
        "binding-before",
        local_feishu_server.endpoint_domain,
        database,
        fault_point="BEFORE_COMMIT",
    )
    supervisor.wait_for_event("binding-before", "CONNECTED")
    local_feishu_server.wait_for_connections(1)

    outcome: dict[str, Exception | None] = {"error": None}

    def send() -> None:
        try:
            local_feishu_server.round_trip(_event_frame("om_before"), timeout=2.0)
        except Exception as exc:  # connection closure is the expected wire outcome
            outcome["error"] = exc

    sender = threading.Thread(target=send)
    sender.start()
    _wait_for_path(fault_dir / "BEFORE_COMMIT.reached")
    exit_code = _wait_for_process_exit(supervisor, "binding-before")
    sender.join(timeout=3.0)

    assert exit_code != 0
    assert outcome["error"] is not None
    assert _inbox_count(database) == 0
    assert supervisor.stop(timeout=2.0)


def test_commit_before_ack_kill_replay_remains_single_row(
    tmp_path: Path, monkeypatch, local_feishu_server: _LocalFeishuServer
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    database = tmp_path / "after.db"
    _create_inbox(database)
    first = FeishuProcessSupervisor(
        runtime_path=SDK_RUNTIME, watchdog_seconds=0.4, database_path=database
    )
    first.start_binding("binding-after", 1)
    fault_dir = _write_contract_config(
        tmp_path,
        first,
        "binding-after",
        local_feishu_server.endpoint_domain,
        database,
        fault_point="AFTER_COMMIT_BEFORE_ACK",
    )
    first.wait_for_event("binding-after", "CONNECTED")
    local_feishu_server.wait_for_connections(1)

    sender_error: list[Exception] = []

    def send_first_attempt() -> None:
        try:
            local_feishu_server.round_trip(_event_frame("om_after"), timeout=2.0)
        except Exception as exc:
            sender_error.append(exc)

    sender = threading.Thread(target=send_first_attempt)
    sender.start()
    _wait_for_path(fault_dir / "AFTER_COMMIT_BEFORE_ACK.reached")
    assert _wait_for_process_exit(first, "binding-after") != 0
    sender.join(timeout=3.0)
    assert sender_error
    assert _inbox_count(database) == 1
    assert first.stop(timeout=2.0)

    second = FeishuProcessSupervisor(
        runtime_path=SDK_RUNTIME, watchdog_seconds=1.0, database_path=database
    )
    try:
        second.start_binding("binding-after", 1)
        _write_contract_config(
            tmp_path, second, "binding-after", local_feishu_server.endpoint_domain, database
        )
        second.wait_for_event("binding-after", "CONNECTED")
        local_feishu_server.wait_for_connections(1)
        response, _elapsed = local_feishu_server.round_trip(_event_frame("om_after"))
        assert json.loads(response.payload.decode())["code"] == 200
        assert _inbox_count(database) == 1
        with sqlite3.connect(database) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        assert second.stop(timeout=3.0)


def test_kill_from_sqlite_commit_trace_preserves_integrity(
    tmp_path: Path, monkeypatch, local_feishu_server: _LocalFeishuServer
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    database = tmp_path / "commit-entered.db"
    _create_inbox(database)
    supervisor = FeishuProcessSupervisor(
        runtime_path=SDK_RUNTIME, watchdog_seconds=0.4, database_path=database
    )
    supervisor.start_binding("binding-commit", 1)
    fault_dir = _write_contract_config(
        tmp_path,
        supervisor,
        "binding-commit",
        local_feishu_server.endpoint_domain,
        database,
        fault_point="COMMIT_ENTERED",
    )
    supervisor.wait_for_event("binding-commit", "CONNECTED")
    local_feishu_server.wait_for_connections(1)

    sender_error: list[Exception] = []

    def send() -> None:
        try:
            local_feishu_server.round_trip(_event_frame("om_commit"), timeout=2.0)
        except Exception as exc:
            sender_error.append(exc)

    sender = threading.Thread(target=send)
    sender.start()
    _wait_for_path(fault_dir / "COMMIT_ENTERED.reached")
    assert _wait_for_process_exit(supervisor, "binding-commit") != 0
    sender.join(timeout=3.0)
    assert sender_error
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] in (0, 1)
    assert supervisor.stop(timeout=2.0)


def test_watchdog_covers_stalled_wire_ack_write(
    tmp_path: Path, monkeypatch, local_feishu_server: _LocalFeishuServer
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    database = tmp_path / "ack-stall.db"
    _create_inbox(database)
    supervisor = FeishuProcessSupervisor(
        runtime_path=SDK_RUNTIME, watchdog_seconds=0.4, database_path=database
    )
    supervisor.start_binding("binding-ack-stall", 1)
    _write_contract_config(
        tmp_path,
        supervisor,
        "binding-ack-stall",
        local_feishu_server.endpoint_domain,
        database,
        ack_write_delay=2.0,
    )
    supervisor.wait_for_event("binding-ack-stall", "CONNECTED")
    local_feishu_server.wait_for_connections(1)

    sender_error: list[Exception] = []

    def send() -> None:
        try:
            local_feishu_server.round_trip(_event_frame("om_ack_stall"), timeout=2.0)
        except Exception as exc:
            sender_error.append(exc)

    sender = threading.Thread(target=send)
    sender.start()
    assert _wait_for_process_exit(supervisor, "binding-ack-stall") != 0
    sender.join(timeout=3.0)
    assert sender_error
    assert _inbox_count(database) == 1
    assert supervisor.stop(timeout=2.0)


def test_multiple_inflight_frames_keep_independent_watchdog_tokens(
    tmp_path: Path, monkeypatch, local_feishu_server: _LocalFeishuServer
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    database = tmp_path / "multi-frame.db"
    _create_inbox(database)
    supervisor = FeishuProcessSupervisor(
        runtime_path=SDK_RUNTIME, watchdog_seconds=1.0, database_path=database
    )
    try:
        supervisor.start_binding("binding-multi-frame", 1)
        _write_contract_config(
            tmp_path,
            supervisor,
            "binding-multi-frame",
            local_feishu_server.endpoint_domain,
            database,
            ack_write_delay=0.15,
        )
        supervisor.wait_for_event("binding-multi-frame", "CONNECTED")
        local_feishu_server.wait_for_connections(1)

        responses = local_feishu_server.round_trip_many(
            [_event_frame("om_multi_1"), _event_frame("om_multi_2")]
        )

        assert set(responses) == {"om_multi_1", "om_multi_2"}
        assert all(json.loads(frame.payload.decode())["code"] == 200 for frame in responses.values())
        assert _inbox_count(database) == 2
        record = supervisor._records["binding-multi-frame"]
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and record.dispatch_deadlines:
            time.sleep(0.01)
        assert record.dispatch_deadlines == {}
    finally:
        assert supervisor.stop(timeout=3.0)


def test_sdk_reconnect_sleep_can_be_stopped_without_connection_reviving(
    tmp_path: Path, monkeypatch, local_feishu_server: _LocalFeishuServer
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    local_feishu_server.reconnect_count = -1
    local_feishu_server.reconnect_interval = 1
    local_feishu_server.reconnect_nonce = 1
    database = tmp_path / "reconnect.db"
    _create_inbox(database)
    supervisor = FeishuProcessSupervisor(
        runtime_path=SDK_RUNTIME, watchdog_seconds=1.0, database_path=database
    )
    record = supervisor.start_binding("binding-reconnect", 1)
    _write_contract_config(
        tmp_path,
        supervisor,
        "binding-reconnect",
        local_feishu_server.endpoint_domain,
        database,
        auto_reconnect=True,
    )
    supervisor.wait_for_event("binding-reconnect", "CONNECTED")
    local_feishu_server.wait_for_connections(1)
    local_feishu_server.close_latest_connection()
    supervisor.wait_for_event("binding-reconnect", "DISCONNECTED")
    connection_count_at_stop = local_feishu_server.total_connection_count()

    assert supervisor.stop_binding("binding-reconnect", timeout=2.0)
    assert record.exit_code == 0
    time.sleep(1.2)
    assert local_feishu_server.total_connection_count() == connection_count_at_stop
    assert local_feishu_server.active_connection_count() == 0
    assert supervisor.stop(timeout=2.0)


def test_two_real_connectors_isolate_shared_sqlite_write_contention(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    server_a = _LocalFeishuServer()
    server_b = _LocalFeishuServer()
    server_a.start()
    server_b.start()
    database = tmp_path / "shared.db"
    _create_inbox(database)
    supervisor = FeishuProcessSupervisor(
        runtime_path=SDK_RUNTIME,
        watchdog_seconds=0.8,
        database_path=database,
    )
    sender_error: list[Exception] = []
    try:
        supervisor.start_binding("binding-a", 1)
        fault_dir = _write_contract_config(
            tmp_path,
            supervisor,
            "binding-a",
            server_a.endpoint_domain,
            database,
            fault_point="AFTER_WRITE_BEFORE_COMMIT",
        )
        supervisor.start_binding("binding-b", 1)
        _write_contract_config(
            tmp_path,
            supervisor,
            "binding-b",
            server_b.endpoint_domain,
            database,
        )
        supervisor.wait_for_event("binding-a", "CONNECTED")
        supervisor.wait_for_event("binding-b", "CONNECTED")
        server_a.wait_for_connections(1)
        server_b.wait_for_connections(1)

        def hold_a_write_lock() -> None:
            try:
                server_a.round_trip(_event_frame("om_a_lock"), timeout=2.0)
            except Exception as exc:
                sender_error.append(exc)

        sender = threading.Thread(target=hold_a_write_lock)
        sender.start()
        _wait_for_path(fault_dir / "AFTER_WRITE_BEFORE_COMMIT.reached")

        response, elapsed = server_b.round_trip(_event_frame("om_b_locked"), timeout=1.0)
        assert elapsed < 0.8
        assert json.loads(response.payload.decode())["code"] == 500
        assert supervisor._records["binding-b"].process.is_alive()
        assert _wait_for_process_exit(supervisor, "binding-a") != 0
        sender.join(timeout=2.0)
        assert sender_error

        response, _elapsed = server_b.round_trip(_event_frame("om_b_after"), timeout=1.0)
        assert json.loads(response.payload.decode())["code"] == 200
        assert _inbox_count(database) == 1
        assert supervisor.state("binding-b") == ConnectorState.RUNNING
    finally:
        assert supervisor.stop(timeout=3.0)
        server_a.stop()
        server_b.stop()


def test_binding_lock_rejects_second_connector(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    database = tmp_path / "lock.db"
    first = FeishuProcessSupervisor(runtime_path=IDLE_RUNTIME, database_path=database)
    second = FeishuProcessSupervisor(
        runtime_path=IDLE_RUNTIME, database_path=database, lock_wait_seconds=0.0
    )
    try:
        first.start_binding("same-binding", 1)
        first.wait_for_event("same-binding", "CONNECTED")
        with pytest.raises(RuntimeError, match="binding lock is still held"):
            second.start_binding("same-binding", 1)
        assert first.state("same-binding") == ConnectorState.RUNNING
    finally:
        assert first.stop(timeout=3.0)
        assert second.stop(timeout=3.0)


def test_binding_lock_treats_inaccessible_lock_as_busy(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "connector.lock"
    lock = BindingProcessLock(path)
    original_open = Path.open

    def inaccessible_open(self, *args, **kwargs):
        if self == path:
            raise PermissionError("simulated inaccessible lock")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", inaccessible_open)
    assert lock.acquire() is False


def test_same_binding_in_different_databases_does_not_false_conflict(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    first = FeishuProcessSupervisor(
        runtime_path=IDLE_RUNTIME, database_path=tmp_path / "tenant-a.db"
    )
    second = FeishuProcessSupervisor(
        runtime_path=IDLE_RUNTIME, database_path=tmp_path / "tenant-b.db"
    )
    try:
        first.start_binding("same-binding-id", 1)
        second.start_binding("same-binding-id", 1)
        first.wait_for_event("same-binding-id", "CONNECTED")
        second.wait_for_event("same-binding-id", "CONNECTED")
        assert first.state("same-binding-id") == ConnectorState.RUNNING
        assert second.state("same-binding-id") == ConnectorState.RUNNING
    finally:
        assert first.stop(timeout=2.0)
        assert second.stop(timeout=2.0)


@pytest.mark.parametrize("wrong_field", ["binding_id", "config_revision", "child_nonce", "pid"])
def test_stale_generation_ipc_cannot_override_current_state(
    tmp_path: Path, monkeypatch, wrong_field: str
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    supervisor = FeishuProcessSupervisor(
        runtime_path=IDLE_RUNTIME, database_path=tmp_path / "generation.db"
    )
    try:
        record = supervisor.start_binding("binding-generation", 4)
        supervisor.wait_for_event("binding-generation", "CONNECTED")
        event = {
            "event": "DISCONNECTED",
            "binding_id": record.spec.binding_id,
            "config_revision": record.spec.config_revision,
            "child_nonce": record.spec.child_nonce,
            "pid": record.process.pid,
        }
        wrong_values = {
            "binding_id": "other-binding",
            "config_revision": record.spec.config_revision + 1,
            "child_nonce": "stale-nonce",
            "pid": record.process.pid + 1,
        }
        event[wrong_field] = wrong_values[wrong_field]
        supervisor.inject_event_for_test("binding-generation", event)
        assert record.connected
        assert record.state == ConnectorState.RUNNING
    finally:
        assert supervisor.stop(timeout=3.0)


def test_child_owned_lock_is_released_after_parent_pipe_closes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    database = tmp_path / "orphan.db"
    supervisor = FeishuProcessSupervisor(runtime_path=IDLE_RUNTIME, database_path=database)
    record = supervisor.start_binding("binding-orphan", 1)
    supervisor.wait_for_event("binding-orphan", "CONNECTED")
    record.connection.close()

    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline and record.process.is_alive():
        time.sleep(0.01)
    record.process.join(timeout=1.0)
    assert not record.process.is_alive()
    lock = BindingProcessLock(binding_lock_path("binding-orphan", database))
    assert lock.acquire()
    lock.release()
    assert supervisor.stop(timeout=2.0)


def test_graceful_stop_reports_stopped_zero_exit_and_no_monitor_leak(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    supervisor = FeishuProcessSupervisor(
        runtime_path=IDLE_RUNTIME,
        database_path=tmp_path / "graceful.db",
    )
    record = supervisor.start_binding("binding-graceful", 1)
    supervisor.wait_for_event("binding-graceful", "CONNECTED")

    assert supervisor.stop_binding("binding-graceful", timeout=2.0)
    assert record.exit_code == 0
    assert any(event.get("event") == "STOPPED" for event in record.events)
    assert supervisor.stop(timeout=2.0)
    assert not supervisor.monitor_alive()


def test_parent_process_death_releases_orphan_before_replacement_starts(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=False)
    parent = context.Process(
        target=_parent_that_exits_without_cleanup,
        args=(str(tmp_path), child_connection),
    )
    parent.start()
    child_connection.close()
    orphan_pid = parent_connection.recv()
    parent.join(timeout=4.0)
    assert parent.exitcode == 91

    replacement = FeishuProcessSupervisor(
        runtime_path=IDLE_RUNTIME,
        database_path=tmp_path / "parent-death.db",
        lock_wait_seconds=4.0,
    )
    try:
        replacement.start_binding("binding-parent-death", 1)
        replacement.wait_for_event("binding-parent-death", "CONNECTED")
        assert replacement.state("binding-parent-death") == ConnectorState.RUNNING
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and _pid_alive(orphan_pid):
            time.sleep(0.01)
        assert not _pid_alive(orphan_pid)
    finally:
        assert replacement.stop(timeout=3.0)


def test_parent_watchdog_escalates_from_ignored_terminate_to_kill(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    supervisor = FeishuProcessSupervisor(
        runtime_path=PARENT_WATCHDOG_RUNTIME,
        database_path=tmp_path / "parent-watchdog.db",
        watchdog_seconds=0.2,
        terminate_grace_seconds=0.1,
    )
    record = supervisor.start_binding("binding-parent-watchdog", 1)
    started_event = supervisor.wait_for_event(
        "binding-parent-watchdog", "DISPATCH_STARTED"
    )
    exit_code = _wait_for_process_exit(supervisor, "binding-parent-watchdog")
    elapsed = time.monotonic() - float(started_event["deadline_monotonic"])

    assert exit_code != 0
    assert record.termination_phase == "kill"
    assert elapsed < 0.6
    assert supervisor.stop(timeout=2.0)


def test_repeated_watchdog_crashes_enter_and_can_reset_circuit_breaker(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    supervisor = FeishuProcessSupervisor(
        runtime_path=PARENT_WATCHDOG_RUNTIME,
        database_path=tmp_path / "poison.db",
        watchdog_seconds=0.1,
        terminate_grace_seconds=0.05,
        crash_limit=3,
        crash_window_seconds=10.0,
        backoff_seconds=0.15,
    )
    for _attempt in range(3):
        supervisor.start_binding("binding-poison", 1)
        _wait_for_process_exit(supervisor, "binding-poison")
    assert supervisor.state("binding-poison") == ConnectorState.CRASH_BACKOFF
    with pytest.raises(RuntimeError, match="crash backoff"):
        supervisor.start_binding("binding-poison", 1)
    time.sleep(0.18)
    record = supervisor.start_binding("binding-poison", 1)
    assert record.process.is_alive()
    supervisor.reset_backoff("binding-poison")
    assert supervisor.stop(timeout=2.0)


def test_global_stop_kills_unresponsive_children_with_one_shared_deadline(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    supervisor = FeishuProcessSupervisor(
        runtime_path=UNSTOPPABLE_RUNTIME,
        database_path=tmp_path / "unresponsive.db",
    )
    records = []
    for index in range(3):
        binding_id = f"binding-unresponsive-{index}"
        records.append(supervisor.start_binding(binding_id, 1))
        supervisor.wait_for_event(binding_id, "CONNECTED")

    started = time.monotonic()
    assert supervisor.stop(timeout=0.8)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert all(record.exit_code is not None for record in records)
    assert not supervisor.monitor_alive()


def test_repeated_start_stop_leaves_no_supervisor_threads_or_children(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    before_children = {process.pid for process in multiprocessing.active_children()}
    for index in range(3):
        supervisor = FeishuProcessSupervisor(
            runtime_path=IDLE_RUNTIME,
            database_path=tmp_path / f"cleanup-{index}.db",
        )
        supervisor.start_binding(f"binding-cleanup-{index}", 1)
        supervisor.wait_for_event(f"binding-cleanup-{index}", "CONNECTED")
        assert supervisor.stop(timeout=2.0)
        assert not supervisor.monitor_alive()

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        leaked_threads = [
            thread
            for thread in threading.enumerate()
            if thread.name.startswith("staffdeck-feishu-supervisor")
        ]
        new_children = {
            process.pid for process in multiprocessing.active_children()
        } - before_children
        if not leaked_threads and not new_children:
            break
        time.sleep(0.02)
    assert leaked_threads == []
    assert new_children == set()


def test_global_stop_waits_for_process_start_operation_to_reap_child(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    supervisor = FeishuProcessSupervisor(
        runtime_path=IDLE_RUNTIME,
        database_path=tmp_path / "start-stop-race.db",
    )
    entered_start = threading.Event()
    release_start = threading.Event()
    original_start = BaseProcess.start

    def blocking_start(process) -> None:
        entered_start.set()
        assert release_start.wait(timeout=2.0)
        original_start(process)

    monkeypatch.setattr(BaseProcess, "start", blocking_start)
    start_errors: list[Exception] = []
    stop_results: list[bool] = []

    def start_binding() -> None:
        try:
            supervisor.start_binding("binding-start-stop-race", 1)
        except Exception as exc:
            start_errors.append(exc)

    start_thread = threading.Thread(target=start_binding)
    start_thread.start()
    assert entered_start.wait(timeout=2.0)
    stop_thread = threading.Thread(target=lambda: stop_results.append(supervisor.stop(timeout=2.0)))
    stop_thread.start()
    time.sleep(0.05)
    assert stop_thread.is_alive()
    release_start.set()
    start_thread.join(timeout=3.0)
    stop_thread.join(timeout=3.0)

    assert stop_results == [True]
    assert start_errors and "closing" in str(start_errors[0])
    assert not supervisor.monitor_alive()


def test_closing_start_hands_unresponsive_started_child_to_global_stop(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    supervisor = FeishuProcessSupervisor(
        runtime_path=UNSTOPPABLE_RUNTIME,
        database_path=tmp_path / "unowned-child-race.db",
    )
    entered_start = threading.Event()
    release_start = threading.Event()
    started_pids: list[int] = []
    original_start = BaseProcess.start
    original_stop_records = supervisor._stop_records
    closing_attempts = 0

    def blocking_start(process) -> None:
        entered_start.set()
        assert release_start.wait(timeout=2.0)
        original_start(process)
        started_pids.append(process.pid)

    def first_closing_attempt_fails(records, deadline):
        nonlocal closing_attempts
        closing_attempts += 1
        if closing_attempts == 1 and threading.current_thread().name == "start-owner-thread":
            return False
        return original_stop_records(records, deadline)

    monkeypatch.setattr(BaseProcess, "start", blocking_start)
    monkeypatch.setattr(supervisor, "_stop_records", first_closing_attempt_fails)
    start_errors: list[Exception] = []
    stop_results: list[bool] = []

    def start_binding() -> None:
        try:
            supervisor.start_binding("binding-unowned-race", 1)
        except Exception as exc:
            start_errors.append(exc)

    start_thread = threading.Thread(target=start_binding, name="start-owner-thread")
    start_thread.start()
    assert entered_start.wait(timeout=2.0)
    stop_thread = threading.Thread(target=lambda: stop_results.append(supervisor.stop(timeout=3.0)))
    stop_thread.start()
    release_start.set()
    start_thread.join(timeout=4.0)
    stop_thread.join(timeout=4.0)

    assert stop_results == [True]
    assert start_errors and "closing" in str(start_errors[0])
    assert started_pids
    assert all(not _pid_alive(pid) for pid in started_pids)
    assert not supervisor.monitor_alive()


def test_global_stop_waits_for_inflight_stop_binding_operation(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    supervisor = FeishuProcessSupervisor(
        runtime_path=IDLE_RUNTIME,
        database_path=tmp_path / "double-stop-race.db",
    )
    supervisor.start_binding("binding-double-stop", 1)
    supervisor.wait_for_event("binding-double-stop", "CONNECTED")
    entered_stop = threading.Event()
    release_stop = threading.Event()
    original_stop_records = supervisor._stop_records

    def blocking_stop_records(records, deadline):
        if threading.current_thread().name == "binding-stop-thread":
            entered_stop.set()
            assert release_stop.wait(timeout=2.0)
        return original_stop_records(records, deadline)

    monkeypatch.setattr(supervisor, "_stop_records", blocking_stop_records)
    binding_results: list[bool] = []
    global_results: list[bool] = []
    binding_thread = threading.Thread(
        target=lambda: binding_results.append(
            supervisor.stop_binding("binding-double-stop", timeout=2.0)
        ),
        name="binding-stop-thread",
    )
    binding_thread.start()
    assert entered_stop.wait(timeout=2.0)
    global_thread = threading.Thread(
        target=lambda: global_results.append(supervisor.stop(timeout=2.0))
    )
    global_thread.start()
    time.sleep(0.05)
    assert global_thread.is_alive()
    release_stop.set()
    binding_thread.join(timeout=3.0)
    global_thread.join(timeout=3.0)

    assert binding_results == [True]
    assert global_results == [True]
    assert not supervisor.monitor_alive()


def test_replace_binding_stops_old_generation_before_revision_commit(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    supervisor = FeishuProcessSupervisor(
        runtime_path=IDLE_RUNTIME,
        database_path=tmp_path / "replace.db",
    )
    old = supervisor.start_binding("binding-replace", 5)
    old_pid = old.process.pid
    supervisor.wait_for_event("binding-replace", "CONNECTED")
    commit_observations: list[tuple[int | None, bool]] = []

    def commit_revision() -> bool:
        lock = BindingProcessLock(Path(old.spec.binding_lock_path))
        lock_free = lock.acquire()
        if lock_free:
            lock.release()
        commit_observations.append((old.exit_code, lock_free))
        return True

    new = supervisor.replace_binding(
        "binding-replace",
        expected_revision=5,
        new_revision=6,
        commit_revision=commit_revision,
        timeout=3.0,
    )
    supervisor.wait_for_event("binding-replace", "CONNECTED")

    assert commit_observations == [(0, True)]
    assert new.spec.config_revision == 6
    assert new.process.pid != old_pid
    with pytest.raises(RuntimeError, match="revision changed"):
        supervisor.start_binding("binding-replace", 7)
    old_event = {
        "event": "DISCONNECTED",
        "binding_id": old.spec.binding_id,
        "config_revision": old.spec.config_revision,
        "child_nonce": old.spec.child_nonce,
        "pid": old_pid,
    }
    supervisor.inject_event_for_test("binding-replace", old_event)
    assert new.connected
    assert supervisor.stop(timeout=2.0)


def test_concurrent_starts_reserve_process_capacity_before_spawn(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    supervisor = FeishuProcessSupervisor(
        runtime_path=IDLE_RUNTIME,
        database_path=tmp_path / "capacity.db",
        max_processes=1,
    )
    entered_spawn = threading.Event()
    release_spawn = threading.Event()
    original_spawn = supervisor._spawn_reserved_binding

    def blocking_spawn(binding_id: str, revision: int):
        if binding_id == "binding-capacity-a":
            entered_spawn.set()
            assert release_spawn.wait(timeout=2.0)
        return original_spawn(binding_id, revision)

    monkeypatch.setattr(supervisor, "_spawn_reserved_binding", blocking_spawn)
    first_records = []
    first_thread = threading.Thread(
        target=lambda: first_records.append(supervisor.start_binding("binding-capacity-a", 1))
    )
    first_thread.start()
    assert entered_spawn.wait(timeout=2.0)
    with pytest.raises(RuntimeError, match="process limit reached"):
        supervisor.start_binding("binding-capacity-b", 1)
    release_spawn.set()
    first_thread.join(timeout=3.0)

    assert len(first_records) == 1
    supervisor.wait_for_event("binding-capacity-a", "CONNECTED")
    assert supervisor.stop(timeout=2.0)


def test_forget_binding_waits_for_unpublished_start_and_cannot_split_owner_lock(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    supervisor = FeishuProcessSupervisor(
        runtime_path=IDLE_RUNTIME,
        database_path=tmp_path / "forget-race.db",
    )
    entered_spawn = threading.Event()
    release_spawn = threading.Event()
    original_spawn = supervisor._spawn_reserved_binding

    def blocking_spawn(binding_id: str, revision: int):
        entered_spawn.set()
        assert release_spawn.wait(timeout=2.0)
        return original_spawn(binding_id, revision)

    monkeypatch.setattr(supervisor, "_spawn_reserved_binding", blocking_spawn)
    start_records = []
    forget_errors: list[Exception] = []
    start_thread = threading.Thread(
        target=lambda: start_records.append(supervisor.start_binding("binding-forget-race", 1))
    )

    def forget() -> None:
        try:
            supervisor.forget_binding("binding-forget-race")
        except Exception as exc:
            forget_errors.append(exc)

    start_thread.start()
    assert entered_spawn.wait(timeout=2.0)
    forget_thread = threading.Thread(target=forget)
    forget_thread.start()
    time.sleep(0.05)
    assert forget_thread.is_alive()
    release_spawn.set()
    start_thread.join(timeout=3.0)
    forget_thread.join(timeout=3.0)

    assert len(start_records) == 1
    assert forget_errors and "running" in str(forget_errors[0])
    assert supervisor.state("binding-forget-race") in {
        ConnectorState.STARTING,
        ConnectorState.RUNNING,
    }
    assert supervisor.stop(timeout=2.0)
    with pytest.raises(RuntimeError, match="closing"):
        supervisor.forget_binding("binding-forget-race")


def test_stop_does_not_mark_closed_until_binding_locks_are_free(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    supervisor = FeishuProcessSupervisor(
        runtime_path=IDLE_RUNTIME,
        database_path=tmp_path / "lock-close.db",
    )
    supervisor.start_binding("binding-lock-close", 1)
    supervisor.wait_for_event("binding-lock-close", "CONNECTED")
    original_lock_check = supervisor._binding_lock_is_free
    checks = 0

    def first_check_blocked(path: Path) -> bool:
        nonlocal checks
        checks += 1
        if checks == 1:
            return False
        return original_lock_check(path)

    monkeypatch.setattr(supervisor, "_binding_lock_is_free", first_check_blocked)
    assert not supervisor.stop(timeout=2.0)
    assert supervisor.stop(timeout=2.0)


@pytest.mark.parametrize(
    ("window", "expected_revision"),
    [
        ("old_running", 1),
        ("after_exit_before_cas", 1),
        ("after_cas_before_spawn", 2),
    ],
)
def test_reconfigure_parent_crash_windows_recover_database_generation(
    tmp_path: Path,
    monkeypatch,
    window: str,
    expected_revision: int,
) -> None:
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path))
    database = tmp_path / "reconfigure-crash.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE binding_revision (revision INTEGER NOT NULL)")
        db.execute("INSERT INTO binding_revision VALUES (1)")
        db.commit()
    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=False)
    parent = context.Process(
        target=_reconfigure_parent_for_crash_window,
        args=(str(tmp_path), window, child_connection),
    )
    parent.start()
    child_connection.close()
    assert parent_connection.recv() == "ready"
    parent.terminate()
    parent.join(timeout=4.0)
    assert not parent.is_alive()
    with sqlite3.connect(database) as db:
        revision = int(db.execute("SELECT revision FROM binding_revision").fetchone()[0])
    assert revision == expected_revision

    replacement = FeishuProcessSupervisor(
        runtime_path=IDLE_RUNTIME,
        database_path=database,
        lock_wait_seconds=4.0,
    )
    try:
        record = replacement.start_binding("binding-reconfigure-crash", revision)
        replacement.wait_for_event("binding-reconfigure-crash", "CONNECTED")
        assert record.spec.config_revision == expected_revision
    finally:
        assert replacement.stop(timeout=3.0)
