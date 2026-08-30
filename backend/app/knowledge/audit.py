"""共享知识库审计层：只追加事件，并为 Agent 变更保存可重放的幂等收据。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import event, func
from sqlmodel import Session, select

from app.db.models import (
    AgentProfile,
    KnowledgeBaseAuditEvent,
    KnowledgeBaseVersion,
    Team,
    User,
)
from app.knowledge.errors import (
    KNOWLEDGE_IDEMPOTENCY_CONFLICT,
    KNOWLEDGE_IDEMPOTENCY_REQUIRED,
    knowledge_error,
)

_RECEIPT_KEY = "idempotency_receipt"


class KnowledgeAuditImmutabilityError(RuntimeError):
    """应用 ORM 尝试修改或删除已持久化审计事件时抛出。"""


@event.listens_for(KnowledgeBaseAuditEvent, "before_update")
def _reject_audit_event_update(
    _mapper: Any,
    _connection: Any,
    _target: KnowledgeBaseAuditEvent,
) -> None:
    """拒绝审计事件 UPDATE；事件更正必须追加新事件而不能覆盖历史。"""
    raise KnowledgeAuditImmutabilityError("knowledge audit events are append-only")


@event.listens_for(KnowledgeBaseAuditEvent, "before_delete")
def _reject_audit_event_delete(
    _mapper: Any,
    _connection: Any,
    _target: KnowledgeBaseAuditEvent,
) -> None:
    """拒绝审计事件 DELETE，确保回滚和后续操作不会抹除历史。"""
    raise KnowledgeAuditImmutabilityError("knowledge audit events cannot be deleted")


@dataclass(frozen=True)
class KnowledgeMutationReceipt:
    """一次已持久化 Agent 变更的原始事件与可重放结果。"""

    event_id: str
    result: dict[str, Any]


@dataclass(frozen=True)
class KnowledgeAuditEventView:
    """供管理界面读取的审计事件投影，补齐团队、操作者与版本名称。"""

    id: str
    knowledge_base_id: str
    team_id: str | None
    team_name: str | None
    knowledge_base_version_id: str | None
    knowledge_base_version: str | None
    actor_type: str
    actor_id: str
    actor_name: str
    action: str
    reason: str | None
    details: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class KnowledgeAuditEventPage:
    """确定性 offset 分页结果；items 按事件时间和 ID 倒序排列。"""

    items: tuple[KnowledgeAuditEventView, ...]
    total: int
    offset: int
    limit: int
    has_more: bool


@dataclass(frozen=True)
class _KnowledgeAuditLabels:
    """一页审计事件所需的显示名称映射，避免逐行查询。"""

    team_names: dict[str, str]
    version_names: dict[str, str]
    user_names: dict[str, str]
    agent_names: dict[str, str]


def _request_fingerprint(payload: Any) -> str:
    """将请求规范化后计算稳定摘要，避免字典字段顺序影响幂等判断。"""
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _required_idempotency_key(value: str | None) -> str:
    """校验 Agent 变更键并返回去除空白后的值；缺失时快速失败。"""
    key = str(value or "").strip()
    if not key:
        raise knowledge_error(KNOWLEDGE_IDEMPOTENCY_REQUIRED)
    if len(key) > 200:
        raise knowledge_error(
            KNOWLEDGE_IDEMPOTENCY_CONFLICT,
            message="幂等键长度不能超过 200 个字符。",
        )
    return key


class KnowledgeAuditService:
    """在调用方事务内追加审计事件，并查询 Agent 幂等收据。"""

    def __init__(self, db: Session) -> None:
        """绑定现有数据库会话；服务本身不会提交或回滚事务。"""
        self.db = db

    def replay_agent_mutation(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        actor_id: str,
        action: str,
        idempotency_key: str | None,
        request_payload: Any,
    ) -> KnowledgeMutationReceipt | None:
        """相同输入返回首个持久化结果，不同输入复用同一键时报告冲突。"""
        key = _required_idempotency_key(idempotency_key)
        event = self.db.exec(
            select(KnowledgeBaseAuditEvent).where(
                KnowledgeBaseAuditEvent.tenant_id == tenant_id,
                KnowledgeBaseAuditEvent.actor_id == actor_id,
                KnowledgeBaseAuditEvent.action == action,
                KnowledgeBaseAuditEvent.idempotency_key == key,
            )
        ).first()
        if event is None:
            return None

        if event.knowledge_base_id != knowledge_base_id:
            raise knowledge_error(
                KNOWLEDGE_IDEMPOTENCY_CONFLICT,
                details={"event_id": event.id},
            )

        receipt = dict((event.details_json or {}).get(_RECEIPT_KEY) or {})
        if receipt.get("request_hash") != _request_fingerprint(request_payload):
            raise knowledge_error(
                KNOWLEDGE_IDEMPOTENCY_CONFLICT,
                details={"event_id": event.id},
            )
        return KnowledgeMutationReceipt(
            event_id=event.id,
            result=dict(receipt.get("result") or {}),
        )

    def query_events(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        offset: int,
        limit: int,
        team_id: str | None = None,
        action: str | None = None,
        actor_type: str | None = None,
        actor_id: str | None = None,
        knowledge_base_version_id: str | None = None,
    ) -> KnowledgeAuditEventPage:
        """在租户和知识库硬边界内组合筛选，并返回带来源名称的倒序分页。"""
        resolved_offset = max(0, offset)
        resolved_limit = max(1, min(limit, 200))
        conditions = self._query_conditions(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            team_id=team_id,
            action=action,
            actor_type=actor_type,
            actor_id=actor_id,
            knowledge_base_version_id=knowledge_base_version_id,
        )

        # 先统计同一组条件下的总数，再读取稳定的一页事件。
        total = int(
            self.db.exec(
                select(func.count(KnowledgeBaseAuditEvent.id)).where(*conditions)
            ).one()
        )
        rows = list(
            self.db.exec(
                select(KnowledgeBaseAuditEvent)
                .where(*conditions)
                .order_by(
                    KnowledgeBaseAuditEvent.created_at.desc(),
                    KnowledgeBaseAuditEvent.id.desc(),
                )
                .offset(resolved_offset)
                .limit(resolved_limit)
            ).all()
        )

        # 再批量解析团队、操作者和版本显示名，避免界面只能解释裸 ID。
        labels = self._load_event_labels(tenant_id, rows)
        items = tuple(self._project_event(row, labels) for row in rows)
        return KnowledgeAuditEventPage(
            items=items,
            total=total,
            offset=resolved_offset,
            limit=resolved_limit,
            has_more=resolved_offset + len(items) < total,
        )

    def _query_conditions(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        team_id: str | None,
        action: str | None,
        actor_type: str | None,
        actor_id: str | None,
        knowledge_base_version_id: str | None,
    ) -> list[Any]:
        """构造审计筛选条件；租户和知识库条件始终存在且不能被可选筛选覆盖。"""
        conditions: list[Any] = [
            KnowledgeBaseAuditEvent.tenant_id == tenant_id,
            KnowledgeBaseAuditEvent.knowledge_base_id == knowledge_base_id,
        ]
        if team_id:
            conditions.append(KnowledgeBaseAuditEvent.team_id == team_id)
        if action:
            conditions.append(KnowledgeBaseAuditEvent.action == action)
        if actor_type:
            conditions.append(KnowledgeBaseAuditEvent.actor_type == actor_type)
        if actor_id:
            conditions.append(KnowledgeBaseAuditEvent.actor_id == actor_id)
        if knowledge_base_version_id:
            conditions.append(
                KnowledgeBaseAuditEvent.knowledge_base_version_id
                == knowledge_base_version_id
            )
        return conditions

    def _load_event_labels(
        self,
        tenant_id: str,
        rows: list[KnowledgeBaseAuditEvent],
    ) -> _KnowledgeAuditLabels:
        """批量加载一页事件引用的同租户团队、版本、用户和员工名称。"""
        team_ids = {row.team_id for row in rows if row.team_id}
        version_ids = {
            row.knowledge_base_version_id
            for row in rows
            if row.knowledge_base_version_id
        }
        user_ids = {row.actor_id for row in rows if row.actor_type == "user"}
        agent_ids = {row.actor_id for row in rows if row.actor_type == "agent"}

        teams = (
            self.db.exec(
                select(Team).where(Team.tenant_id == tenant_id, Team.id.in_(team_ids))
            ).all()
            if team_ids
            else []
        )
        versions = (
            self.db.exec(
                select(KnowledgeBaseVersion).where(
                    KnowledgeBaseVersion.tenant_id == tenant_id,
                    KnowledgeBaseVersion.id.in_(version_ids),
                )
            ).all()
            if version_ids
            else []
        )
        users = (
            self.db.exec(
                select(User).where(User.tenant_id == tenant_id, User.id.in_(user_ids))
            ).all()
            if user_ids
            else []
        )
        agents = (
            self.db.exec(
                select(AgentProfile).where(
                    AgentProfile.tenant_id == tenant_id,
                    AgentProfile.id.in_(agent_ids),
                )
            ).all()
            if agent_ids
            else []
        )
        return _KnowledgeAuditLabels(
            team_names={team.id: team.name for team in teams},
            version_names={version.id: version.version for version in versions},
            user_names={user.id: user.display_name or user.username for user in users},
            agent_names={agent.id: agent.name for agent in agents},
        )

    def _project_event(
        self,
        row: KnowledgeBaseAuditEvent,
        labels: _KnowledgeAuditLabels,
    ) -> KnowledgeAuditEventView:
        """把持久化事件投影为安全展示结构，并隐藏内部幂等收据。"""
        if row.actor_type == "user":
            actor_name = labels.user_names.get(row.actor_id, row.actor_id)
        elif row.actor_type == "agent":
            actor_name = labels.agent_names.get(row.actor_id, row.actor_id)
        else:
            actor_name = row.actor_id
        details = {
            key: value
            for key, value in dict(row.details_json or {}).items()
            if key != _RECEIPT_KEY
        }
        return KnowledgeAuditEventView(
            id=row.id,
            knowledge_base_id=row.knowledge_base_id,
            team_id=row.team_id,
            team_name=labels.team_names.get(row.team_id) if row.team_id else None,
            knowledge_base_version_id=row.knowledge_base_version_id,
            knowledge_base_version=(
                labels.version_names.get(row.knowledge_base_version_id)
                if row.knowledge_base_version_id
                else None
            ),
            actor_type=row.actor_type,
            actor_id=row.actor_id,
            actor_name=actor_name,
            action=row.action,
            reason=row.reason,
            details=details,
            created_at=row.created_at,
        )

    def append_event(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        team_id: str | None,
        knowledge_base_version_id: str | None,
        actor_type: str,
        actor_id: str,
        action: str,
        reason: str | None = None,
        details: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        request_payload: Any = None,
        durable_result: dict[str, Any] | None = None,
    ) -> KnowledgeBaseAuditEvent:
        """追加一个不可变事件；Agent 事件额外保存请求摘要和可重放结果。"""
        event_details = dict(details or {})
        normalized_key = str(idempotency_key or "").strip() or None
        if actor_type == "agent":
            normalized_key = _required_idempotency_key(idempotency_key)

        event = KnowledgeBaseAuditEvent(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            team_id=team_id,
            knowledge_base_version_id=knowledge_base_version_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            idempotency_key=normalized_key,
            reason=reason,
            details_json=event_details,
        )
        if actor_type == "agent":
            # 事件 ID 在对象创建时已经稳定生成，因此可直接写入幂等返回结果。
            receipt_result = dict(durable_result or {})
            receipt_result.setdefault("audit_event_id", event.id)
            event.details_json = {
                **event_details,
                _RECEIPT_KEY: {
                    "request_hash": _request_fingerprint(request_payload),
                    "result": receipt_result,
                },
            }
        self.db.add(event)
        return event
