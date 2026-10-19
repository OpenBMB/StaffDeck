"""分享访客令牌的可达范围：对话链路放行，定时任务写入路径一律拒绝。

背景：定时任务的创建/草案接口挂在 `/api/chat/` 前缀下（`chat_router` /
`chat_draft_router`），而分享访客的路径白名单原来只按 `/api/chat/` 放行，
等于把「创建长期后台任务」这件事交给了任何拿到分享链接的人。
"""

from __future__ import annotations

from app.api.chat import _apply_share_scope, _maybe_handle_scheduled_task_request
from app.main import share_scope_allows_path
from app.security.auth import ShareScope
from app.session.session_schema import ChatTurnRequest


def _share_scope() -> ShareScope:
    return ShareScope(
        share_id="agentshare_1",
        share_token="tok",
        tenant_id="tenant_demo",
        agent_id="agent_share",
        model_config_id="model_share",
        visitor_id="share_guest_1",
    )


def test_share_scope_allows_chat_turn_paths():
    assert share_scope_allows_path("/api/chat/stream")
    assert share_scope_allows_path("/api/chat/stream/abc/events")
    assert share_scope_allows_path("/api/chat/turn")
    assert share_scope_allows_path("/api/public/agent-shares/tok")


def test_share_scope_denies_scheduled_task_paths():
    # 任务本体的增删改查
    assert not share_scope_allows_path("/api/chat/scheduled-tasks")
    assert not share_scope_allows_path("/api/chat/scheduled-tasks/")
    # 草案生成（定时任务模式的入口）
    assert not share_scope_allows_path("/api/chat/scheduled-task-drafts")


def test_share_scope_denies_unrelated_tenant_paths():
    assert not share_scope_allows_path("/api/enterprise/scheduled-tasks")
    assert not share_scope_allows_path("/api/enterprise/agents")


def test_apply_share_scope_downgrades_scheduled_task_mode():
    request = ChatTurnRequest(
        tenant_id="tenant_demo",
        agent_id="agent_attacker",
        model_config_id="model_attacker",
        message="每天早上 9 点帮我发日报",
        interaction_mode="scheduled_task",
    )

    scoped = _apply_share_scope(request, _share_scope())

    assert scoped.interaction_mode == "normal"
    # 员工与模型仍被钉死在分享时锁定的那一个
    assert scoped.agent_id == "agent_share"
    assert scoped.model_config_id == "model_share"


def test_apply_share_scope_keeps_normal_mode_untouched():
    request = ChatTurnRequest(
        tenant_id="tenant_demo",
        agent_id="agent_share",
        model_config_id="model_share",
        message="你好",
    )

    scoped = _apply_share_scope(request, _share_scope())

    assert scoped.interaction_mode == "normal"


def test_apply_share_scope_is_noop_without_share_token():
    request = ChatTurnRequest(
        tenant_id="tenant_demo",
        agent_id="agent_1",
        message="每天早上 9 点帮我发日报",
        interaction_mode="scheduled_task",
    )

    # 站内登录令牌：FastAPI 依赖默认值不是 ShareScope，必须原样放行
    assert _apply_share_scope(request, None) is request


def test_scheduled_task_shortcut_requires_scheduled_task_mode():
    request = ChatTurnRequest(
        tenant_id="tenant_demo",
        agent_id="agent_share",
        message="每天早上 9 点帮我发日报",
        interaction_mode="normal",
    )

    assert _maybe_handle_scheduled_task_request(None, request, None) is None  # type: ignore[arg-type]
