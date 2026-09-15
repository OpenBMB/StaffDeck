"""One employee directory meaning for every source, independent of transport or storage."""
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

DirectoryScope = Literal['visible', 'available', 'mine', 'gallery', 'shared', 'managed']
DIRECTORY_ALIASES = {f'/api/enterprise/agents/{path}': scope for path, scope in (
    ('gallery', 'gallery'), ('shared-with-me', 'shared'), ('managed', 'managed'), ('available', 'available'), ('mine', 'mine'))}


def without_directory_projection(metadata):
    return {key: value for key, value in metadata.items() if key not in {
        'directory_access', 'directory_statistics', 'used_by_current_user', 'chat_used_by_current_user',
        'resource_counts', 'directory_summary', 'avatar_resource_url'}}


class DirectoryEntry(BaseModel):
    model_config = ConfigDict(strict=True)
    profile: dict[str, Any]
    owner_user_id: str | None = None
    public: bool = False
    shared: bool = False
    added: bool = False
    allowed_actions: list[str] = Field(default_factory=list)


class DirectorySnapshot(BaseModel):
    model_config = ConfigDict(strict=True)
    schema_version: Literal[1] = 1
    projection: Literal['full', 'summary'] = 'full'
    tenant_id: str
    subject_user_id: str
    entries: list[DirectoryEntry]


def select_directory(snapshot: DirectorySnapshot, *, tenant_id: str, user_id: str,
                     scope: DirectoryScope) -> list[DirectoryEntry]:
    from .errors import ModuleSdkError
    if (snapshot.tenant_id, snapshot.subject_user_id) != (tenant_id, user_id):
        raise ModuleSdkError('员工目录身份不匹配', code='DIRECTORY_CONTRACT_INVALID')
    seen = {}
    for entry in snapshot.entries:
        row = entry.profile
        identifier = row.get('id')
        if not isinstance(identifier, str) or not identifier or row.get('tenant_id') != tenant_id:
            raise ModuleSdkError('员工目录包含错误的资源归属', code='DIRECTORY_CONTRACT_INVALID')
        if not isinstance(row.get('is_overall'), bool) or not isinstance(row.get('status'), str):
            raise ModuleSdkError('员工目录状态字段类型不正确', code='DIRECTORY_CONTRACT_INVALID')
        if identifier in seen and seen[identifier] != entry:
            raise ModuleSdkError('员工目录包含相互冲突的重复记录', code='DIRECTORY_CONTRACT_INVALID')
        seen[identifier] = entry
    result = []
    for entry in seen.values():
        row = entry.profile
        if 'view' not in entry.allowed_actions or row.get('status') == 'deleted' or (row.get('metadata') or {}).get('hidden_from_staffdeck'):
            continue
        if scope != 'visible' and row.get('is_overall'):
            continue
        matches = {
            'visible': True,
            'available': row.get('status') == 'active' and 'use' in entry.allowed_actions,
            'mine': entry.owner_user_id == user_id,
            'gallery': entry.public,
            'shared': entry.shared and entry.owner_user_id != user_id,
            'managed': 'manage' in entry.allowed_actions,
        }
        if matches[scope]:
            result.append(entry)
    return result
