from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from app.db.models import ChatSession, Skill
from app.session.session_schema import RouterDecision, TurnPlan


class SlotHydrationPolicy:
    ALLOWED_MEMORY_KINDS = frozenset({"profile", "preference", "fact"})
    SLOT_MEMORY_KEY_ALIASES: dict[str, tuple[str, ...]] = {
        "user_name": ("preferred_name",),
    }

    @classmethod
    def hydrate_plan(
        cls,
        chat_session: ChatSession,
        plan: TurnPlan,
        skills: list[Skill],
        memory_context: list[dict[str, object]],
        patcher: Callable[
            [Skill | None, dict[str, Any], list[dict[str, object]]], dict[str, Any]
        ]
        | None = None,
    ) -> dict[str, Any]:
        """Hydrate SOP TaskFrames before they are persisted or executed.

        Slot hydration used to exist only as a RouterDecision utility and was
        not called by Harness v2. That allowed a step to ask for information
        already available in structured long-term memory; the next user
        message would then appear to satisfy two different questions. Mutating
        the canonical TurnPlan keeps persistence, execution and the public
        router projection on the same slot snapshot.
        """

        patch_slots = patcher or cls.patch
        skills_by_id = {skill.skill_id: skill for skill in skills}
        hydrated_tasks: list[dict[str, Any]] = []
        for frame in plan.task_frames:
            if frame.kind != "sop":
                continue
            target_skill_id = str(
                frame.target_skill_id or chat_session.active_skill_id or ""
            ).strip()
            target_skill = skills_by_id.get(target_skill_id)
            continues_active = bool(
                frame.decision == "continue_active"
                and target_skill_id == chat_session.active_skill_id
            )
            base_slots = (
                dict(chat_session.slots_json or {}) if continues_active else {}
            )
            base_slots.update(dict(frame.slot_hints or {}))
            patch = patch_slots(target_skill, base_slots, memory_context)
            if not patch:
                continue
            frame.slot_hints = {**dict(frame.slot_hints or {}), **patch}
            hydrated_tasks.append(
                {
                    "task_id": frame.task_id,
                    "target_skill_id": target_skill_id,
                    "slots": patch,
                }
            )
        return {"tasks": hydrated_tasks} if hydrated_tasks else {}

    @classmethod
    def hydrate(
        cls,
        chat_session: ChatSession,
        router_decision: RouterDecision,
        skills: list[Skill],
        memory_context: list[dict[str, object]],
        patcher: Callable[
            [Skill | None, dict[str, Any], list[dict[str, object]]], dict[str, Any]
        ]
        | None = None,
        awaiting_trimmer: Callable[
            [RouterDecision, dict[str, Any]], list[str] | None
        ]
        | None = None,
    ) -> dict[str, Any]:
        patch_slots = patcher or cls.patch
        trim_awaiting = awaiting_trimmer or cls.trim_satisfied_awaiting_fields
        skills_by_id = {skill.skill_id: skill for skill in skills}
        hydrated: dict[str, Any] = {}
        target_skill = skills_by_id.get(
            router_decision.target_skill_id or chat_session.active_skill_id or ""
        )
        base_slots = dict(chat_session.slots_json or {})
        base_slots.update(dict(router_decision.slot_hints or {}))
        patch = patch_slots(target_skill, base_slots, memory_context)
        if patch:
            router_decision.slot_hints = {**dict(router_decision.slot_hints or {}), **patch}
            hydrated["primary"] = patch
        remaining_awaiting = trim_awaiting(router_decision, {**base_slots, **patch})
        if remaining_awaiting is not None:
            hydrated["awaiting_input_expected_fields"] = remaining_awaiting

        task_patches: list[dict[str, Any]] = []
        for task in [
            *router_decision.task_frames,
            *router_decision.pending_tasks,
            *router_decision.created_tasks,
        ]:
            task_skill = skills_by_id.get(task.target_skill_id or "")
            task_slots = dict(task.slot_hints or {})
            task_patch = patch_slots(task_skill, task_slots, memory_context)
            if task_patch:
                task.slot_hints = {**task_slots, **task_patch}
                task_patches.append(
                    {
                        "task_id": task.task_id,
                        "target_skill_id": task.target_skill_id,
                        "slots": task_patch,
                    }
                )
        if task_patches:
            hydrated["tasks"] = task_patches
        return hydrated

    @classmethod
    def patch(
        cls,
        skill: Skill | None,
        slots: dict[str, Any],
        memory_context: list[dict[str, object]],
    ) -> dict[str, Any]:
        expected_fields = cls.skill_expected_fields(skill) if skill else set()
        memory_values = cls.memory_values_by_key(memory_context)
        patch: dict[str, Any] = {
            key: value
            for key, value in memory_values.items()
            if not cls.slot_has_value(slots, key)
        }
        for field in expected_fields:
            if cls.slot_has_value(slots, field) or cls.slot_has_value(patch, field):
                continue
            for memory_key in cls.memory_keys_for_slot(field):
                if memory_key not in memory_values:
                    continue
                patch[field] = memory_values[memory_key]
                break
        return patch

    @classmethod
    def trim_satisfied_awaiting_fields(
        cls, router_decision: RouterDecision, slots: dict[str, Any]
    ) -> list[str] | None:
        if not router_decision.awaiting_input:
            return None
        original = list(router_decision.awaiting_input.expected_fields)
        remaining = [
            field
            for field in router_decision.awaiting_input.expected_fields
            if not cls.slot_has_value(slots, field)
        ]
        if remaining == original:
            return None
        if remaining:
            router_decision.awaiting_input.expected_fields = remaining
        else:
            router_decision.awaiting_input = None
        return remaining

    @staticmethod
    def slot_has_value(slots: dict[str, Any], field: str) -> bool:
        value = slots.get(field)
        return value is not None and value != "" and value != []

    @staticmethod
    def skill_expected_fields(skill: Skill) -> set[str]:
        content = skill.content_json or {}
        fields: set[str] = set()
        required_info = content.get("required_info")
        if isinstance(required_info, list):
            fields.update(str(item) for item in required_info if str(item).strip())
        nodes = content.get("nodes")
        if isinstance(nodes, list):
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                expected = node.get("expected_user_info")
                if isinstance(expected, list):
                    fields.update(str(item) for item in expected if str(item).strip())
        return fields

    @classmethod
    def memory_values_by_key(
        cls, memory_context: list[dict[str, object]]
    ) -> dict[str, str]:
        """Index eligible structured memories without inferring from prose.

        ``MemoryService.context_memories`` returns newest records first, so
        ``setdefault`` preserves the most recent value when legacy duplicate
        keys exist. Memories without an explicit stable key are deliberately
        ignored: free-text similarity is not a safe basis for filling a Slot.
        Every keyed profile, preference and fact is projected into the
        TaskFrame; declared SOP fields only add explicit aliases such as
        ``preferred_name`` to ``user_name``.
        """

        values: dict[str, str] = {}
        for memory in memory_context:
            if str(memory.get("kind") or "") not in cls.ALLOWED_MEMORY_KINDS:
                continue
            metadata = memory.get("metadata")
            raw_key = metadata.get("key") if isinstance(metadata, dict) else None
            key = cls.normalize_key(raw_key)
            content = str(memory.get("content") or "").strip()
            if key and content:
                values.setdefault(key, content)
        return values

    @classmethod
    def memory_keys_for_slot(cls, field: str) -> tuple[str, ...]:
        normalized = cls.normalize_key(field)
        if not normalized:
            return ()
        aliases = cls.SLOT_MEMORY_KEY_ALIASES.get(normalized, ())
        return (normalized, *aliases)

    @staticmethod
    def normalize_key(value: object) -> str:
        if not isinstance(value, str):
            return ""
        return re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip().lower()).strip("_")
