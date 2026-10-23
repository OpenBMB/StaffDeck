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

# An account master key represents the signed-in user in the public API. It is
# not bound to one employee, and every request re-evaluates both view and manage
# permissions. It covers the employee lifecycle while deliberately excluding
# tenant credentials, provider secrets, webhooks, audit logs, and tenant usage.
USER_FULL_ACCESS_SCOPES = frozenset(
    {
        *AGENT_FULL_ACCESS_SCOPES,
        "agents:write",
        "gallery:read",
        "gallery:use",
        "sops:write",
        "sops:publish",
        "knowledge:write",
        "knowledge:publish",
        "skills:write",
        "skills:test",
        "tools:write",
        "tools:test",
        "scheduled_tasks:write",
        "scheduled_tasks:run",
        # 决策助手（Laya 决策头）是无状态的推理服务，不属于任何单个员工，
        # 因此只挂在账号级全量密钥上；员工级密钥（AGENT_RUNTIME_SCOPES）不授予。
        "decisions:run",
        # OCR 助手同理：识别在 OCR 服务内完成，与租户内某个员工无关。
        "ocr:run",
    }
)

AGENT_KEY_ALLOWED_SCOPES = AGENT_RUNTIME_SCOPES


def scopes_for_agent_access(access: AgentCredentialAccess) -> list[str]:
    scopes = AGENT_FULL_ACCESS_SCOPES if access == "full_access" else AGENT_RUNTIME_SCOPES
    return sorted(scopes)


def agent_access_for_scopes(scopes: list[str]) -> AgentCredentialAccess:
    return "full_access" if AGENT_FULL_ACCESS_SCOPES.issubset(scopes) else "runtime"
