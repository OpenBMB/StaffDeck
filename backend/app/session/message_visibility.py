from __future__ import annotations

from collections.abc import Iterable

from app.db.models import Message

_LEGACY_TEAM_REQUEST_MARKER = "人的需求:"
_LEGACY_ASSIGNMENT_MARKER = "\n派发任务的唯一方式"
_LEGACY_REPAIR_MARKER = "系统提示:你的上一条回复没有包含规定的 ```json 任务代码块"


def is_internal_message(message: Message) -> bool:
    metadata = message.metadata_json or {}
    if metadata.get("message_visibility") == "internal":
        return True
    return (
        message.role == "user"
        and metadata.get("interaction_mode") == "team_tl"
        and _LEGACY_REPAIR_MARKER in (message.content or "")
    )


def visible_message_rows(messages: Iterable[Message]) -> list[Message]:
    rows = list(messages)
    internal_turn_ids = internal_message_turn_ids(rows)
    visible: list[Message] = []
    for row in rows:
        if is_internal_message(row):
            continue
        metadata = row.metadata_json or {}
        linked_turn_id = str(
            metadata.get("user_message_id") or metadata.get("turn_id") or ""
        ).strip()
        if row.role == "assistant" and linked_turn_id in internal_turn_ids:
            continue
        visible.append(row)
    return visible


def internal_message_turn_ids(messages: Iterable[Message]) -> set[str]:
    return {
        row.id
        for row in messages
        if row.role == "user" and is_internal_message(row)
    }


def visible_message_content(message: Message) -> str:
    """Recover the original user text from team prompts stored by older builds."""

    content = message.content or ""
    metadata = message.metadata_json or {}
    if (
        message.role != "user"
        or metadata.get("interaction_mode") != "team_tl"
        or _LEGACY_TEAM_REQUEST_MARKER not in content
    ):
        return content
    visible = content.split(_LEGACY_TEAM_REQUEST_MARKER, 1)[1]
    if _LEGACY_ASSIGNMENT_MARKER in visible:
        visible = visible.split(_LEGACY_ASSIGNMENT_MARKER, 1)[0]
    return visible.strip()


__all__ = [
    "internal_message_turn_ids",
    "is_internal_message",
    "visible_message_content",
    "visible_message_rows",
]
