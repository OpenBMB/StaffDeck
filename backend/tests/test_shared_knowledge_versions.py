from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select
from test_teams_api import _test_session

from app.api.knowledge_bases import (
    create_shared_knowledge_draft,
    list_knowledge_base_versions,
    list_shared_knowledge_audit_events,
    list_shared_knowledge_teams,
    publish_shared_knowledge_version,
    reject_shared_knowledge_version,
    rollback_knowledge_base,
)
from app.db.models import (
    KnowledgeBase,
    KnowledgeBaseAuditEvent,
    KnowledgeBaseVersion,
    KnowledgeDocument,
    KnowledgeIngestJob,
    Team,
    TeamKnowledgeBaseBinding,
    Tenant,
    User,
)
from app.knowledge.errors import KnowledgeError
from app.knowledge.schema import (
    SharedKnowledgeDraftCreateRequest,
    SharedKnowledgePublishRequest,
    SharedKnowledgeRejectRequest,
    SharedKnowledgeRollbackRequest,
)
from app.knowledge.versioning import SharedKnowledgeVersionService


def _seed_shared_base(db: Session) -> tuple[KnowledgeBase, KnowledgeBaseVersion]:
    """创建带一份就绪文档的共享知识库正式快照。"""
    db.add(Tenant(id="tenant_demo", name="Demo"))
    released = KnowledgeBaseVersion(
        id="kbver_shared_100",
        tenant_id="tenant_demo",
        knowledge_base_id="kb_shared",
        version="1.0.0",
        name="内容知识库",
        publication_state="released",
    )
    base = KnowledgeBase(
        id="kb_shared",
        tenant_id="tenant_demo",
        name="内容知识库",
        mode="shared",
        published_version_id=released.id,
    )
    db.add(base)
    db.add(released)
    db.add(
        KnowledgeDocument(
            id="kdoc_release",
            tenant_id="tenant_demo",
            knowledge_base_id=base.id,
            knowledge_base_version_id=released.id,
            filename="选题库.md",
            file_type="md",
            title="选题库",
            status="ready",
            metadata_json={"source": "manual"},
        )
    )
    db.commit()
    return base, released


def _create_draft(
    db: Session,
    *,
    expected_published_version_id: str,
    reason: str = "补充本周选题",
) -> KnowledgeBaseVersion:
    """通过用户身份创建一份来源可追踪的共享草稿。"""
    return SharedKnowledgeVersionService(db).create_draft(
        tenant_id="tenant_demo",
        knowledge_base_id="kb_shared",
        source_team_id="team_content",
        actor_type="user",
        actor_id="user_admin",
        change_reason=reason,
        expected_published_version_id=expected_published_version_id,
        source_task_id="task_topic",
        source_conversation_id="session_group",
        source_references=[{"type": "url", "value": "https://example.test/source"}],
    )


def _bind_team(db: Session, base: KnowledgeBase) -> Team:
    """为生命周期 API 创建一个由普通用户拥有的有效团队绑定。"""
    team = Team(
        id="team_content",
        tenant_id=base.tenant_id,
        name="内容团队",
        owner_user_id="user_owner",
    )
    db.add(team)
    db.add(
        TeamKnowledgeBaseBinding(
            id="teamkb_content",
            tenant_id=base.tenant_id,
            team_id=team.id,
            knowledge_base_id=base.id,
            created_by_user_id="user_admin",
        )
    )
    db.commit()
    return team


def _owner_user() -> User:
    """返回不具备租户管理员角色的团队创建者。"""
    return User(
        id="user_owner",
        tenant_id="tenant_demo",
        username="owner",
        role="member",
        password_hash="test",
    )


def _admin_user() -> User:
    """返回用于全局管理场景的租户管理员。"""
    return User(
        id="user_admin",
        tenant_id="tenant_demo",
        username="admin",
        role="admin",
        password_hash="test",
    )


