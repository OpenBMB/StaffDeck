from __future__ import annotations

import json
from collections.abc import Callable
from queue import Queue
from typing import Any

import pytest


class _FakeInput:
    """将 JSON-RPC 写入交给测试进程，以便它返回确定性的协议响应。"""

    def __init__(self, on_request: Callable[[dict[str, Any]], None]) -> None:
        """保存解析后的请求回调；写入非 JSON 视为测试失败。"""
        self._on_request = on_request

    def write(self, value: str) -> int:
        """解析一行 JSON-RPC 请求并同步安排其假响应。"""
        self._on_request(json.loads(value))
        return len(value)

    def flush(self) -> None:
        """模拟管道 flush；假进程没有缓冲层。"""


class _FakeOutput:
    """以队列模拟 app-server 的逐行标准输出。"""

    def __init__(self) -> None:
        """初始化会阻塞直到测试放入一行协议输出的队列。"""
        self._lines: Queue[str] = Queue()

    def put(self, payload: dict[str, Any]) -> None:
        """序列化一个协议消息并把它交给后台读取线程。"""
        self._lines.put(json.dumps(payload) + "\n")

    def readline(self) -> str:
        """返回下一个协议行；空字符串代表进程已经关闭。"""
        return self._lines.get()


class _FakeProcess:
    """提供 Codex app-server 进程边界所需的最小可观察行为。"""

    def __init__(self) -> None:
        """初始化可检查的标准输入、输出、请求记录和终止标记。"""
        self.stdout = _FakeOutput()
        self.stdin = _FakeInput(self._respond)
        self.requests: list[dict[str, Any]] = []
        self.terminated = False
        self.environment: dict[str, str] | None = None

    def _respond(self, request: dict[str, Any]) -> None:
        """按方法写回 schema 兼容的初始化或账户响应。"""
        self.requests.append(request)
        method = request["method"]
        if method == "initialize":
            result: dict[str, Any] = {
                "codexHome": "/tmp/codex-home",
                "platformFamily": "unix",
                "platformOs": "macos",
                "userAgent": "codex-cli/test",
            }
        elif method == "account/read":
            result = {
                "account": {"type": "chatgpt", "email": "user@example.test", "planType": "plus"},
                "requiresOpenaiAuth": False,
            }
        elif method == "thread/start":
            result = {"thread": {"id": "thread-1"}}
        elif method == "turn/start":
            result = {"turn": {"id": "turn-1"}}
        elif method == "model/list":
            result = {
                "data": [
                    {
                        "id": "gpt-5.6-terra",
                        "model": "gpt-5.6-terra",
                        "displayName": "GPT-5.6-Terra",
                        "hidden": False,
                        "isDefault": True,
                        "defaultReasoningEffort": "medium",
                        "description": "Balanced agentic coding model.",
                        "supportedReasoningEfforts": [],
                    },
                ],
                "nextCursor": None,
            }
        else:
            result = {}
        self.stdout.put({"id": request["id"], "result": result})

    def poll(self) -> int | None:
        """在 terminate 前保持运行，模拟健康的子进程。"""
        return 0 if self.terminated else None

    def terminate(self) -> None:
        """记录清理请求并唤醒仍在读取标准输出的线程。"""
        self.terminated = True
        self.stdout._lines.put("")

    def wait(self, timeout: float | None = None) -> int:
        """立即确认假进程已经退出。"""
        del timeout
        return 0


def test_session_initializes_reads_account_and_removes_platform_api_key() -> None:
    """本地订阅会话仅通过 Codex JSON-RPC 读取安全账户状态并清理 API Key 环境。"""
    from app.codex_subscription.app_server import CodexAppServerSession

    process = _FakeProcess()

    def process_factory(command: list[str], environment: dict[str, str]) -> _FakeProcess:
        """记录运行命令和环境后返回确定性的本地假进程。"""
        assert command == ["/opt/codex", "app-server"]
        process.environment = environment
        return process

    session = CodexAppServerSession(
        command="/opt/codex",
        timeout_seconds=1,
        command_resolver=lambda configured: configured,
        process_factory=process_factory,
        environment={"OPENAI_API_KEY": "platform-key", "PATH": "/usr/bin"},
    )

    account = session.account_read()
    session.close()

    assert account["account"]["type"] == "chatgpt"
    assert account["account"]["planType"] == "plus"
    assert [request["method"] for request in process.requests] == ["initialize", "account/read"]
    assert process.environment == {"PATH": "/usr/bin"}
    assert process.terminated is True


