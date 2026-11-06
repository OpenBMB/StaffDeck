"""Compatibility exports; implementation is owned by staffdeck_harness.sop."""
from staffdeck_harness.sop.state import (
    SopSessionState as SkillRuntime,  # noqa: F401
    _sanitize_decision_slots,  # noqa: F401
    _sanitize_task_frames,  # noqa: F401
    _task_frame_from_pending,  # noqa: F401
    _current_frame,  # noqa: F401
    _active_task_id,  # noqa: F401
    _set_active_task_id,  # noqa: F401
    _activate_frame,  # noqa: F401
    _pop_last_skill_frame,  # noqa: F401
    _pop_task_frame,  # noqa: F401
    _without_task_or_skill,  # noqa: F401
    _find_equivalent_task_frame_index,  # noqa: F401
    _without_equivalent_task_frames,  # noqa: F401
    _task_frames_equivalent,  # noqa: F401
    _merge_task_frames,  # noqa: F401
    _frame_skill_id,  # noqa: F401
    _task_identity_slots,  # noqa: F401
    _patch_task_frame,  # noqa: F401
    _upsert_frame,  # noqa: F401
    _frame_with_slot_hints,  # noqa: F401
    TASK_IDENTITY_FIELDS,  # noqa: F401
)
