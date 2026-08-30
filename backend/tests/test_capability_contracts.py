from dataclasses import FrozenInstanceError

import pytest

from app.capabilities import CapabilityContext, GeneralSkillFile, GeneralSkillPackage


def test_capability_context_carries_optional_trusted_team_scope() -> None:
    """Provider 上下文可携带 Core 注入的团队标识，私聊上下文仍保持为空。"""
    private = CapabilityContext(
        request_id="request-1",
        tenant_id="tenant-demo",
        agent_id="agent-writer",
        user_id="user-1",
        session_id="session-private",
        turn_id="turn-1",
        channel="web",
    )
    team = CapabilityContext(
        request_id="request-2",
        tenant_id="tenant-demo",
        agent_id="agent-writer",
        user_id="user-1",
        session_id="session-team",
        turn_id="turn-2",
        channel="web",
        team_id="team-a",
    )

    assert private.team_id is None
    assert team.team_id == "team-a"

    with pytest.raises(ValueError):
        CapabilityContext(
            request_id="request-3",
            tenant_id="tenant-demo",
            agent_id="agent-writer",
            user_id="user-1",
            session_id="session-team",
            turn_id="turn-3",
            channel="web",
            team_id="   ",
        )


def test_general_skill_package_is_an_immutable_content_snapshot() -> None:
    package = GeneralSkillPackage(
        package_id="weather",
        slug="weather",
        version="1.4.0",
        digest="sha256:package",
        package_contract_version="1",
        skill_markdown="# Weather",
        files=(
            GeneralSkillFile(
                path="SKILL.md",
                content="# Weather",
                size=9,
                mime_type="text/markdown",
            ),
        ),
        entrypoint="SKILL.md",
    )

    assert package.files[0].content == package.skill_markdown
    with pytest.raises(FrozenInstanceError):
        package.version = "1.5.0"  # type: ignore[misc]