def test_session_lists_available_models() -> None:
    """model/list 是只读目录查询，返回本机 Codex 管理的可用模型。"""
    from app.codex_subscription.app_server import CodexAppServerSession

    process = _FakeProcess()

    session = CodexAppServerSession(
        command="/opt/codex",
        timeout_seconds=1,
        command_resolver=lambda configured: configured,
        process_factory=lambda command, environment: process,
        environment={},
    )

    response = session.model_list()
    session.close()

    assert response["data"][0]["id"] == "gpt-5.6-terra"
    assert response["data"][0]["displayName"] == "GPT-5.6-Terra"
    assert [request["method"] for request in process.requests] == ["initialize", "model/list"]


def test_session_resolves_the_configured_codex_command_before_starting() -> None:
    """运行时启动前解析配置命令，避免依赖不可见的服务进程 PATH 差异。"""
    from app.codex_subscription.app_server import CodexAppServerSession

    process = _FakeProcess()
    observed_command: list[str] | None = None

    def process_factory(command: list[str], _environment: dict[str, str]) -> _FakeProcess:
        """保存解析后的启动命令并返回确定性的本地假进程。"""
        nonlocal observed_command
        observed_command = command
        return process

    session = CodexAppServerSession(
        command="codex",
        timeout_seconds=1,
        command_resolver=lambda configured: "/Applications/Codex/bin/codex"
        if configured == "codex"
        else None,
        process_factory=process_factory,
        environment={"PATH": "/usr/bin"},
    )
    session.close()

    assert observed_command == ["/Applications/Codex/bin/codex", "app-server"]


def test_session_starts_a_restricted_thread_turn_and_receives_notifications() -> None:
    """模型调用通过安全的 app-server thread/turn 参数执行，并保留服务端通知顺序。"""
    from app.codex_subscription.app_server import CodexAppServerSession

    process = _FakeProcess()
    session = CodexAppServerSession(
        command="/opt/codex",
        timeout_seconds=1,
        command_resolver=lambda configured: configured,
        process_factory=lambda _command, _environment: process,
        environment={"PATH": "/usr/bin"},
    )

    thread = session.thread_start("gpt-5.1-codex")
    turn = session.turn_start("thread-1", "[user]\n总结本周")
    process.stdout.put(
        {
            "method": "agentMessage/delta",
            "params": {"threadId": "thread-1", "turnId": "turn-1", "delta": "已总结"},
        }
    )
    notification = session.wait_for_notification()
    session.close()

    assert thread == {"thread": {"id": "thread-1"}}
    assert turn == {"turn": {"id": "turn-1"}}
    assert process.requests[1:3] == [
        {
            "id": 2,
            "method": "thread/start",
            "params": {
                "model": "gpt-5.1-codex",
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "ephemeral": True,
            },
        },
        {
            "id": 3,
            "method": "turn/start",
            "params": {
                "threadId": "thread-1",
                "input": [{"type": "text", "text": "[user]\n总结本周"}],
            },
        },
    ]
    assert notification["params"]["delta"] == "已总结"


def test_session_maps_malformed_runtime_messages_to_a_safe_protocol_error() -> None:
    """app-server 输出畸形 JSON 时，调用方只收到稳定错误码且会话仍能安全关闭。"""
    from app.codex_subscription.app_server import CodexAppServerSession, CodexSubscriptionError

    process = _FakeProcess()
    session = CodexAppServerSession(
        command="/opt/codex",
        timeout_seconds=1,
        command_resolver=lambda configured: configured,
        process_factory=lambda _command, _environment: process,
        environment={"PATH": "/usr/bin"},
    )
    process.stdout._lines.put("{not-json}\\n")

    with pytest.raises(CodexSubscriptionError, match="MODEL_SUBSCRIPTION_RUNTIME_PROTOCOL_ERROR"):
        session.account_read()

    session.close()
