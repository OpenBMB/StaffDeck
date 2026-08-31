from __future__ import annotations

from collections import deque
from typing import Any

import pytest

from app.llm.protocol_drivers import ProtocolCallError


class _GenerationSession:
    """模拟 app-server thread/turn 调用和按顺序到达的运行时通知。"""

    def __init__(self, notifications: list[dict[str, Any]]) -> None:
        """保存通知队列和每个可观察的 thread/turn 调用。"""
        self.notifications = deque(notifications)
        self.calls: list[tuple[str, Any]] = []
        self.closed = False

    def thread_start(self, model: str) -> dict[str, Any]:
        """返回由 runtime 创建的线程 ID，并记录安全的模型选择。"""
        self.calls.append(("thread/start", model))
        return {"thread": {"id": "thread-1"}}

    def turn_start(self, thread_id: str, prompt: str) -> dict[str, Any]:
        """返回由 runtime 创建的 turn ID，并记录合并后的纯文本输入。"""
        self.calls.append(("turn/start", {"thread_id": thread_id, "prompt": prompt}))
        return {"turn": {"id": "turn-1"}}

    def wait_for_notification(self) -> dict[str, Any]:
        """返回下一个通知；测试缺少终止通知时立即失败。"""
        return self.notifications.popleft()

    def close(self) -> None:
        """记录驱动在任意终态后关闭了短生命周期会话。"""
        self.closed = True


def test_driver_completes_a_prompt_with_a_runtime_agent_message() -> None:
    """非流式订阅请求将标准消息压缩为一个 runtime text turn 并返回最终助手文本。"""
    from app.llm.protocol_drivers import CodexAppServerDriver

    session = _GenerationSession(
        [
            {
                "method": "item/agentMessage/delta",
                "params": {"threadId": "thread-1", "turnId": "turn-1", "delta": "草稿"},
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {
                        "id": "turn-1",
                        "status": "completed",
                        "items": [{"id": "item-1", "type": "agentMessage", "text": "最终回复"}],
                    },
                },
            },
        ]
    )
    driver = CodexAppServerDriver(session_factory=lambda: session)

    completion = driver.complete(
        {
            "model": "gpt-5.1-codex",
            "messages": [
                {"role": "system", "content": "遵循团队规范"},
                {"role": "user", "content": "整理本周计划"},
            ],
            "temperature": 0.2,
            "max_tokens": 512,
        }
    )

    assert completion.choices[0].message.content == "最终回复"
    assert session.calls == [
        ("thread/start", "gpt-5.1-codex"),
        (
            "turn/start",
            {"thread_id": "thread-1", "prompt": "[system]\n遵循团队规范\n\n[user]\n整理本周计划"},
        ),
    ]
    assert session.closed is True


def test_driver_streams_runtime_text_deltas_then_emits_a_terminal_chunk() -> None:
    """流式订阅请求保持 runtime delta 顺序，并以终态 chunk 标示完整结束。"""
    from app.llm.protocol_drivers import CodexAppServerDriver

    session = _GenerationSession(
        [
            {
                "method": "item/agentMessage/delta",
                "params": {"threadId": "thread-1", "turnId": "turn-1", "delta": "你好"},
            },
            {
                "method": "item/agentMessage/delta",
                "params": {"threadId": "thread-1", "turnId": "turn-1", "delta": "，世界"},
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {
                        "id": "turn-1",
                        "status": "completed",
                        "items": [{"id": "item-1", "type": "agentMessage", "text": "你好，世界"}],
                    },
                },
            },
        ]
    )
    driver = CodexAppServerDriver(session_factory=lambda: session)

    chunks = list(
        driver.stream(
            {
                "model": "gpt-5.1-codex",
                "messages": [{"role": "user", "content": "问候"}],
                "temperature": 0.2,
                "max_tokens": 512,
            }
        )
    )

    assert [chunk.choices[0].delta.content for chunk in chunks[:2]] == ["你好", "，世界"]
    assert chunks[-1].choices[0].finish_reason == "stop"
    assert session.closed is True


def test_driver_maps_a_failed_runtime_turn_to_a_safe_protocol_error() -> None:
    """失败 turn 不能伪装为正常完成，LLM 层只接收可展示的稳定错误码。"""
    from app.llm.protocol_drivers import CodexAppServerDriver

    session = _GenerationSession(
        [
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "failed", "items": []},
                },
            }
        ]
    )
    driver = CodexAppServerDriver(session_factory=lambda: session)

    with pytest.raises(ProtocolCallError, match="MODEL_SUBSCRIPTION_RUNTIME_FAILED"):
        driver.complete(
            {
                "model": "gpt-5.1-codex",
                "messages": [{"role": "user", "content": "测试失败映射"}],
                "temperature": 0.2,
                "max_tokens": 512,
            }
        )

    assert session.closed is True
