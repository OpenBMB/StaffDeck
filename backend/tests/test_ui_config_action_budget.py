from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api.ui_config import UIConfigUpdateRequest
from app.core.agent_loop import AgentLoop


def test_agent_loop_action_budget_defaults_to_32_and_accepts_100() -> None:
    assert UIConfigUpdateRequest(tenant_id="tenant_test").agent_loop_max_actions == 32
    assert (
        UIConfigUpdateRequest(
            tenant_id="tenant_test",
            agent_loop_max_actions=100,
        ).agent_loop_max_actions
        == 100
    )


def test_agent_loop_action_budget_rejects_values_above_100() -> None:
    with pytest.raises(ValidationError):
        UIConfigUpdateRequest(
            tenant_id="tenant_test",
            agent_loop_max_actions=101,
        )


def test_agent_loop_action_budget_clamps_stored_values_to_100() -> None:
    owner = SimpleNamespace(
        db=SimpleNamespace(
            get=lambda _model, _tenant_id: SimpleNamespace(
                agent_loop_max_actions=999,
            )
        )
    )

    assert AgentLoop._get_agent_loop_max_actions(owner, "tenant_test") == 100


def test_agent_loop_action_budget_prefers_employee_configuration() -> None:
    agent = SimpleNamespace(
        tenant_id="tenant_test",
        status="active",
        harness_max_actions=17,
    )

    def get(model, key):
        if model.__name__ == "AgentProfile" and key == "agent_1":
            return agent
        return SimpleNamespace(agent_loop_max_actions=64)

    owner = SimpleNamespace(db=SimpleNamespace(get=get))

    assert AgentLoop._get_agent_loop_max_actions(owner, "tenant_test", "agent_1") == 17
