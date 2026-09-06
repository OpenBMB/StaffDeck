"""Additive persistence for version-pinned workbench checks."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.db.models import new_id, utc_now


class AuditDocumentCheck(SQLModel, table=True):
    __tablename__ = "audit_document_checks"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "audit_case_id", "request_key", name="uq_document_check_request"
        ),
    )

    id: str = Field(default_factory=lambda: new_id("doccheck"), primary_key=True)
    tenant_id: str = Field(index=True)
    audit_case_id: str = Field(index=True)
    document_id: str = Field(index=True)
    document_version_id: str = Field(index=True)
    created_by_user_id: str
    request_key: str
    request_hash: str
    status: str = Field(default="queued", index=True)
    generation: int = 1
    reference_versions_json: list[dict[str, str]] = Field(
        default_factory=list, sa_column=Column(JSON)
    )
    rule_snapshot_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    field_labels_json: dict[str, str] = Field(default_factory=dict, sa_column=Column(JSON))
    findings_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    error_code: str | None = None
    retry_keys_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
