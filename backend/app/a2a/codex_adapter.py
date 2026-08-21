from __future__ import annotations

import json
import mimetypes
import os
import queue
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from sqlmodel import Session, select

from app.config import get_settings
from app.db import engine
from app.db.models import A2ATaskEvent, A2ATaskRun, utc_now


router = APIRouter(tags=["a2a-codex"])
_processes: dict[str, subprocess.Popen[str]] = {}
_process_lock = threading.Lock()
_submission_lock = threading.Lock()
_shutting_down = threading.Event()
_TERMINAL = {"completed", "failed", "canceled", "rejected", "input-required"}


@router.get("/.well-known/agent-card.json")
def codex_agent_card(request: Request) -> dict[str, Any]:
    settings = _enabled_settings()
    endpoint = str(request.base_url).rstrip("/") + "/api/a2a/codex"
    return {
        "name": "Codex CLI",
        "description": "Codex CLI exposed as a durable A2A agent.",
        "version": "1.0.0",
        "protocolVersion": "1.0",
        "supportedInterfaces": [
            {"url": endpoint, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
        "capabilities": {"streaming": True, "pushNotifications": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain", "application/json", "application/octet-stream"],
        "skills": [
            {
                "id": "codex-cli",
                "name": "Codex CLI",
                "description": "Coding and knowledge work with file artifacts.",
                "tags": ["coding", "files", "analysis"],
            }
        ],
        "securitySchemes": ({"bearer": {"type": "http", "scheme": "bearer"}} if settings.codex_a2a_token else {}),
    }


@router.post("/api/a2a/codex")
def codex_a2a_rpc(
    request: Request,
    payload: dict[str, Any],
    authorization: str | None = Header(default=None),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> Response:
    settings = _enabled_settings()
    _authorize(settings.codex_a2a_token, authorization)
    request_id = payload.get("id")
    method = str(payload.get("method") or "")
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    try:
        if method in {"SendMessage", "SendStreamingMessage"}:
            task, stream_after = _submit_message(params, request=request)
            if method == "SendStreamingMessage":
                return _stream_task(
                    task.id,
                    request_id=request_id,
                    after_event_id=last_event_id or str(stream_after),
                )
            return JSONResponse(_envelope(request_id, _task_payload(task, request=request)))
        if method == "GetTask":
            task = _require_task(str(params.get("id") or params.get("taskId") or ""))
            return JSONResponse(_envelope(request_id, _task_payload(task, request=request)))
        if method == "CancelTask":
            task = _require_task(str(params.get("id") or params.get("taskId") or ""))
            _cancel(task.id)
            with Session(engine) as db:
                task = db.get(A2ATaskRun, task.id)
                assert task is not None
                return JSONResponse(_envelope(request_id, _task_payload(task, request=request)))
        if method == "SubscribeToTask":
            task = _require_task(str(params.get("id") or params.get("taskId") or ""))
            return _stream_task(task.id, request_id=request_id, after_event_id=last_event_id)
        if method == "ListTasks":
            return JSONResponse(_envelope(request_id, _list_tasks(params, request=request)))
        return JSONResponse(
            _error_envelope(request_id, -32601, f"Unsupported A2A method: {method}"),
            status_code=400,
        )
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse(_error_envelope(request_id, -32000, str(exc)), status_code=500)


@router.get("/api/a2a/codex/tasks/{task_id}/artifacts/{artifact_path:path}")
def codex_a2a_artifact(
    task_id: str,
    artifact_path: str,
    authorization: str | None = Header(default=None),
) -> FileResponse:
    settings = _enabled_settings()
    _authorize(settings.codex_a2a_token, authorization)
    task = _require_task(task_id)
    root = Path(str(task.request_json.get("workspace") or "")).resolve()
    target = (root / artifact_path).resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(target, filename=target.name)


def recover_codex_a2a_tasks() -> None:
    settings = get_settings()
    if not settings.codex_a2a_enabled:
        return
    _shutting_down.clear()
    with Session(engine) as db:
        tasks = list(
            db.exec(
                select(A2ATaskRun).where(
                    A2ATaskRun.direction == "server",
                    A2ATaskRun.status.in_(["submitted", "working"]),
                )
            ).all()
        )
        ids = [task.id for task in tasks]
    for task_id in ids:
        _launch(task_id, recovery=True)


def stop_codex_a2a_tasks() -> None:
    """Stop CLI children while leaving durable tasks eligible for recovery."""

    _shutting_down.set()
    with _process_lock:
        processes = list(_processes.values())
    for process in processes:
        if process.poll() is None:
            process.terminate()
    for process in processes:
        if process.poll() is not None:
            continue
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()


def _submit_message(params: dict[str, Any], *, request: Request) -> tuple[A2ATaskRun, int]:
    with _submission_lock:
        return _submit_message_locked(params, request=request)


def _submit_message_locked(
    params: dict[str, Any], *, request: Request
) -> tuple[A2ATaskRun, int]:
    message = params.get("message") if isinstance(params.get("message"), dict) else {}
    prompt = _message_text(message)
    if not prompt:
        raise RuntimeError("A2A message has no text part.")
    message_id = str(message.get("messageId") or message.get("message_id") or "").strip()
    if message_id:
        duplicate = _task_for_message_id(message_id)
        if duplicate is not None and duplicate.remote_task_id:
            latest = _latest_event_sequence(duplicate.id)
            return _require_task(duplicate.remote_task_id), max(latest - 1, 0)
    existing_id = str(message.get("taskId") or "").strip()
    if existing_id:
        with Session(engine) as db:
            existing = db.exec(
                select(A2ATaskRun).where(
                    A2ATaskRun.direction == "server",
                    A2ATaskRun.remote_task_id == existing_id,
                )
            ).first()
            if existing is None:
                raise RuntimeError("A2A continuation task was not found.")
            if existing.status not in _TERMINAL:
                raise RuntimeError("A2A task is still running.")
            stream_after = _latest_event_sequence(existing.id, db=db)
            existing.status = "submitted"
            existing.finished_at = None
            existing.cancel_requested = False
            existing.request_json = {**existing.request_json, "prompt": prompt, "resume": True}
            existing.result_json = {}
            existing.error_json = {}
            existing.updated_at = utc_now()
            db.add(existing)
            db.commit()
            db.refresh(existing)
            if message_id:
                _append_event(
                    db,
                    existing,
                    "message_received",
                    {"messageId": message_id},
                    external_event_id=message_id,
                )
            _append_event(db, existing, "submitted", _status_update(existing, "submitted"))
            task_id = existing.id
        _launch(task_id)
        return _require_task(existing_id), stream_after

    settings = get_settings()
    workspace_root = Path(settings.codex_a2a_workspace_root or "/tmp/staffdeck-codex-a2a")
    task_public_id = uuid.uuid4().hex
    workspace = (workspace_root / task_public_id).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    attachment_paths = _materialize_message_files(message, workspace)
    if attachment_paths:
        prompt += "\n\nAttached files are available at:\n" + "\n".join(
            f"- {path}" for path in attachment_paths
        )
    context_id = str(message.get("contextId") or uuid.uuid4().hex)
    task = A2ATaskRun(
        direction="server",
        tenant_id="a2a_codex",
        endpoint_url=str(request.base_url).rstrip("/") + "/api/a2a/codex",
        protocol_binding="JSONRPC",
        protocol_version="1.0",
        remote_task_id=task_public_id,
        context_id=context_id,
        invocation_id=message_id or None,
        status="submitted",
        request_json={"prompt": prompt, "workspace": str(workspace), "resume": False},
        started_at=utc_now(),
    )
    with Session(engine) as db:
        db.add(task)
        db.commit()
        db.refresh(task)
        if message_id:
            _append_event(
                db,
                task,
                "message_received",
                {"messageId": message_id},
                external_event_id=message_id,
            )
        _append_event(db, task, "submitted", _status_update(task, "submitted"))
        task_id = task.id
    _launch(task_id)
    return _require_task(task_public_id), 0


def _launch(task_id: str, *, recovery: bool = False) -> None:
    thread = threading.Thread(
        target=_run_codex_task,
        args=(task_id,),
        kwargs={"recovery": recovery},
        daemon=True,
        name=f"codex-a2a-{task_id[-8:]}",
    )
    thread.start()


def _run_codex_task(task_id: str, *, recovery: bool = False) -> None:
    settings = get_settings()
    with Session(engine) as db:
        task = db.get(A2ATaskRun, task_id)
        if task is None or task.cancel_requested:
            return
        prompt = str(task.request_json.get("prompt") or "")
        workspace = Path(str(task.request_json.get("workspace") or "")).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        before = _workspace_snapshot(workspace)
        task.status = "working"
        task.recovery_attempts += 1 if recovery else 0
        task.updated_at = utc_now()
        db.add(task)
        db.commit()
        _append_event(db, task, "working", _status_update(task, "working"))
        codex_session_id = task.codex_session_id
        should_resume = bool(task.request_json.get("resume") or recovery) and codex_session_id

    command = [settings.codex_a2a_command, "exec"]
    if should_resume:
        command.extend(
            [
                "resume",
                "--json",
                "--skip-git-repo-check",
                str(codex_session_id),
                prompt,
            ]
        )
    else:
        command.extend(
            [
                "--json",
                "--skip-git-repo-check",
                "--sandbox",
                "workspace-write",
                "-C",
                str(workspace),
                prompt,
            ]
        )
    started = time.monotonic()
    final_text = ""
    try:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=os.environ.copy(),
        )
        with _process_lock:
            _processes[task_id] = process
        assert process.stdout is not None
        output_queue: queue.Queue[str | None] = queue.Queue()

        def read_output() -> None:
            assert process.stdout is not None
            for output_line in process.stdout:
                output_queue.put(output_line)
            output_queue.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        raw_output: list[str] = []
        while True:
            if time.monotonic() - started > settings.codex_a2a_timeout_seconds:
                process.terminate()
                raise TimeoutError("Codex CLI A2A task timed out.")
            with Session(engine) as db:
                current_task = db.get(A2ATaskRun, task_id)
                if current_task is None:
                    process.terminate()
                    return
                if current_task.cancel_requested:
                    process.terminate()
                    raise RuntimeError("Codex CLI A2A task was cancelled.")
            try:
                line = output_queue.get(timeout=0.25)
            except queue.Empty:
                if process.poll() is not None and not reader.is_alive():
                    break
                continue
            if line is None:
                break
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                raw_output.append(line.rstrip())
                continue
            with Session(engine) as db:
                task = db.get(A2ATaskRun, task_id)
                if task is None:
                    process.terminate()
                    return
                if task.cancel_requested:
                    process.terminate()
                    raise RuntimeError("Codex CLI A2A task was cancelled.")
                session_id = _codex_session_id(event)
                if session_id:
                    task.codex_session_id = session_id
                text = _codex_text(event)
                if text:
                    final_text = text
                task.updated_at = utc_now()
                db.add(task)
                db.commit()
                _append_event(
                    db,
                    task,
                    "codex_event",
                    {
                        "taskId": task.remote_task_id,
                        "contextId": task.context_id,
                        "codexEvent": event,
                    },
                )
        return_code = process.wait(timeout=5)
        if _shutting_down.is_set():
            return
        if return_code != 0:
            diagnostic = "\n".join(raw_output[-20:]).strip()
            raise RuntimeError(diagnostic or f"Codex CLI exited with {return_code}.")

        artifacts = _collect_artifacts(workspace, before)
        with Session(engine) as db:
            task = db.get(A2ATaskRun, task_id)
            if task is None:
                return
            task.status = "completed"
            task.artifacts_json = artifacts
            task.result_json = {"text": final_text, "artifacts": artifacts}
            task.finished_at = utc_now()
            task.updated_at = utc_now()
            db.add(task)
            db.commit()
            _append_event(db, task, "completed", _task_payload(task))
    except Exception as exc:
        if _shutting_down.is_set():
            with Session(engine) as db:
                task = db.get(A2ATaskRun, task_id)
                if task is not None and task.status not in _TERMINAL:
                    task.status = "working"
                    task.error_json = {
                        "message": "Service stopped; the durable task will resume on startup."
                    }
                    task.updated_at = utc_now()
                    db.add(task)
                    db.commit()
                    _append_event(
                        db,
                        task,
                        "interrupted",
                        _status_update(task, "working"),
                    )
            return
        with Session(engine) as db:
            task = db.get(A2ATaskRun, task_id)
            if task is None:
                return
            cancelled = task.cancel_requested or "cancel" in str(exc).lower()
            task.status = "canceled" if cancelled else "failed"
            task.error_json = {"message": str(exc)}
            task.finished_at = utc_now()
            task.updated_at = utc_now()
            db.add(task)
            db.commit()
            _append_event(db, task, task.status, _task_payload(task))
    finally:
        with _process_lock:
            _processes.pop(task_id, None)


def _stream_task(
    task_id: str,
    *,
    request_id: Any,
    after_event_id: str | None,
) -> StreamingResponse:
    def generate() -> Iterator[str]:
        after = _event_sequence(after_event_id)
        last_heartbeat = time.monotonic()
        while True:
            emitted = False
            with Session(engine) as db:
                events = list(
                    db.exec(
                        select(A2ATaskEvent)
                        .where(A2ATaskEvent.run_id == task_id, A2ATaskEvent.sequence > after)
                        .order_by(A2ATaskEvent.sequence)
                    ).all()
                )
                task = db.get(A2ATaskRun, task_id)
            for event in events:
                after = event.sequence
                emitted = True
                yield f"id: {event.sequence}\ndata: {json.dumps(_envelope(request_id, event.data_json), ensure_ascii=False)}\n\n"
            if task is None or (task.status in _TERMINAL and not events):
                break
            if not emitted and time.monotonic() - last_heartbeat >= 10:
                yield ": keep-alive\n\n"
                last_heartbeat = time.monotonic()
            time.sleep(0.15)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _latest_event_sequence(task_id: str, *, db: Session | None = None) -> int:
    def find(session: Session) -> int:
        event = session.exec(
            select(A2ATaskEvent)
            .where(A2ATaskEvent.run_id == task_id)
            .order_by(A2ATaskEvent.sequence.desc())
        ).first()
        return event.sequence if event is not None else 0

    if db is not None:
        return find(db)
    with Session(engine) as session:
        return find(session)


def _cancel(task_id: str) -> None:
    with Session(engine) as db:
        task = db.get(A2ATaskRun, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        task.cancel_requested = True
        task.status = "canceled"
        task.finished_at = utc_now()
        task.updated_at = utc_now()
        db.add(task)
        db.commit()
        _append_event(db, task, "canceled", _task_payload(task))
    with _process_lock:
        process = _processes.get(task_id)
    if process and process.poll() is None:
        process.terminate()


def _require_task(task_public_id: str) -> A2ATaskRun:
    with Session(engine) as db:
        task = db.exec(
            select(A2ATaskRun).where(
                A2ATaskRun.direction == "server",
                A2ATaskRun.remote_task_id == task_public_id,
            )
        ).first()
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        db.expunge(task)
        return task


def _task_for_message_id(message_id: str) -> A2ATaskRun | None:
    with Session(engine) as db:
        event = db.exec(
            select(A2ATaskEvent).where(A2ATaskEvent.external_event_id == message_id)
        ).first()
        task = db.get(A2ATaskRun, event.run_id) if event is not None else None
        if task is None:
            task = db.exec(
                select(A2ATaskRun).where(
                    A2ATaskRun.direction == "server",
                    A2ATaskRun.invocation_id == message_id,
                )
            ).first()
        if task is not None:
            db.expunge(task)
        return task


def _append_event(
    db: Session,
    task: A2ATaskRun,
    event_type: str,
    data: dict[str, Any],
    *,
    external_event_id: str | None = None,
) -> None:
    previous = db.exec(
        select(A2ATaskEvent)
        .where(A2ATaskEvent.run_id == task.id)
        .order_by(A2ATaskEvent.sequence.desc())
    ).first()
    db.add(
        A2ATaskEvent(
            tenant_id=task.tenant_id,
            run_id=task.id,
            sequence=(previous.sequence + 1) if previous else 1,
            external_event_id=external_event_id,
            event_type=event_type,
            data_json=data,
        )
    )
    db.commit()


def _task_payload(task: A2ATaskRun, *, request: Request | None = None) -> dict[str, Any]:
    state = task.status
    message_text = str(task.result_json.get("text") or task.error_json.get("message") or "")
    status: dict[str, Any] = {"state": state}
    if message_text:
        status["message"] = {
            "messageId": uuid.uuid4().hex,
            "role": "ROLE_AGENT",
            "parts": [{"text": message_text}],
        }
    artifacts = []
    for item in task.artifacts_json:
        path = str(item.get("path") or "")
        if not path:
            continue
        file_data = {
            "name": item.get("name") or Path(path).name,
            "mimeType": item.get("mime_type") or "application/octet-stream",
        }
        base_url = _artifact_base_url(task, request=request)
        if base_url:
            file_data["uri"] = (
                base_url
                + f"/api/a2a/codex/tasks/{task.remote_task_id}/artifacts/{quote(path)}"
            )
        artifacts.append(
            {
                "artifactId": str(item.get("artifact_id") or uuid.uuid4().hex),
                "name": file_data["name"],
                "parts": [{"file": file_data}],
            }
        )
    return {
        "id": task.remote_task_id,
        "contextId": task.context_id,
        "status": status,
        "artifacts": artifacts,
    }


def _status_update(task: A2ATaskRun, state: str) -> dict[str, Any]:
    return {
        "statusUpdate": {
            "taskId": task.remote_task_id,
            "contextId": task.context_id,
            "status": {"state": state},
            "final": state in _TERMINAL,
        }
    }


def _message_text(message: dict[str, Any]) -> str:
    texts = [
        str(part.get("text"))
        for part in message.get("parts") or []
        if isinstance(part, dict) and part.get("text") is not None
    ]
    return "\n".join(texts).strip()


def _materialize_message_files(message: dict[str, Any], workspace: Path) -> list[str]:
    import base64

    written: list[str] = []
    target_root = workspace / "attachments"
    for index, part in enumerate(message.get("parts") or [], start=1):
        if not isinstance(part, dict) or not isinstance(part.get("file"), dict):
            continue
        file_part = part["file"]
        encoded = file_part.get("bytes")
        if not isinstance(encoded, str) or not encoded:
            continue
        try:
            content = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise RuntimeError("A2A file part is not valid base64.") from exc
        filename = _safe_name(str(file_part.get("name") or f"attachment-{index}"))
        target_root.mkdir(parents=True, exist_ok=True)
        target = target_root / filename
        suffix = 2
        while target.exists():
            target = target_root / f"{Path(filename).stem}-{suffix}{Path(filename).suffix}"
            suffix += 1
        target.write_bytes(content)
        written.append(target.relative_to(workspace).as_posix())
    return written


def _codex_session_id(event: dict[str, Any]) -> str | None:
    if str(event.get("type") or "") in {"thread.started", "session.started"}:
        return str(event.get("thread_id") or event.get("session_id") or event.get("id") or "") or None
    return None


def _codex_text(event: dict[str, Any]) -> str:
    item = event.get("item")
    if isinstance(item, dict) and str(item.get("type") or "") in {"agent_message", "message"}:
        return str(item.get("text") or item.get("content") or "")
    if str(event.get("type") or "") in {"message.completed", "turn.completed"}:
        return str(event.get("message") or event.get("text") or "")
    return ""


def _workspace_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        path.relative_to(root).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }


def _collect_artifacts(root: Path, before: dict[str, tuple[int, int]]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        current = (path.stat().st_size, path.stat().st_mtime_ns)
        if before.get(relative) == current:
            continue
        artifacts.append(
            {
                "artifact_id": uuid.uuid4().hex,
                "path": relative,
                "name": path.name,
                "size": current[0],
                "mime_type": _mime_type(path),
            }
        )
    return artifacts


def _mime_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _safe_name(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in {"-", "_", "."} else "-"
        for character in Path(value.replace("\\", "/")).name
    ).strip(".-")
    return cleaned[:180] or "attachment"


def _artifact_base_url(task: A2ATaskRun, *, request: Request | None) -> str:
    if request is not None:
        return str(request.base_url).rstrip("/")
    parsed = urlsplit(task.endpoint_url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def _list_tasks(params: dict[str, Any], *, request: Request) -> dict[str, Any]:
    requested_context = str(params.get("contextId") or "").strip()
    limit = min(max(int(params.get("pageSize") or 50), 1), 100)
    with Session(engine) as db:
        statement = select(A2ATaskRun).where(A2ATaskRun.direction == "server")
        if requested_context:
            statement = statement.where(A2ATaskRun.context_id == requested_context)
        tasks = list(db.exec(statement.order_by(A2ATaskRun.created_at.desc()).limit(limit)).all())
    return {"tasks": [_task_payload(task, request=request) for task in tasks]}


def _event_sequence(value: str | None) -> int:
    try:
        return max(int(value or 0), 0)
    except ValueError:
        return 0


def _envelope(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error_envelope(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _enabled_settings():
    settings = get_settings()
    if not settings.codex_a2a_enabled:
        raise HTTPException(status_code=404, detail="Codex A2A adapter is disabled")
    return settings


def _authorize(expected: str, authorization: str | None) -> None:
    if expected and authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Invalid A2A adapter credential")