def test_create_draft_clones_published_snapshot_and_records_provenance() -> None:
    """草稿从当前正式快照复制，且不提前改变全局正式指针。"""
    with _test_session() as db:
        base, released = _seed_shared_base(db)

        draft = _create_draft(db, expected_published_version_id=released.id)
        db.commit()
        db.refresh(base)

        assert draft.publication_state == "draft"
        assert draft.parent_version_id == released.id
        assert draft.source_team_id == "team_content"
        assert draft.created_by_user_id == "user_admin"
        assert draft.created_by_agent_id is None
        assert draft.change_reason == "补充本周选题"
        assert draft.metadata_json["provenance"] == {
            "source_task_id": "task_topic",
            "source_conversation_id": "session_group",
            "source_references": [{"type": "url", "value": "https://example.test/source"}],
        }
        assert base.published_version_id == released.id

        cloned = db.exec(
            select(KnowledgeDocument).where(
                KnowledgeDocument.knowledge_base_version_id == draft.id
            )
        ).one()
        assert cloned.id != "kdoc_release"
        assert cloned.filename == "选题库.md"
        assert cloned.status == "ready"

        event = db.exec(
            select(KnowledgeBaseAuditEvent).where(
                KnowledgeBaseAuditEvent.action == "draft_created"
            )
        ).one()
        assert event.knowledge_base_version_id == draft.id
        assert event.team_id == "team_content"
        assert event.actor_type == "user"
        assert event.actor_id == "user_admin"
        assert event.details_json["parent_version_id"] == released.id


def test_shared_knowledge_teams_are_loaded_in_one_authorized_projection() -> None:
    """管理员看全部活动绑定，普通所有者只看到自己可管理的绑定团队。"""
    with _test_session() as db:
        base, _released = _seed_shared_base(db)
        owned = _bind_team(db, base)
        other = Team(
            id="team_other",
            tenant_id="tenant_demo",
            name="其他团队",
            owner_user_id="user_other",
        )
        db.add(other)
        db.add(
            TeamKnowledgeBaseBinding(
                id="teamkb_other",
                tenant_id="tenant_demo",
                team_id=other.id,
                knowledge_base_id=base.id,
                created_by_user_id="user_admin",
            )
        )
        db.commit()

        owner_rows = list_shared_knowledge_teams(
            base.id,
            "tenant_demo",
            db,
            User(
                id="user_owner",
                tenant_id="tenant_demo",
                username="owner",
                role="member",
                password_hash="test",
            ),
        )
        admin_rows = list_shared_knowledge_teams(
            base.id,
            "tenant_demo",
            db,
            _admin_user(),
        )

    assert [(row.id, row.name) for row in owner_rows] == [(owned.id, owned.name)]
    assert [(row.id, row.name) for row in admin_rows] == [
        (other.id, other.name),
        (owned.id, owned.name),
    ]


def test_create_draft_maps_version_label_collision_to_publish_conflict(monkeypatch) -> None:
    """并行草稿版本标签冲突时返回可重试领域错误，不泄漏数据库异常。"""
    with _test_session() as db:
        _base, released = _seed_shared_base(db)
        monkeypatch.setattr(
            "app.knowledge.versioning._next_shared_version_label",
            lambda _labels: released.version,
        )

        with pytest.raises(KnowledgeError) as conflict:
            _create_draft(db, expected_published_version_id=released.id)

    assert conflict.value.code == "KNOWLEDGE_PUBLISH_CONFLICT"


