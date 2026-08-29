from __future__ import annotations

import json
import queue
import subprocess
import tempfile
import threading
import webbrowser
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol

from app.config import get_settings


class CodexAppServerError(Exception):
    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class CodexSubscriptionAccount:
    status: str
    plan_type: str | None
    message: str

    def to_dict(self) -> dict[str, str | None]:
        return {
            "status": self.status,
            "plan_type": self.plan_type,
            "message": self.message,
        }


@dataclass(frozen=True)
class CodexTurnRequest:
    system_prompt: str
    user_prompt: str
    model: str
    ephemeral: bool = True
    sandbox: str = "read-only"
    approval_policy: str = "never"
    cancellation: Any = None


class JsonRpcTransport(Protocol):
    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...

    def close(self) -> None: ...


class _CodexAppServerStdioTransport:
    def __init__(self, command: list[str], timeout_seconds: float) -> None:
        self._command = command
        self._timeout_seconds = timeout_seconds
        self._process: subprocess.Popen[str] | None = None
        self._messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._request_id = 0
        self._initialized = False
        self._lock = threading.RLock()

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self._ensure_started()
            return self._request(method, params or {})

    def stream_turn(self, request: CodexTurnRequest) -> Iterator[str]:
        with self._lock:
            self._ensure_started()
            thread = self._request(
                "thread/start",
                {
                    "ephemeral": request.ephemeral,
                    "cwd": tempfile.gettempdir(),
                    "model": request.model,
                    "developerInstructions": request.system_prompt,
                    "sandbox": request.sandbox,
                    "approvalPolicy": request.approval_policy,
                },
            ).get("thread")
            if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
                raise CodexAppServerError("MODEL_SUBSCRIPTION_RUNTIME_UNAVAILABLE")
            thread_id = thread["id"]
            turn = self._request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": request.user_prompt}],
                    "model": request.model,
                    "approvalPolicy": request.approval_policy,
                    "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                },
            ).get("turn")
            if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                raise CodexAppServerError("MODEL_SUBSCRIPTION_RUNTIME_UNAVAILABLE")
            turn_id = turn["id"]
            while True:
                if getattr(request.cancellation, "cancelled", False):
                    self._request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
                    raise CodexAppServerError("MODEL_CANCELLED")
                try:
                    payload = self._messages.get(timeout=self._timeout_seconds)
                except queue.Empty as exc:
                    raise CodexAppServerError("MODEL_SUBSCRIPTION_RUNTIME_UNAVAILABLE") from exc
                if payload is None:
                    raise CodexAppServerError("MODEL_SUBSCRIPTION_RUNTIME_UNAVAILABLE")
                method = payload.get("method")
                params = payload.get("params")
                if not isinstance(params, dict):
                    continue
                if method == "item/agentMessage/delta":
                    delta = params.get("delta")
                    if isinstance(delta, str) and delta:
                        yield delta
                    continue
                if method != "turn/completed":
                    continue
                completed_turn = params.get("turn")
                if not isinstance(completed_turn, dict):
                    completed_turn = params
                if completed_turn.get("status") in {"completed", "success"}:
                    return
                error = completed_turn.get("error")
                raise CodexAppServerError(_safe_error_code(error))

    def close(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
            self._initialized = False
            if process is None or process.poll() is not None:
                return
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

    def _ensure_started(self) -> None:
        if self._process is not None and self._process.poll() is None:
            if not self._initialized:
                self._initialize()
            return
        try:
            self._process = subprocess.Popen(
                [*self._command, "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except (FileNotFoundError, OSError) as exc:
            raise CodexAppServerError("MODEL_SUBSCRIPTION_RUNTIME_UNAVAILABLE") from exc
        self._messages = queue.Queue()
        threading.Thread(target=self._read_stdout, daemon=True, name="codex-subscription-rpc").start()
        self._initialize()

    def _initialize(self) -> None:
        self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": "staffdeck",
                    "title": "StaffDeck",
                    "version": "1.0",
                }
            },
        )
        self._write({"method": "initialized", "params": {}})
        self._initialized = True

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            self._messages.put(None)
            return
        for line in process.stdout:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                self._messages.put(payload)
        self._messages.put(None)

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        self._write({"id": request_id, "method": method, "params": params})
        while True:
            try:
                payload = self._messages.get(timeout=self._timeout_seconds)
            except queue.Empty as exc:
                raise CodexAppServerError("MODEL_SUBSCRIPTION_RUNTIME_UNAVAILABLE") from exc
            if payload is None:
                raise CodexAppServerError("MODEL_SUBSCRIPTION_RUNTIME_UNAVAILABLE")
            if payload.get("id") != request_id:
                continue
            error = payload.get("error")
            if isinstance(error, dict):
                raise CodexAppServerError(_safe_error_code(error.get("message")))
            result = payload.get("result")
            return dict(result) if isinstance(result, dict) else {}

    def _write(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.poll() is not None or process.stdin is None:
            raise CodexAppServerError("MODEL_SUBSCRIPTION_RUNTIME_UNAVAILABLE")
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except OSError as exc:
            raise CodexAppServerError("MODEL_SUBSCRIPTION_RUNTIME_UNAVAILABLE") from exc


class CodexAppServer:
    def __init__(
        self,
        *,
        command: list[str] | None = None,
        timeout_seconds: float = 30.0,
        transport: JsonRpcTransport | None = None,
        browser_opener: Callable[[str], Any] | None = None,
    ) -> None:
        self._transport = transport or _CodexAppServerStdioTransport(
            command or ["codex"], timeout_seconds
        )
        self._browser_opener = browser_opener or webbrowser.open
        self._pending_login_id: str | None = None
        self._lock = threading.RLock()

    def account_status(self, *, refresh_token: bool = True) -> CodexSubscriptionAccount:
        with self._lock:
            try:
                result = self._transport.request("account/read", {"refreshToken": refresh_token})
            except CodexAppServerError:
                return _unavailable_account()
            account = result.get("account")
            if not isinstance(account, dict) or account.get("type") != "chatgpt":
                return _pending_account() if self._pending_login_id else _requires_login_account()
            self._pending_login_id = None
            plan_type = account.get("planType")
            return CodexSubscriptionAccount(
                status="connected",
                plan_type=str(plan_type) if isinstance(plan_type, str) else None,
                message="已连接 ChatGPT 订阅",
            )

    def start_login(self) -> CodexSubscriptionAccount:
        with self._lock:
            if self._pending_login_id:
                return _pending_account()
            result = self._transport.request(
                "account/login/start",
                {"type": "chatgpt", "appBrand": "chatgpt"},
            )
            login_id = result.get("loginId")
            auth_url = result.get("authUrl")
            if not isinstance(login_id, str) or not isinstance(auth_url, str):
                raise CodexAppServerError("MODEL_SUBSCRIPTION_RUNTIME_UNAVAILABLE")
            self._pending_login_id = login_id
            self._browser_opener(auth_url)
            return _pending_account()

    def cancel_login(self) -> CodexSubscriptionAccount:
        with self._lock:
            if self._pending_login_id:
                self._transport.request(
                    "account/login/cancel", {"loginId": self._pending_login_id}
                )
                self._pending_login_id = None
            return _requires_login_account()

    def logout(self) -> CodexSubscriptionAccount:
        with self._lock:
            self._transport.request("account/logout", {})
            self._pending_login_id = None
            return _requires_login_account()

    def require_connected(self) -> CodexSubscriptionAccount:
        account = self.account_status()
        if account.status == "connected":
            return account
        if account.status == "unavailable":
            raise CodexAppServerError("MODEL_SUBSCRIPTION_RUNTIME_UNAVAILABLE")
        raise CodexAppServerError("MODEL_SUBSCRIPTION_AUTH_REQUIRED")

    def stream_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        cancellation: Any = None,
    ) -> Iterator[str]:
        with self._lock:
            self.require_connected()
            stream_turn = getattr(self._transport, "stream_turn", None)
            if not callable(stream_turn):
                raise CodexAppServerError("MODEL_SUBSCRIPTION_RUNTIME_UNAVAILABLE")
            request = CodexTurnRequest(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=model,
                cancellation=cancellation,
            )
            yield from stream_turn(request)

    def close(self) -> None:
        close = getattr(self._transport, "close", None)
        if callable(close):
            close()


