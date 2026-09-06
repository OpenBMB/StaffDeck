from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WorkbenchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class WorkItemCreate(WorkbenchRequest):
    document_id: str = Field(min_length=1, max_length=100)
    process_number: int = Field(ge=1, le=36)
    assigned_to_user_id: str = Field(min_length=1, max_length=100)
    reviewer_user_id: str = Field(min_length=1, max_length=100)
    reference_document_ids: list[str] = Field(default_factory=list, max_length=100)


class WorkItemTransition(WorkbenchRequest):
    action: Literal["submit", "request_changes", "approve", "reopen"]
    expected_revision: int = Field(ge=0)
    request_key: str = Field(min_length=1, max_length=120)
    comment: str | None = Field(default=None, max_length=2000)


class WorkIssueCreate(WorkbenchRequest):
    work_item_id: str = Field(min_length=1, max_length=100)
    kind: Literal["document_check", "nonconformity", "review"]
    title: str = Field(min_length=1, max_length=300)
    detail: str = Field(default="", max_length=6000)
    blocking: bool = True
    assigned_to_user_id: str | None = Field(default=None, min_length=1, max_length=100)


class WorkIssueTransition(WorkbenchRequest):
    action: Literal["respond", "close", "reopen"]
    response: str | None = Field(default=None, max_length=6000)
    request_key: str = Field(min_length=1, max_length=120)


class WorkMemberRoleUpdate(WorkbenchRequest):
    role: Literal["project_admin", "reviewer", "editor", "viewer"]
