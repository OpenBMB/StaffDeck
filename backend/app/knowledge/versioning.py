"""共享知识库版本生命周期：草稿快照、原子发布、驳回与回滚。"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select, update

from app.agents.branching import clone_knowledge_version_assets
from app.db.models import (
    KnowledgeBase,
    KnowledgeBaseVersion,
    KnowledgeDocument,
    KnowledgeIngestJob,
    utc_now,
)
from app.knowledge.audit import KnowledgeAuditService
from app.knowledge.errors import (
    KNOWLEDGE_CONTEXT_MISMATCH,
    KNOWLEDGE_MODE_INVALID,
    KNOWLEDGE_PUBLISH_CONFLICT,
    KNOWLEDGE_VERSION_NOT_READY,
    KnowledgeError,
    knowledge_error,
)


def _required_reason(value: str | None) -> str:
    """返回非空变更原因，避免无说明的共享知识生命周期变更。"""
    reason = str(value or "").strip()
    if not reason:
        raise knowledge_error(
            KNOWLEDGE_MODE_INVALID,
            message="共享知识库版本操作必须填写变更原因。",
        )
    return reason


def _semantic_version_parts(value: str) -> tuple[int, int, int] | None:
    """只解析三段数字版本，忽略专用分支等非共享版本标签。"""
    parts = value.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])


def _next_shared_version_label(versions: Sequence[str]) -> str:
    """从现有共享版本中分配唯一的下一次次版本标签。"""
    parsed = [parts for value in versions if (parts := _semantic_version_parts(value))]
    if not parsed:
        return "1.0.0"
    major, minor, _patch = max(parsed)
    return f"{major}.{minor + 1}.0"


def _required_text(value: str | None, field_name: str) -> str:
    """返回非空工具输入；缺失时用稳定领域错误阻止部分写入。"""
    normalized = str(value or "").strip()
    if not normalized:
        raise knowledge_error(
            KNOWLEDGE_MODE_INVALID,
            message=f"{field_name} 不能为空。",
        )
    return normalized


def _safe_source_filename(value: str | None) -> str:
    """只接受单层来源文件名，拒绝路径穿越和控制字符。"""
    filename = _required_text(value, "filename")
    if (
        filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or "\x00" in filename
        or len(filename) > 255
    ):
        raise knowledge_error(
            KNOWLEDGE_MODE_INVALID,
            message="filename 必须是安全的单层文件名。",
        )
    return filename


class SharedKnowledgeVersionService:
    """在调用方事务内维护共享知识库唯一正式版本指针。"""

    def __init__(self, db: Session) -> None:
        """绑定数据库会话；所有生命周期状态与审计共用该事务。"""
        self.db = db
        self.audit = KnowledgeAuditService(db)

    def create_draft(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        source_team_id: str | None,
        actor_type: str,
        actor_id: str,
        change_reason: str,
        expected_published_version_id: str | None = None,
        source_task_id: str | None = None,
        source_conversation_id: str | None = None,
        source_references: list[dict[str, Any]] | None = None,
        idempotency_key: str | None = None,
        request_payload: Any = None,
    ) -> KnowledgeBaseVersion:
        """从当前正式快照克隆可编辑草稿，并持久化完整来源。"""
        base = self._shared_base(tenant_id, knowledge_base_id)
        parent = self._published_version(base)
        if (
            expected_published_version_id is not None
            and expected_published_version_id != parent.id
        ):
            raise self._publish_conflict(base, expected_published_version_id)

        # 版本标签按已有快照单调分配；唯一约束负责检测并行分配冲突。
        labels = self.db.exec(
            select(KnowledgeBaseVersion.version).where(
                KnowledgeBaseVersion.tenant_id == tenant_id,
                KnowledgeBaseVersion.knowledge_base_id == knowledge_base_id,
            )
        ).all()
        reason = _required_reason(change_reason)
        provenance = {
            "source_task_id": source_task_id,
            "source_conversation_id": source_conversation_id,
            "source_references": list(source_references or []),
        }
        draft = KnowledgeBaseVersion(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            version=_next_shared_version_label(labels),
            name=base.name,
            description=base.description,
            status="active",
            parent_version_id=parent.id,
            publication_state="draft",
            source_team_id=source_team_id,
            created_by_agent_id=actor_id if actor_type == "agent" else None,
            created_by_user_id=actor_id if actor_type == "user" else None,
            change_reason=reason,
            capability_scope=base.capability_scope,
            metadata_json={
                **dict(base.metadata_json or {}),
                "draft_change_reason": reason,
                "provenance": provenance,
            },
        )
        try:
            with self.db.begin_nested():
                self.db.add(draft)
                self.db.flush()
        except IntegrityError as exc:
            raise self._publish_conflict(base, parent.id) from exc
        clone_knowledge_version_assets(
            self.db,
            tenant_id,
            knowledge_base_id,
            parent.id,
            draft.id,
        )
        self.audit.append_event(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            team_id=source_team_id,
            knowledge_base_version_id=draft.id,
            actor_type=actor_type,
            actor_id=actor_id,
            action="draft_created",
            reason=reason,
            details={
                "parent_version_id": parent.id,
                "version": draft.version,
                "provenance": provenance,
            },
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            durable_result={
                "knowledge_base_id": knowledge_base_id,
                "draft_version_id": draft.id,
                "parent_published_version_id": parent.id,
                "publication_state": "draft",
            },
        )
        return draft

    def list_versions(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        publication_state: str | None = None,
    ) -> list[KnowledgeBaseVersion]:
        """按创建时间倒序列出共享版本，可选生命周期状态过滤。"""
        self._shared_base(tenant_id, knowledge_base_id)
        statement = select(KnowledgeBaseVersion).where(
            KnowledgeBaseVersion.tenant_id == tenant_id,
            KnowledgeBaseVersion.knowledge_base_id == knowledge_base_id,
        )
        if publication_state:
            statement = statement.where(
                KnowledgeBaseVersion.publication_state == publication_state
            )
        return list(
            self.db.exec(
                statement.order_by(KnowledgeBaseVersion.created_at.desc())
            ).all()
        )

    def require_writable_draft(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        version_id: str,
    ) -> KnowledgeBaseVersion:
        """只返回同库草稿，正式或已驳回快照一律拒绝写入。"""
        self._shared_base(tenant_id, knowledge_base_id)
        version = self._version(tenant_id, knowledge_base_id, version_id)
        if version.publication_state != "draft":
            raise knowledge_error(
                KNOWLEDGE_MODE_INVALID,
                message="共享知识库正式版本或已驳回版本不可修改。",
                details={"knowledge_base_version_id": version.id},
            )
        return version

    def ensure_ready(self, version: KnowledgeBaseVersion) -> None:
        """确认草稿内文档全部就绪，且没有未成功的摄取任务。"""
        blocking_document = self.db.exec(
            select(KnowledgeDocument.id).where(
                KnowledgeDocument.tenant_id == version.tenant_id,
                KnowledgeDocument.knowledge_base_id == version.knowledge_base_id,
                KnowledgeDocument.knowledge_base_version_id == version.id,
                KnowledgeDocument.status != "ready",
            )
        ).first()
        blocking_job = self.db.exec(
            select(KnowledgeIngestJob.id).where(
                KnowledgeIngestJob.tenant_id == version.tenant_id,
                KnowledgeIngestJob.knowledge_base_id == version.knowledge_base_id,
                KnowledgeIngestJob.knowledge_base_version_id == version.id,
                KnowledgeIngestJob.status.not_in(("succeeded", "failed", "cancelled")),
            )
        ).first()
        if blocking_document or blocking_job:
            raise knowledge_error(
                KNOWLEDGE_VERSION_NOT_READY,
                details={"knowledge_base_version_id": version.id},
            )

    def queue_draft_update(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        draft_version_id: str,
        actor_id: str,
        source_team_id: str,
        source_task_id: str,
        source_conversation_id: str,
        title: str,
        filename: str,
        content: str,
        source_references: list[dict[str, Any]] | None,
        idempotency_key: str,
        request_payload: Any,
    ) -> KnowledgeIngestJob:
        """为共享草稿创建一个带来源与幂等收据的持久摄取任务。"""
        draft = self.require_writable_draft(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            version_id=draft_version_id,
        )
        resolved_title = _required_text(title, "title")
        resolved_filename = _safe_source_filename(filename)
        resolved_content = _required_text(content, "content")
        provenance = {
            "source_task_id": source_task_id,
            "source_conversation_id": source_conversation_id,
            "source_references": list(source_references or []),
            "created_by_agent_id": actor_id,
            "source_team_id": source_team_id,
        }

        # 摄取任务与审计收据在同一事务内写入，重试不会创建第二个任务。
        job = KnowledgeIngestJob(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            knowledge_base_version_id=draft.id,
            filename=resolved_filename,
            status="queued",
            stage="queued",
            progress=0.0,
            metadata_json={
                "content_base64": base64.b64encode(
                    resolved_content.encode("utf-8")
                ).decode("ascii"),
                "title": resolved_title,
                "metadata": {
                    "provenance": provenance,
                    "source_references": list(source_references or []),
                },
            },
        )
        self.db.add(job)
        self.db.flush()
        self.audit.append_event(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            team_id=source_team_id,
            knowledge_base_version_id=draft.id,
            actor_type="agent",
            actor_id=actor_id,
            action="draft_updated",
            details={
                "ingest_job_id": job.id,
                "filename": resolved_filename,
                "provenance": provenance,
            },
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            durable_result={
                "knowledge_base_id": knowledge_base_id,
                "draft_version_id": draft.id,
                "document_id": None,
                "ingest_job_id": job.id,
                "processing_state": job.status,
            },
        )
        return job

    def publish_draft(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        draft_version_id: str,
        expected_published_version_id: str,
        actor_type: str,
        actor_id: str,
        source_team_id: str | None,
        change_reason: str,
        idempotency_key: str | None = None,
        request_payload: Any = None,
    ) -> KnowledgeBaseVersion:
        """校验草稿后以 CAS 原子替换全局正式指针。"""
        base = self._shared_base(tenant_id, knowledge_base_id)
        draft = self.require_writable_draft(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            version_id=draft_version_id,
        )
        reason = _required_reason(change_reason)
        if draft.parent_version_id != expected_published_version_id:
            raise self._publish_conflict(base, expected_published_version_id)
        self.ensure_ready(draft)

        # 单条条件更新是全局正式版本的并发闸门；失败时不触碰草稿状态。
        now = utc_now()
        result = self.db.exec(
            update(KnowledgeBase)
            .where(
                KnowledgeBase.id == knowledge_base_id,
                KnowledgeBase.tenant_id == tenant_id,
                KnowledgeBase.mode == "shared",
                KnowledgeBase.published_version_id == expected_published_version_id,
            )
            .values(published_version_id=draft.id, updated_at=now)
        )
        if result.rowcount != 1:
            self.db.expire(base)
            raise self._publish_conflict(base, expected_published_version_id)

        draft.publication_state = "released"
        draft.published_at = now
        draft.change_reason = reason
        draft.updated_at = now
        self.db.add(draft)
        self.db.flush()
        self.db.refresh(base)
        base.metadata_json = {
            **dict(base.metadata_json or {}),
            "current_version": draft.version,
        }
        self.db.add(base)
        self.audit.append_event(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            team_id=source_team_id,
            knowledge_base_version_id=draft.id,
            actor_type=actor_type,
            actor_id=actor_id,
            action="version_published",
            reason=reason,
            details={
                "previous_published_version_id": expected_published_version_id,
                "published_version_id": draft.id,
            },
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            durable_result={
                "knowledge_base_id": knowledge_base_id,
                "previous_published_version_id": expected_published_version_id,
                "published_version_id": draft.id,
                "publication_state": "released",
                "published_at": now.isoformat(),
                "global_scope_notice": "该版本将供所有已绑定且有权的团队在下一轮使用。",
            },
        )
        return draft

    def reject_draft(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        draft_version_id: str,
        actor_type: str,
        actor_id: str,
        source_team_id: str | None,
        change_reason: str,
        idempotency_key: str | None = None,
        request_payload: Any = None,
    ) -> KnowledgeBaseVersion:
        """驳回草稿但保留其快照、来源和后续审计。"""
        draft = self.require_writable_draft(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            version_id=draft_version_id,
        )
        reason = _required_reason(change_reason)
        draft.publication_state = "rejected"
        draft.change_reason = reason
        draft.updated_at = utc_now()
        self.db.add(draft)
        self.audit.append_event(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            team_id=source_team_id,
            knowledge_base_version_id=draft.id,
            actor_type=actor_type,
            actor_id=actor_id,
            action="draft_rejected",
            reason=reason,
            details={"draft_version_id": draft.id},
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            durable_result={
                "knowledge_base_id": knowledge_base_id,
                "draft_version_id": draft.id,
                "publication_state": "rejected",
            },
        )
        return draft

    def rollback(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        target_version_id: str,
        expected_published_version_id: str,
        actor_type: str,
        actor_id: str,
        source_team_id: str | None,
        change_reason: str,
        idempotency_key: str | None = None,
        request_payload: Any = None,
    ) -> KnowledgeBaseVersion:
        """以 CAS 将指针恢复到历史正式快照，不删除任何后续版本。"""
        base = self._shared_base(tenant_id, knowledge_base_id)
        target = self._version(tenant_id, knowledge_base_id, target_version_id)
        if target.publication_state != "released":
            raise knowledge_error(
                KNOWLEDGE_MODE_INVALID,
                message="只能回滚到同一共享知识库的正式版本。",
                details={"target_version_id": target_version_id},
            )
        reason = _required_reason(change_reason)
        now = utc_now()
        result = self.db.exec(
            update(KnowledgeBase)
            .where(
                KnowledgeBase.id == knowledge_base_id,
                KnowledgeBase.tenant_id == tenant_id,
                KnowledgeBase.mode == "shared",
                KnowledgeBase.published_version_id == expected_published_version_id,
            )
            .values(published_version_id=target.id, updated_at=now)
        )
        if result.rowcount != 1:
            self.db.expire(base)
            raise self._publish_conflict(base, expected_published_version_id)

        self.db.flush()
        self.db.refresh(base)
        base.metadata_json = {
            **dict(base.metadata_json or {}),
            "current_version": target.version,
        }
        self.db.add(base)
        self.audit.append_event(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            team_id=source_team_id,
            knowledge_base_version_id=target.id,
            actor_type=actor_type,
            actor_id=actor_id,
            action="version_rolled_back",
            reason=reason,
            details={
                "previous_published_version_id": expected_published_version_id,
                "target_version_id": target.id,
            },
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            durable_result={
                "knowledge_base_id": knowledge_base_id,
                "previous_published_version_id": expected_published_version_id,
                "target_version_id": target.id,
                "published_at": now.isoformat(),
            },
        )
        return target

    def _shared_base(self, tenant_id: str, knowledge_base_id: str) -> KnowledgeBase:
        """读取同租户活动共享库，并隐藏跨租户资源存在性。"""
        base = self.db.get(KnowledgeBase, knowledge_base_id)
        if not base or base.tenant_id != tenant_id or base.status != "active":
            raise knowledge_error(KNOWLEDGE_CONTEXT_MISMATCH)
        if base.mode != "shared":
            raise knowledge_error(KNOWLEDGE_MODE_INVALID)
        return base

    def _published_version(self, base: KnowledgeBase) -> KnowledgeBaseVersion:
        """校验共享库正式指针指向同库已发布快照。"""
        if not base.published_version_id:
            raise knowledge_error(
                KNOWLEDGE_MODE_INVALID,
                message="共享知识库尚未配置正式版本。",
            )
        version = self._version(base.tenant_id, base.id, base.published_version_id)
        if version.publication_state != "released":
            raise knowledge_error(
                KNOWLEDGE_MODE_INVALID,
                message="共享知识库正式指针无效。",
            )
        return version

    def _version(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        version_id: str,
    ) -> KnowledgeBaseVersion:
        """读取同租户同知识库版本，拒绝跨库或跨租户标识。"""
        version = self.db.get(KnowledgeBaseVersion, version_id)
        if (
            not version
            or version.tenant_id != tenant_id
            or version.knowledge_base_id != knowledge_base_id
        ):
            raise knowledge_error(KNOWLEDGE_CONTEXT_MISMATCH)
        return version

    def _publish_conflict(
        self,
        base: KnowledgeBase,
        expected_version_id: str | None,
    ) -> KnowledgeError:
        """构造只暴露版本标识的安全发布冲突。"""
        return knowledge_error(
            KNOWLEDGE_PUBLISH_CONFLICT,
            details={
                "expected_published_version_id": expected_version_id,
                "current_published_version_id": base.published_version_id,
            },
        )
