from __future__ import annotations

from collections import deque
from typing import Any


class _RuntimeSession:
    """模拟由 Codex 管理认证状态的 app-server 会话。"""

    def __init__(self, *, account: dict[str, Any] | None = None, login_id: str | None = None) -> None:
        """初始化可观察的账户结果、登录 ID、通知队列和操作记录。"""
        self.account = account or {"account": None, "requiresOpenaiAuth": True}
        self.login_id = login_id
        self.calls: list[tuple[str, str | None]] = []
        self.notifications: deque[dict[str, Any]] = deque()
        self.closed = False

    def account_read(self) -> dict[str, Any]:
        """返回 runtime 管理的安全账户信息。"""
        self.calls.append(("account/read", None))
        return self.account

    def account_login_start(self) -> dict[str, Any]:
        """返回 runtime 创建的登录 ID，不含任何 OAuth token。"""
        self.calls.append(("account/login/start", None))
        return {
            "type": "chatgpt",
            "loginId": self.login_id,
            "authUrl": "https://auth.openai.example/runtime-managed-login",
        }

    def account_login_cancel(self, login_id: str) -> dict[str, Any]:
        """记录 runtime 登录取消请求。"""
        self.calls.append(("account/login/cancel", login_id))
        return {}

    def account_logout(self) -> dict[str, Any]:
        """记录 runtime 账户退出请求。"""
        self.calls.append(("account/logout", None))
        return {}

    def take_notification(self) -> dict[str, Any] | None:
        """按轮询顺序返回一个 runtime 通知。"""
        return self.notifications.popleft() if self.notifications else None

    def close(self) -> None:
        """记录会话资源已由服务关闭。"""
        self.closed = True


def test_subscription_service_uses_runtime_managed_account_and_login() -> None:
    """订阅状态和浏览器登录只委托给 Codex runtime，服务不持有令牌存储。"""
    from app.codex_subscription.app_server import ChatGPTSubscriptionService

    connected = _RuntimeSession(
        account={
            "account": {"type": "chatgpt", "email": "user@example.test", "planType": "plus"},
            "requiresOpenaiAuth": False,
        }
    )
    pending = _RuntimeSession(login_id="runtime-login-id")
    sessions = deque([connected, pending])
    opened_urls: list[str] = []

    service = ChatGPTSubscriptionService(
        session_factory=sessions.popleft,
        browser_opener=opened_urls.append,
    )

    status = service.account_status()
    started = service.start_login()

    assert status.to_dict() == {
        "status": "connected",
        "plan_type": "plus",
        "message": "已连接本机 Codex 管理的 ChatGPT 订阅。",
    }
    assert started.to_dict() == {
        "status": "pending",
        "plan_type": None,
        "message": "请在 Codex 打开的浏览器页面完成 ChatGPT 登录。",
    }
    assert connected.calls == [("account/read", None)]
    assert connected.closed is True
    assert pending.calls == [("account/login/start", None)]
    assert pending.closed is False
    assert opened_urls == ["https://auth.openai.example/runtime-managed-login"]
    assert not hasattr(service, "_credential_store")


def test_subscription_service_finishes_cancels_and_logs_out_through_runtime_sessions() -> None:
    """登录完成、取消和退出只操作相应 runtime 会话，并总是关闭临时资源。"""
    from app.codex_subscription.app_server import ChatGPTSubscriptionService

    pending_to_finish = _RuntimeSession(login_id="finished-login")
    connected_after_login = _RuntimeSession(
        account={
            "account": {"type": "chatgpt", "email": "user@example.test", "planType": "pro"},
            "requiresOpenaiAuth": False,
        }
    )
    pending_to_cancel = _RuntimeSession(login_id="cancelled-login")
    logout_session = _RuntimeSession()
    sessions = deque(
        [
            pending_to_finish,
            connected_after_login,
            pending_to_cancel,
            logout_session,
        ]
    )
    service = ChatGPTSubscriptionService(
        session_factory=sessions.popleft,
        browser_opener=lambda _url: True,
    )

    assert service.start_login().status == "pending"
    pending_to_finish.notifications.append(
        {
            "method": "account/login/completed",
            "params": {"loginId": "finished-login", "success": True},
        }
    )
    assert service.account_status().to_dict()["plan_type"] == "pro"
    assert pending_to_finish.closed is True
    assert connected_after_login.closed is True

    assert service.start_login().status == "pending"
    assert service.cancel_login().status == "requires_login"
    assert pending_to_cancel.calls == [
        ("account/login/start", None),
        ("account/login/cancel", "cancelled-login"),
    ]
    assert pending_to_cancel.closed is True

    assert service.logout().status == "requires_login"
    assert logout_session.calls == [("account/logout", None)]
    assert logout_session.closed is True
