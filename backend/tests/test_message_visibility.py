from app.db.models import Message
from app.session.message_visibility import visible_message_content, visible_message_rows


def _message(
    message_id: str,
    role: str,
    content: str,
    metadata: dict | None = None,
) -> Message:
    return Message(
        id=message_id,
        tenant_id="tenant_demo",
        session_id="session_team",
        role=role,
        content=content,
        metadata_json=metadata or {},
    )


def test_legacy_team_prompt_is_projected_back_to_the_original_user_message() -> None:
    row = _message(
        "msg_user",
        "user",
        (
            "你是团队「增长」的 TL。\n"
            "团队花名册:\n- agent_worker\n"
            "人的需求:帮我调研竞品\n"
            "派发任务的唯一方式是输出 JSON。"
        ),
        {"interaction_mode": "team_tl"},
    )

    assert visible_message_content(row) == "帮我调研竞品"


def test_internal_repair_turn_and_its_assistant_reply_are_hidden() -> None:
    visible_user = _message("msg_visible", "user", "帮我调研竞品")
    internal_user = _message(
        "msg_internal",
        "user",
        "系统提示:请重试",
        {"interaction_mode": "team_tl", "message_visibility": "internal"},
    )
    internal_reply = _message(
        "msg_internal_reply",
        "assistant",
        "内部格式修复结果",
        {"user_message_id": internal_user.id},
    )
    visible_reply = _message(
        "msg_visible_reply",
        "assistant",
        "已经开始处理",
        {"user_message_id": visible_user.id},
    )

    assert visible_message_rows(
        [visible_user, internal_user, internal_reply, visible_reply]
    ) == [visible_user, visible_reply]


def test_legacy_repair_prompt_is_hidden_without_new_visibility_metadata() -> None:
    internal_user = _message(
        "msg_legacy_internal",
        "user",
        (
            "你是团队「增长」的 TL。\n人的需求:帮我调研竞品\n"
            "系统提示:你的上一条回复没有包含规定的 ```json 任务代码块,因此没有创建任何任务。"
        ),
        {"interaction_mode": "team_tl"},
    )
    internal_reply = _message(
        "msg_legacy_reply",
        "assistant",
        "内部重试回复",
        {"turn_id": internal_user.id},
    )

    assert visible_message_rows([internal_user, internal_reply]) == []
