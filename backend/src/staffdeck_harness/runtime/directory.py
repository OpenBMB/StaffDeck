"""Shared directory projection and Runtime-owned statistics. Sources supply facts only."""
import logging
from copy import deepcopy

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import select

from staffdeck_harness.contracts.directory import DirectorySnapshot, select_directory
from staffdeck_harness.contracts.errors import ModuleSdkError, PermissionDenied
from staffdeck_harness.contracts.manifest import SlotName


def employee_directory(db, tenant_id, user, *, scope='visible', authorization='', summary=False):
    from contextlib import nullcontext
    from staffdeck_harness.modules.registry import peek_registry
    registry = db.info.get('staffdeck_registry') or peek_registry()
    try:
        with registry.turn_lease() if registry else nullcontext():
            return _employee_directory(db, tenant_id, user, scope=scope, authorization=authorization, summary=summary)
    except ModuleSdkError as exc:
        raise HTTPException(503, exc.to_dict()) from exc


def _employee_directory(db, tenant_id, user, *, scope='visible', authorization='', summary=False):
    from app.db.models import ChatSession
    from app.agents.schema import AgentProfileRead
    from staffdeck_harness.runtime.staff_directory import directory_context
    from staffdeck_harness.modules.registry import peek_registry
    from staffdeck_harness.composition.sources import resolve_source
    from staffdeck_harness.composition.local_sources import LocalStaffSource
    if user.tenant_id != tenant_id:
        raise HTTPException(403, 'Tenant mismatch')
    actor_id = user.id
    registry = db.info.get('staffdeck_registry') or peek_registry()
    try:
        source = resolve_source(registry, SlotName.STAFF_SOURCE, db) if registry else LocalStaffSource(db)
        reader = (getattr(source, 'directory_summary', None) if summary else None) or getattr(source, 'directory', None)
        if not callable(reader):
            raise ModuleSdkError('当前员工来源不支持统一目录契约', code='DIRECTORY_CONTRACT_UNAVAILABLE')
        snapshot = DirectorySnapshot.model_validate(reader(directory_context(db, tenant_id, None, user=user), authorization=authorization))
        if snapshot.projection == 'summary':
            if not summary:
                raise ValueError('Full directory requested but source returned only a summary')
            for entry in snapshot.entries:
                counts = (entry.profile.get('metadata') or {}).get('resource_counts')
                if not isinstance(counts, dict) or any(type(v) is not int or v < 0 for v in counts.values()):
                    raise ValueError('Summary directory omitted valid resource counts')
        entries = select_directory(snapshot, tenant_id=tenant_id, user_id=actor_id, scope=scope)
    except ModuleSdkError as exc:
        raise HTTPException(403 if isinstance(exc, PermissionDenied) else 503, exc.to_dict()) from exc
    except ValueError:
        raise HTTPException(503, {'code': 'DIRECTORY_CONTRACT_INVALID', 'message': '员工目录返回格式不符合统一契约'}) from None
    ids = [entry.profile['id'] for entry in entries]
    totals, used, stats_ok = {}, set(), True
    try:
        if ids:
            totals = dict(db.exec(select(ChatSession.agent_id, func.count(ChatSession.id)).where(
                ChatSession.tenant_id == tenant_id, ChatSession.agent_id.in_(ids)).group_by(ChatSession.agent_id)).all())
            used = set(db.exec(select(ChatSession.agent_id).where(ChatSession.tenant_id == tenant_id,
                ChatSession.user_id == actor_id, ChatSession.agent_id.in_(ids))).all())
    except SQLAlchemyError:
        db.rollback()
        stats_ok = False
        logging.getLogger(__name__).exception('Employee directory Runtime statistics unavailable')
    result = []
    for entry in entries:
        row = deepcopy(entry.profile)
        metadata = dict(row.get('metadata') or {})
        metadata.update(published_to_gallery=entry.public,
            directory_access={'owned': entry.owner_user_id == actor_id, 'public': entry.public, 'shared': entry.shared,
                              'allowed_actions': list(entry.allowed_actions),
                              'can_view': 'view' in entry.allowed_actions, 'can_use': 'use' in entry.allowed_actions,
                              'can_manage': 'manage' in entry.allowed_actions},
            directory_statistics={'status': 'available' if stats_ok else 'unavailable',
                                  'chat_count': totals.get(row['id'], 0) if stats_ok else None},
            used_by_current_user=True if entry.added else row['id'] in used if stats_ok else None)
        row['metadata'] = metadata
        if summary:
            from collections import Counter
            from staffdeck_harness.runtime.avatar_assets import remember, realm
            if snapshot.projection != 'summary':
                metadata['resource_counts'] = dict(Counter(r['resource_type'] for r in row.get('resources', [])
                    if r.get('status') not in {'deleted','inactive'}))
            metadata['directory_summary'] = True
            avatar = remember(tenant_id, row['id'], metadata.get('avatar_image'), namespace=realm(db))
            if isinstance(metadata.get('avatar_image'), str) and metadata['avatar_image'].startswith('data:'):
                metadata.pop('avatar_image', None)
            if avatar:
                metadata['avatar_resource_url'] = avatar
            row['resources'] = []
            row['persona_prompt'] = None
        try:
            result.append(AgentProfileRead.model_validate(row))
        except ValueError:
            raise HTTPException(503, {'code': 'DIRECTORY_CONTRACT_INVALID', 'message': '员工目录资料不符合接口契约'}) from None
    return result
