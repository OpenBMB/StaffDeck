"""Public PD-owned approval projection; never writes native SD handoffs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session

from app.db import get_session
from app.db.models import User
from app.security.auth import get_current_user
from app.public_api.errors import PublicAPIError

router = APIRouter(prefix="/pilotdeck/approvals", tags=["pilotdeck-approvals"])


class ApprovalReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_key: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    wait_id: str = Field(min_length=1)
    expected_revision: int = Field(ge=0)
    message: str = Field(min_length=1)


@dataclass(frozen=True)
class PilotDeckApprovalClient:
    """Private service composition binding to the authenticated PD bridge.

    bridge_token authenticates the SD service; the original user's normal
    bearer remains separate and must be revalidated by PD. The PD owner also
    checks the authoritative tenant/target/session mapping. No shadow ACL,
    session key inference, or approval state is kept by this client.
    """

    origin: str
    bridge_token: str
    tenant_id: str
    agent_id: str
    transport: httpx.BaseTransport | None = None
    project_bindings: bool = False

    def call(self, operation: str, body: dict[str, Any], bearer: str) -> dict[str, Any]:
        if operation not in {"status", "resume"}:
            raise HTTPException(400, "Unknown approval operation")
        with httpx.Client(base_url=self.origin, transport=self.transport,
                          timeout=30, follow_redirects=False) as client:
            response = client.post(
                f"/api/module-host/approvals/{operation}",
                headers={"Authorization": f"Bearer {self.bridge_token}",
                         "X-StaffDeck-Approver-Authorization": bearer},
                json={**body, "tenantId": self.tenant_id,
                      **({} if self.project_bindings else {"agentId": self.agent_id})},
            )
        if not response.is_success:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            if isinstance(detail, dict) and isinstance(detail.get("code"), str) and detail["code"]:
                raise PublicAPIError(response.status_code, detail["code"], detail.get("message") or detail["code"])
            raise HTTPException(response.status_code, detail=detail)
        value = response.json()
        if not isinstance(value, dict):
            raise HTTPException(502, "Invalid PD approval response")
        return value


def _binding(request: Request, user: User, db: Session) -> tuple[PilotDeckApprovalClient, str]:
    binding = getattr(request.app.state, "pilotdeck_approval_client", None)
    if not isinstance(binding, PilotDeckApprovalClient):
        raise HTTPException(503, detail={"code": "PD_APPROVAL_BRIDGE_UNBOUND"})
    if user.tenant_id != binding.tenant_id or user.source != "web" or user.role not in {"admin", "member"}:
        raise HTTPException(403, "Native approval subject does not match the configured tenant")
    # Native get_current_user authenticates a persisted User, whose schema has
    # no disabled field. External login projections are not account authority:
    # resolve their current member facts through the configured directory.
    from staffdeck_harness.runtime.control_auth import provider
    if provider() is not None:
        from staffdeck_harness.runtime.identity_directory import resolve_members
        member = resolve_members(db, user.tenant_id, [user.id]).get(user.id)
        if member is None:
            raise HTTPException(403, "Approver is absent from the authoritative member directory")
        if "disabled" not in member.model_fields_set:
            raise HTTPException(503, detail={"code": "MEMBER_DIRECTORY_INVALID",
                                            "message": "Approver disabled fact is missing"})
        if member.disabled or (member.source, member.role) != (user.source, user.role):
            raise HTTPException(403, "Approver is disabled or differs from the authenticated subject")
    bearer = request.headers.get("authorization", "")
    if not bearer.lower().startswith("bearer "):
        raise HTTPException(401, "Approver login is required")
    return binding, bearer


@router.get("/{session_key}")
def approval_status(session_key: str, request: Request,
                    user: User = Depends(get_current_user),
                    db: Session = Depends(get_session)) -> dict[str, Any]:
    binding, bearer = _binding(request, user, db)
    # PD verifies session access and pinned assignee before returning projection.
    return binding.call("status", {"sessionKey": session_key}, bearer)


@router.post("/reply")
def approval_reply(body: ApprovalReply, request: Request,
                   user: User = Depends(get_current_user),
                   db: Session = Depends(get_session)) -> dict[str, Any]:
    binding, bearer = _binding(request, user, db)
    # No pending-status preflight: authenticated replay is decided under the
    # original PD session lock using its persisted resumeRequests receipt.
    return binding.call("resume", {
        "sessionKey": body.session_key, "requestId": body.request_id,
        "waitId": body.wait_id, "expectedRevision": body.expected_revision,
        "message": body.message, "source": "human",
    }, bearer)