def test_publish_requires_ready_ingestion_and_freezes_released_version() -> None:
    """未完成或失败的摄取阻止发布；发布后该快照不再允许写入。"""
    with _test_session() as db:
        base, released = _seed_shared_base(db)
        draft = _create_draft(db, expected_published_version_id=released.id)
        db.add(
            KnowledgeIngestJob(
                id="kjob_pending",
                tenant_id="tenant_demo",
                knowledge_base_id=base.id,
                knowledge_base_version_id=draft.id,
                filename="新增资料.md",
                status="running",
            )
        )
        db.commit()
        service = SharedKnowledgeVersionService(db)

        with pytest.raises(KnowledgeError) as not_ready:
            service.publish_draft(
                tenant_id="tenant_demo",
                knowledge_base_id=base.id,
                draft_version_id=draft.id,
                expected_published_version_id=released.id,
                actor_type="user",
                actor_id="user_admin",
                source_team_id="team_content",
                change_reason="审核通过",
            )
        assert not_ready.value.code == "KNOWLEDGE_VERSION_NOT_READY"
        db.refresh(base)
        db.refresh(draft)
        assert base.published_version_id == released.id
        assert draft.publication_state == "draft"

        job = db.get(KnowledgeIngestJob, "kjob_pending")
        assert job is not None
        job.status = "succeeded"
        db.add(job)
        published = service.publish_draft(
            tenant_id="tenant_demo",
            knowledge_base_id=base.id,
            draft_version_id=draft.id,
            expected_published_version_id=released.id,
            actor_type="user",
            actor_id="user_admin",
            source_team_id="team_content",
            change_reason="审核通过",
        )
        db.commit()
        db.refresh(base)

        assert published.publication_state == "released"
        assert published.published_at is not None
        assert published.change_reason == "审核通过"
        assert base.published_version_id == published.id
        with pytest.raises(KnowledgeError) as immutable:
            service.require_writable_draft(
                tenant_id="tenant_demo",
                knowledge_base_id=base.id,
                version_id=published.id,
            )
        assert immutable.value.code == "KNOWLEDGE_MODE_INVALID"


def test_terminal_failed_or_cancelled_ingest_jobs_do_not_block_ready_draft() -> None:
    """已终止任务不再阻塞发布；失败文档本身仍保持阻塞。"""
    with _test_session() as db:
        _base, released = _seed_shared_base(db)
        draft = _create_draft(db, expected_published_version_id=released.id)
        for job_id, status in (("kjob_failed", "failed"), ("kjob_cancelled", "cancelled")):
            db.add(
                KnowledgeIngestJob(
                    id=job_id,
                    tenant_id="tenant_demo",
                    knowledge_base_id="kb_shared",
                    knowledge_base_version_id=draft.id,
                    filename=f"{job_id}.md",
                    status=status,
                )
            )
        db.commit()
        service = SharedKnowledgeVersionService(db)

        service.ensure_ready(draft)

        failed_document = KnowledgeDocument(
            id="kdoc_failed",
            tenant_id="tenant_demo",
            knowledge_base_id="kb_shared",
            knowledge_base_version_id=draft.id,
            filename="failed.md",
            file_type="md",
            status="failed",
        )
        db.add(failed_document)
        db.commit()
        with pytest.raises(KnowledgeError) as not_ready:
            service.ensure_ready(draft)

    assert not_ready.value.code == "KNOWLEDGE_VERSION_NOT_READY"


def test_publish_compare_and_swap_rejects_stale_parallel_draft() -> None:
    """同一正式版本派生的并行草稿只能有一个原子更新全局指针。"""
    with _test_session() as db:
        base, released = _seed_shared_base(db)
        first = _create_draft(
            db,
            expected_published_version_id=released.id,
            reason="团队 A 修改",
        )
        second = _create_draft(
            db,
            expected_published_version_id=released.id,
            reason="团队 B 修改",
        )
        db.commit()
        service = SharedKnowledgeVersionService(db)

        service.publish_draft(
            tenant_id="tenant_demo",
            knowledge_base_id=base.id,
            draft_version_id=first.id,
            expected_published_version_id=released.id,
            actor_type="user",
            actor_id="user_admin",
            source_team_id="team_content",
            change_reason="发布团队 A 草稿",
        )
        db.commit()

        with pytest.raises(KnowledgeError) as conflict:
            service.publish_draft(
                tenant_id="tenant_demo",
                knowledge_base_id=base.id,
                draft_version_id=second.id,
                expected_published_version_id=released.id,
                actor_type="user",
                actor_id="user_admin",
                source_team_id="team_research",
                change_reason="发布团队 B 草稿",
            )
        assert conflict.value.code == "KNOWLEDGE_PUBLISH_CONFLICT"
        assert conflict.value.details["current_published_version_id"] == first.id
        db.refresh(base)
        db.refresh(second)
        assert base.published_version_id == first.id
        assert second.publication_state == "draft"
        assert len(
            db.exec(
                select(KnowledgeBaseAuditEvent).where(
                    KnowledgeBaseAuditEvent.action == "version_published"
                )
            ).all()
        ) == 1


