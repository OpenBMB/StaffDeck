from __future__ import annotations

from typing import Literal


AgentCredentialAccess = Literal["runtime", "full_access"]

AGENT_RUNTIME_SCOPES = frozenset(
    {
        "agents:read",
        "capabilities:read",
        "runs:create",
        "runs:read",
        "runs:cancel",
        "sessions:read",
        "sessions:write",
        "artifacts:read",
        "traces:read",
    }
)

# This is an employee-scoped master key, not a tenant administrator key. It can
# inspect every resource that is visible to the bound employee and execute that
# employee, but it deliberately has no configuration write or publish scopes.
AGENT_FULL_ACCESS_SCOPES = frozenset(
    {
        *AGENT_RUNTIME_SCOPES,
        "sops:read",
        "knowledge:read",
        "skills:read",
        "tools:read",
        "scheduled_tasks:read",
        "operations:read",
    }
)

AGENT_KEY_ALLOWED_SCOPES = AGENT_FULL_ACCESS_SCOPES


def scopes_for_agent_access(access: AgentCredentialAccess) -> list[str]:
    scopes = AGENT_FULL_ACCESS_SCOPES if access == "full_access" else AGENT_RUNTIME_SCOPES
    return sorted(scopes)


def agent_access_for_scopes(scopes: list[str]) -> AgentCredentialAccess:
    return "full_access" if AGENT_FULL_ACCESS_SCOPES.issubset(scopes) else "runtime"
