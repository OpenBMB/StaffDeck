from __future__ import annotations

from dataclasses import dataclass

from app.db.models import Skill, Tool


@dataclass(frozen=True)
class SopToolAuthorization:
    """Server-only grants derived from the validated, expanded current SOP node.

    Never populate this context from tool arguments or request metadata. The
    Harness supplies its execution Skill after nesting validation/expansion.
    """

    tenant_id: str
    parent_skill_id: str | None
    step_id: str | None
    skill_ids: frozenset[str]
    tool_refs: frozenset[str]

    def permits_skill(self, tool: Tool) -> bool:
        return not tool.allowed_skills_json or bool(
            self.skill_ids.intersection(
                str(value).strip() for value in tool.allowed_skills_json if str(value).strip()
            )
        )

    def explicitly_allows(self, tool: Tool) -> bool:
        return bool(self.tool_refs.intersection({tool.id, tool.name}))


def current_sop_tool_authorization(
    tenant_id: str,
    skill: Skill | None,
    step_id: str | None,
) -> SopToolAuthorization:
    from app.core.task_request_compiler import (
        current_step_authorization_skill_ids,
        current_step_capability_refs,
    )

    if skill is not None and skill.tenant_id != tenant_id:
        raise ValueError("SOP authorization tenant mismatch")
    return SopToolAuthorization(
        tenant_id=tenant_id,
        parent_skill_id=skill.skill_id if skill is not None else None,
        step_id=step_id,
        skill_ids=frozenset(current_step_authorization_skill_ids(skill, step_id)),
        tool_refs=frozenset(current_step_capability_refs(skill, step_id)["tool_ids"]),
    )