_app_server: CodexAppServer | None = None
_app_server_lock = threading.Lock()


def get_codex_app_server() -> CodexAppServer:
    global _app_server
    with _app_server_lock:
        if _app_server is None:
            settings = get_settings()
            _app_server = CodexAppServer(
                command=[settings.codex_subscription_command],
                timeout_seconds=settings.codex_subscription_timeout_seconds,
            )
        return _app_server


def stop_codex_app_server() -> None:
    global _app_server
    with _app_server_lock:
        if _app_server is not None:
            _app_server.close()
        _app_server = None


def _pending_account() -> CodexSubscriptionAccount:
    return CodexSubscriptionAccount(
        status="pending",
        plan_type=None,
        message="已在默认浏览器中打开 ChatGPT 授权页面",
    )


def _requires_login_account() -> CodexSubscriptionAccount:
    return CodexSubscriptionAccount(
        status="requires_login",
        plan_type=None,
        message="尚未连接 ChatGPT 订阅",
    )


def _unavailable_account() -> CodexSubscriptionAccount:
    return CodexSubscriptionAccount(
        status="unavailable",
        plan_type=None,
        message="本机 Codex 订阅运行时不可用",
    )


def _safe_error_code(value: Any) -> str:
    text = str(value or "").lower()
    if "cancel" in text:
        return "MODEL_CANCELLED"
    if any(term in text for term in ("permission", "access", "limit", "rate")):
        return "MODEL_SUBSCRIPTION_ACCESS_DENIED"
    if "auth" in text or "login" in text or "subscription" in text:
        return "MODEL_SUBSCRIPTION_AUTH_REQUIRED"
    return "MODEL_SUBSCRIPTION_RUNTIME_UNAVAILABLE"
