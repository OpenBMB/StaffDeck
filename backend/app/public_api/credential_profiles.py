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

# Kept for existing employee-scoped credentials created before account-level
# keys were introduced. New employee keys use AGENT_RUNTIME_SCOPES only.
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

# An account master key is not bound to an agent. Its actor is the target user,
# so every request re-evaluates the digital employees visible to that account.
# It deliberately has no configuration-write, publish, credential-management,
# provider-secret, audit-log, or tenant-usage scopes.
USER_FULL_ACCESS_SCOPES = AGENT_FULL_ACCESS_SCOPES

AGENT_KEY_ALLOWED_SCOPES = AGENT_RUNTIME_SCOPES


def scopes_for_agent_access(access: AgentCredentialAccess) -> list[str]:
    scopes = AGENT_FULL_ACCESS_SCOPES if access == "full_access" else AGENT_RUNTIME_SCOPES
    return sorted(scopes)


def agent_access_for_scopes(scopes: list[str]) -> AgentCredentialAccess:
    return "full_access" if AGENT_FULL_ACCESS_SCOPES.issubset(scopes) else "runtime"