def test_reject_preserves_draft_and_rollback_only_moves_global_pointer() -> None:
    """驳回与回滚保留全部历史快照，只改变生命周期或正式指针。"""
    with _test_session() as db:
        base, original = _seed_shared_base(db)
        rejected = _create_draft(
            db,
            expected_published_version_id=original.id,
            reason="待核实素材",
        )
        service = SharedKnowledgeVersionService(db)
        service.reject_draft(
            tenant_id="tenant_demo",
            knowledge_base_id=base.id,
            draft_version_id=rejected.id,
            actor_type="user",
            actor_id="user_admin",
            source_team_id="team_content",
            change_reason="来源不足",
        )
        db.commit()
        db.refresh(rejected)
        assert rejected.publication_state == "rejected"
        assert rejected.change_reason == "来源不足"
        assert db.exec(
            select(KnowledgeDocument).where(
                KnowledgeDocument.knowledge_base_version_id == rejected.id
            )
        ).one()

        accepted = _create_draft(
            db,
            expected_published_version_id=original.id,
            reason="已核验素材",
        )
        service.publish_draft(
            tenant_id="tenant_demo",
            knowledge_base_id=base.id,
            draft_version_id=accepted.id,
            expected_published_version_id=original.id,
            actor_type="user",
            actor_id="user_admin",
            source_team_id="team_content",
            change_reason="正式发布",
        )
        db.commit()

        restored = service.rollback(
            tenant_id="tenant_demo",
            knowledge_base_id=base.id,
            target_version_id=original.id,
            expected_published_version_id=accepted.id,
            actor_type="user",
            actor_id="user_admin",
            source_team_id="team_content",
            change_reason="恢复上一正式版本",
        )
        db.commit()
        db.refresh(base)
        db.refresh(accepted)
        db.refresh(original)

        assert restored.id == original.id
        assert base.published_version_id == original.id
        assert original.publication_state == "released"
        assert accepted.publication_state == "released"
        events = db.exec(
            select(KnowledgeBaseAuditEvent).where(
                KnowledgeBaseAuditEvent.action.in_(
                    ["draft_rejected", "version_published", "version_rolled_back"]
                )
            )
        ).all()
        assert {event.action for event in events} == {
            "draft_rejected",
            "version_published",
            "version_rolled_back",
        }
        rollback_event = next(
            event for event in events if event.action == "version_rolled_back"
        )
        assert rollback_event.details_json == {
            "previous_published_version_id": accepted.id,
            "target_version_id": original.id,
        }


