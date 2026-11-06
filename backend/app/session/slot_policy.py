from __future__ import annotations

from collections.abc import Mapping
from typing import Any


ROUTER_GENERATED_MESSAGE_SLOT_KEYS = {
    "message_content",
    "user_message",
    "rewritten_message",
    "normalized_message",
    "current_message",
    "source_message",
}


def slot_is_filled(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return value not in (None, "", [], {})


def missing_step_slots(requirement, updates, known=None) -> list[str]:
    fields = getattr(requirement, "expected_slots", None) or requirement.required_slots
    values = {**dict(requirement.known_slots if known is None else known), **dict(updates or {})}
    return [field for field in fields if not slot_is_filled(values.get(field))]


def strip_router_generated_message_slots(slots: Mapping[str, Any] | None) -> dict[str, Any]:
    """Router must not persist rewritten user text as skill slot values."""
    if not isinstance(slots, Mapping):
        return {}
    return {
        str(key): value
        for key, value in slots.items()
        if str(key).strip() not in ROUTER_GENERATED_MESSAGE_SLOT_KEYS
    }
