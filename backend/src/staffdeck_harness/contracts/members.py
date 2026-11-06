"""Tenant-scoped account facts, separate from execution identity and local projections."""
from typing import Protocol
from pydantic import BaseModel, ConfigDict


class MemberRecord(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)
    id: str
    tenant_id: str
    username: str
    display_name: str = ''
    source: str
    role: str = 'member'
    disabled: bool = False


class MemberDirectoryPort(Protocol):
    member_identity_source: str
    def resolve_members(self, tenant_id: str, user_ids: list[str]) -> list[MemberRecord]: ...
