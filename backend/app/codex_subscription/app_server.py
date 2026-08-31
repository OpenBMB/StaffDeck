"""通过本机 Codex app-server 提供 ChatGPT 订阅状态和会话，不管理订阅凭据。"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.config import get_settings


class CodexSubscriptionError(Exception):
    """表示可安全展示的本机 Codex 订阅运行时错误码。"""

    def __init__(self, code: str) -> None:
        """保存稳定错误码；异常文本不包含运行时输出或认证信息。"""
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CodexSubscriptionAccount:
    """保存可发送给浏览器的订阅状态，不包含账户标识或认证信息。"""

    status: str
    plan_type: str | None
    message: str

    def to_dict(self) -> dict[str, str | None]:
        """转换为既有 HTTP API 使用的稳定字段集合。"""
        return {
            "status": self.status,
            "plan_type": self.plan_type,
            "message": self.message,
        }


@dataclass
class _PendingLogin:
    """保存尚未完成的 Codex 管理登录会话；仅存在于当前进程内存。"""

    login_id: str
    session: Any


class CodexAppServerSession:
    """通过单个本机 Codex app-server 进程执行 JSON-RPC 请求。"""

    def __init__(
        self,
        *,
        command: str,
        timeout_seconds: float,
        command_resolver: Callable[[str], str | None] | None = None,
        process_factory: Callable[[list[str], dict[str, str]], Any] | None = None,
        environment: dict[str, str] | None = None,
    ) -> None:
        """启动受限子进程并完成初始化；启动失败抛出安全错误码。"""
        self._timeout_seconds = timeout_seconds
        self._condition = threading.Condition()
        self._responses: dict[int, dict[str, Any]] = {}
        self._notifications: queue.Queue[dict[str, Any]] = queue.Queue()
        self._reader_error: CodexSubscriptionError | None = None
        self._closed = False
        self._next_request_id = 1
        child_environment = dict(environment or os.environ)
        child_environment.pop("OPENAI_API_KEY", None)
        resolver = command_resolver or _resolve_codex_command
        resolved_command = resolver(command)
        if not resolved_command:
            raise CodexSubscriptionError("MODEL_SUBSCRIPTION_RUNTIME_UNAVAILABLE")
        factory = process_factory or _start_codex_app_server
        try:
            self._process = factory([resolved_command, "app-server"], child_environment)
        except (OSError, ValueError) as exc:
            raise CodexSubscriptionError("MODEL_SUBSCRIPTION_RUNTIME_UNAVAILABLE") from exc
        self._reader = threading.Thread(target=self._read_messages, daemon=True)
        self._reader.start()
        self._request(
            "initialize",
            {"clientInfo": {"name": "StaffDeck", "version": "local"}},
        )

    def account_read(self) -> dict[str, Any]:
        """读取 Codex 管理的账户状态；结果仅在进程内进行安全状态映射。"""
        return self._request("account/read", {})

    def account_login_start(self) -> dict[str, Any]:
        """请求 Codex 启动其管理的 ChatGPT 浏览器登录，不传递任何认证令牌。"""
        return self._request("account/login/start", {"type": "chatgpt"})

    def account_login_cancel(self, login_id: str) -> dict[str, Any]:
        """取消给定的运行时登录请求；登录 ID 只在内存会话中使用。"""
        return self._request("account/login/cancel", {"loginId": login_id})

    def account_logout(self) -> dict[str, Any]:
        """请求 Codex 清除其自身管理的登录状态，不访问 StaffDeck 数据库。"""
        return self._request("account/logout", {})

    def thread_start(self, model: str) -> dict[str, Any]:
        """创建只读、无需批准的临时线程；模型名必须来自已保存的模型配置。"""
        return self._request(
            "thread/start",
            {
                "model": model,
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "ephemeral": True,
            },
        )

    def turn_start(self, thread_id: str, prompt: str) -> dict[str, Any]:
        """在指定线程中提交纯文本任务；JSON-RPC 校验或运行时失败会抛出安全错误码。"""
        return self._request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
            },
        )

    def wait_for_notification(self) -> dict[str, Any]:
        """等待下一条服务端通知；超时、协议中断或会话关闭都会返回安全错误码。"""
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            with self._condition:
                if self._reader_error is not None:
                    raise self._reader_error
                if self._closed:
                    raise CodexSubscriptionError("MODEL_SUBSCRIPTION_RUNTIME_UNAVAILABLE")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexSubscriptionError("MODEL_SUBSCRIPTION_RUNTIME_TIMEOUT")
            try:
                return self._notifications.get(timeout=remaining)
            except queue.Empty:
                continue

    def take_notification(self) -> dict[str, Any] | None:
        """非阻塞读取一个运行时通知；无通知时返回空而不改变会话状态。"""
        try:
            return self._notifications.get_nowait()
        except queue.Empty:
            return None

    def close(self) -> None:
        """终止本会话的子进程；重复调用安全且不影响其他 Codex 会话。"""
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._condition.notify_all()
        if self._process.poll() is not None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            kill = getattr(self._process, "kill", None)
            if callable(kill):
                kill()
                self._process.wait(timeout=1)

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """发送一个请求并等待同 ID 响应；超时和协议错误均收敛为安全错误码。"""
        with self._condition:
            if self._closed:
                raise CodexSubscriptionError("MODEL_SUBSCRIPTION_RUNTIME_UNAVAILABLE")
            request_id = self._next_request_id
            self._next_request_id += 1
        payload = {"id": request_id, "method": method, "params": params}
        try:
            self._process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self._process.stdin.flush()
        except (AttributeError, OSError, ValueError) as exc:
            raise CodexSubscriptionError("MODEL_SUBSCRIPTION_RUNTIME_UNAVAILABLE") from exc
        deadline = time.monotonic() + self._timeout_seconds
        with self._condition:
            while request_id not in self._responses:
                if self._reader_error is not None:
                    raise self._reader_error
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CodexSubscriptionError("MODEL_SUBSCRIPTION_RUNTIME_TIMEOUT")
                self._condition.wait(timeout=remaining)
            response = self._responses.pop(request_id)
        if "error" in response:
            raise CodexSubscriptionError("MODEL_SUBSCRIPTION_RUNTIME_FAILED")
        result = response.get("result")
        if not isinstance(result, dict):
            raise CodexSubscriptionError("MODEL_SUBSCRIPTION_RUNTIME_PROTOCOL_ERROR")
        return result

    def _read_messages(self) -> None:
        """持续读取逐行 JSON-RPC 消息，分别分派响应和服务端通知。"""
        try:
            while True:
                line = self._process.stdout.readline()
                if not line:
                    break
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise TypeError("JSON-RPC message must be an object")
                message_id = message.get("id")
                if isinstance(message_id, int):
                    with self._condition:
                        self._responses[message_id] = message
                        self._condition.notify_all()
                    continue
                if isinstance(message.get("method"), str):
                    self._notifications.put(message)
        except (TypeError, ValueError, json.JSONDecodeError):
            with self._condition:
                self._reader_error = CodexSubscriptionError(
                    "MODEL_SUBSCRIPTION_RUNTIME_PROTOCOL_ERROR"
                )
                self._condition.notify_all()
            return
        with self._condition:
            if not self._closed:
                self._reader_error = CodexSubscriptionError(
                    "MODEL_SUBSCRIPTION_RUNTIME_UNAVAILABLE"
                )
            self._condition.notify_all()


class ChatGPTSubscriptionService:
    """协调 Codex 管理的账户状态和浏览器登录，不保存任何订阅凭据。"""

    def __init__(
        self,
        *,
        command: str = "codex",
        timeout_seconds: float = 30.0,
        session_factory: Callable[[], Any] | None = None,
        browser_opener: Callable[[str], Any] | None = None,
    ) -> None:
        """注入可测试的会话工厂；默认按当前配置打开一个本地 Codex 会话。"""
        self._lock = threading.RLock()
        self._pending: _PendingLogin | None = None
        self._browser_opener = browser_opener or webbrowser.open
        self._session_factory = session_factory or (
            lambda: CodexAppServerSession(
                command=command,
                timeout_seconds=timeout_seconds,
            )
        )

    def account_status(self) -> CodexSubscriptionAccount:
        """返回当前安全账户状态；完成的登录由运行时通知推动，不读取本地凭据表。"""
        with self._lock:
            if self._pending is not None:
                completed = self._consume_login_completion_locked()
                if completed is None:
                    return _pending_account()
                if not completed:
                    return _requires_login_account()
            return self._read_account_locked()

    def start_login(self) -> CodexSubscriptionAccount:
        """启动 Codex 管理的 ChatGPT 登录并保留会话直到完成或取消。"""
        with self._lock:
            if self._pending is not None:
                return _pending_account()
            session = self._session_factory()
            try:
                result = session.account_login_start()
                login_id = result.get("loginId")
                auth_url = result.get("authUrl")
                if not isinstance(login_id, str) or not login_id:
                    raise CodexSubscriptionError("MODEL_SUBSCRIPTION_RUNTIME_PROTOCOL_ERROR")
                if not isinstance(auth_url, str) or not auth_url:
                    raise CodexSubscriptionError("MODEL_SUBSCRIPTION_RUNTIME_PROTOCOL_ERROR")
                if self._browser_opener(auth_url) is False:
                    raise CodexSubscriptionError("MODEL_SUBSCRIPTION_BROWSER_UNAVAILABLE")
            except CodexSubscriptionError:
                session.close()
                raise
            except Exception as exc:
                session.close()
                raise CodexSubscriptionError("MODEL_SUBSCRIPTION_BROWSER_UNAVAILABLE") from exc
            self._pending = _PendingLogin(login_id=login_id, session=session)
            return _pending_account()

    def cancel_login(self) -> CodexSubscriptionAccount:
        """取消未完成的运行时登录并释放其本地子进程。"""
        with self._lock:
            self._discard_pending_locked(cancel=True)
            return _requires_login_account()

    def logout(self) -> CodexSubscriptionAccount:
        """让 Codex 清除自身登录并释放本服务持有的临时登录会话。"""
        with self._lock:
            self._discard_pending_locked(cancel=True)
            session = self._session_factory()
            try:
                session.account_logout()
            finally:
                session.close()
            return _requires_login_account()

    def create_session(self) -> Any:
        """创建供 LLM 协议驱动使用的独立运行时会话；调用方必须在完成后关闭。"""
        return self._session_factory()

    def close(self) -> None:
        """应用关闭时仅取消未完成登录并关闭其会话，不改变 Codex 的已登录账户。"""
        with self._lock:
            self._discard_pending_locked(cancel=True)

    def _read_account_locked(self) -> CodexSubscriptionAccount:
        """使用短生命周期会话读取账户并立即关闭，以隔离状态检查与模型请求。"""
        session = self._session_factory()
        try:
            payload = session.account_read()
        finally:
            session.close()
        return _account_from_runtime_payload(payload)

    def _consume_login_completion_locked(self) -> bool | None:
        """处理一个匹配的登录完成通知；无通知保持 pending，失败通知回到未登录。"""
        pending = self._pending
        if pending is None:
            return None
        notification = pending.session.take_notification()
        if notification is None:
            return None
        if notification.get("method") != "account/login/completed":
            return None
        params = notification.get("params")
        login_id = params.get("loginId") if isinstance(params, dict) else None
        if login_id not in {None, pending.login_id}:
            return None
        success = bool(params.get("success")) if isinstance(params, dict) else False
        self._discard_pending_locked(cancel=False)
        return success

    def _discard_pending_locked(self, *, cancel: bool) -> None:
        """释放 pending 会话；取消请求失败不掩盖关闭资源这一必要清理动作。"""
        pending = self._pending
        self._pending = None
        if pending is None:
            return
        try:
            if cancel:
                pending.session.account_login_cancel(pending.login_id)
        except CodexSubscriptionError:
            pass
        finally:
            pending.session.close()


def _start_codex_app_server(command: list[str], environment: dict[str, str]) -> subprocess.Popen[str]:
    """启动 app-server 并建立逐行文本管道；该函数不执行登录或网络请求。"""
    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=environment,
    )


def _resolve_codex_command(configured_command: str) -> str | None:
    """把配置中的命令名或路径解析为可执行绝对路径；不可执行目标返回空。"""
    command = configured_command.strip()
    if not command:
        return None
    candidate = shutil.which(command)
    return os.path.realpath(candidate) if candidate else None


def _account_from_runtime_payload(payload: Any) -> CodexSubscriptionAccount:
    """将 app-server 账户响应缩减为安全 UI 状态，不返回邮箱或账户标识。"""
    if not isinstance(payload, dict):
        raise CodexSubscriptionError("MODEL_SUBSCRIPTION_RUNTIME_PROTOCOL_ERROR")
    account = payload.get("account")
    if not isinstance(account, dict) or account.get("type") != "chatgpt":
        return _requires_login_account()
    plan_type = account.get("planType")
    return _connected_account(plan_type if isinstance(plan_type, str) else None)


def _connected_account(plan_type: str | None) -> CodexSubscriptionAccount:
    """构建不会泄露身份信息的已连接状态。"""
    return CodexSubscriptionAccount(
        status="connected",
        plan_type=plan_type,
        message="已连接本机 Codex 管理的 ChatGPT 订阅。",
    )


def _pending_account() -> CodexSubscriptionAccount:
    """构建等待 Codex 浏览器登录完成的安全状态。"""
    return CodexSubscriptionAccount(
        status="pending",
        plan_type=None,
        message="请在 Codex 打开的浏览器页面完成 ChatGPT 登录。",
    )


def _requires_login_account() -> CodexSubscriptionAccount:
    """构建未登录状态并提示用户从 Codex 管理的流程重新连接。"""
    return CodexSubscriptionAccount(
        status="requires_login",
        plan_type=None,
        message="本机 Codex 尚未登录 ChatGPT 订阅。",
    )


_subscription_service: ChatGPTSubscriptionService | None = None
_subscription_service_lock = threading.Lock()


def get_codex_subscription_service() -> ChatGPTSubscriptionService:
    """返回进程共享的本机 Codex 服务；只读取命令和超时配置。"""
    global _subscription_service
    with _subscription_service_lock:
        if _subscription_service is None:
            settings = get_settings()
            _subscription_service = ChatGPTSubscriptionService(
                command=getattr(settings, "codex_subscription_command", "codex"),
                timeout_seconds=settings.codex_subscription_timeout_seconds,
            )
        return _subscription_service


def stop_codex_subscription_service() -> None:
    """关闭进程共享服务的暂存登录会话，供应用关闭与测试重置使用。"""
    global _subscription_service
    with _subscription_service_lock:
        if _subscription_service is not None:
            _subscription_service.close()
        _subscription_service = None
