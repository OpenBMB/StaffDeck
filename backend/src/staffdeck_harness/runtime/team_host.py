"""Team composition adapter: plugins get DTOs, durable publishing stays a host service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from staffdeck_harness.contracts.interaction import InteractionCall, InteractionContext


@dataclass
class TeamHost:
    provider: Any

    def planner_context(self, db, team):
        invoke = getattr(self.provider, "invoke", None)
        if not callable(invoke):
            return self.provider.planner_context(db, team)
        from app.teams.wakeup import build_team_planner_context
        from app.session.session_schema import TeamPlannerContext

        result = invoke(
            InteractionContext({}, lambda: build_team_planner_context(db, team)),
            InteractionCall("team.context/v1", team.tenant_id, {"team_id": team.id}),
        )
        return TeamPlannerContext.model_validate(result)

    def publish(self, db, **kwargs):
        invoke = getattr(self.provider, "invoke", None)
        if not callable(invoke):
            return self.provider.publish(db, **kwargs)
        from app.teams.wakeup import publish_team_planner_frames

        payload = {
            "team_id": kwargs["team"].id,
            "session_id": kwargs["session"].id,
            "source_turn_id": kwargs["source_turn_id"],
            "frames": [f.model_dump(mode="json") for f in kwargs["frames"]],
        }
        return invoke(
            InteractionContext({}, lambda: publish_team_planner_frames(db, **kwargs)),
            InteractionCall("team.delegate/v1", kwargs["team"].tenant_id, payload),
        )