def test_lifecycle_endpoints_commit_and_project_shared_version_history() -> None:
    """团队所有者可创建、发布、驳回和回滚，并读取完整共享版本投影。"""
    with _test_session() as db:
        base, original = _seed_shared_base(db)
        _bind_team(db, base)
        owner = _owner_user()

        draft = create_shared_knowledge_draft(
            base.id,
            SharedKnowledgeDraftCreateRequest(
                tenant_id="tenant_demo",
                team_id="team_content",
                change_reason="准备第二版",
                expected_published_version_id=original.id,
            ),
            db=db,
            current_user=owner,
        )
        assert draft.publication_state == "draft"
        assert draft.parent_version_id == original.id
        assert draft.is_published_head is False

        published = publish_shared_knowledge_version(
            base.id,
            draft.id,
            SharedKnowledgePublishRequest(
                tenant_id="tenant_demo",
                team_id="team_content",
                expected_published_version_id=original.id,
                change_reason="发布第二版",
            ),
            db=db,
            current_user=owner,
        )
        assert published.publication_state == "released"
        assert published.is_published_head is True

        rejected_draft = create_shared_knowledge_draft(
            base.id,
            SharedKnowledgeDraftCreateRequest(
                tenant_id="tenant_demo",
                team_id="team_content",
                change_reason="未完成第三版",
                expected_published_version_id=published.id,
            ),
            db=db,
            current_user=owner,
        )
        rejected = reject_shared_knowledge_version(
            base.id,
            rejected_draft.id,
            SharedKnowledgeRejectRequest(
                tenant_id="tenant_demo",
                team_id="team_content",
                change_reason="暂不采用",
            ),
            db=db,
            current_user=owner,
        )
        assert rejected.publication_state == "rejected"

        rolled_back = rollback_knowledge_base(
            base.id,
            SharedKnowledgeRollbackRequest(
                tenant_id="tenant_demo",
                team_id="team_content",
                target_version_id=original.id,
                expected_published_version_id=published.id,
                change_reason="恢复初始版本",
            ),
            db=db,
            current_user=owner,
        )
        assert rolled_back["target_version_id"] == original.id

        create_shared_knowledge_draft(
            base.id,
            SharedKnowledgeDraftCreateRequest(
                tenant_id="tenant_demo",
                team_id="team_content",
                change_reason="回滚后继续准备",
                expected_published_version_id=original.id,
            ),
            db=db,
            current_user=owner,
        )

        versions = list_knowledge_base_versions(
            base.id,
            tenant_id="tenant_demo",
            agent_id=None,
            db=db,
            current_user=owner,
        )
        assert {item["publication_state"] for item in versions} == {
            "draft",
            "released",
            "rejected",
        }
        assert next(item for item in versions if item["id"] == original.id)[
            "is_published_head"
        ] is True
        assert next(item for item in versions if item["id"] == published.id)[
            "is_published_head"
        ] is False
        assert next(item for item in versions if item["id"] == rejected.id)[
            "source_team_id"
        ] == "team_content"
        audit_events = list_shared_knowledge_audit_events(
            base.id,
            tenant_id="tenant_demo",
            db=db,
            current_user=owner,
        )
        assert {
            "draft_created",
            "version_published",
            "draft_rejected",
            "version_rolled_back",
        }.issubset({item.action for item in audit_events.items})


def test_lifecycle_endpoints_require_team_owner_or_admin_and_active_binding() -> None:
    """浏览器提交的团队 ID 必须真实绑定，且调用者必须是团队所有者或管理员。"""
    with _test_session() as db:
        base, original = _seed_shared_base(db)
        _bind_team(db, base)
        outsider = User(
            id="user_outsider",
            tenant_id="tenant_demo",
            username="outsider",
            role="member",
            password_hash="test",
        )
        request = SharedKnowledgeDraftCreateRequest(
            tenant_id="tenant_demo",
            team_id="team_content",
            change_reason="无权修改",
            expected_published_version_id=original.id,
        )

        with pytest.raises(HTTPException) as forbidden:
            create_shared_knowledge_draft(
                base.id,
                request,
                db=db,
                current_user=outsider,
            )
        assert forbidden.value.status_code == 403

        binding = db.get(TeamKnowledgeBaseBinding, "teamkb_content")
        assert binding is not None
        binding.status = "revoked"
        db.add(binding)
        db.commit()
        with pytest.raises(HTTPException) as unbound:
            create_shared_knowledge_draft(
                base.id,
                request,
                db=db,
                current_user=_admin_user(),
            )
        assert unbound.value.status_code == 403
        assert unbound.value.detail["code"] == "KNOWLEDGE_CONTEXT_MISMATCH"
        assert db.exec(
            select(KnowledgeBaseVersion).where(
                KnowledgeBaseVersion.publication_state == "draft"
            )
        ).first() is None
