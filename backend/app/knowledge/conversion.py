"""专用员工知识分支到独立共享知识谱系的可回滚转换。"""

from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import Session, select

from app.agents.branching import (
    archive_agent_private_knowledge_branch,
    clone_knowledge_version_assets,
)
from app.db.models import (
    AgentKnowledgeBranch,
    AgentProfile,
    KnowledgeBase,
    KnowledgeBaseVersion,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeConcept,
    KnowledgeDiscoverySuggestion,
    KnowledgeDocument,
    Team,
    TeamKnowledgeBaseBinding,
    utc_now,
)
from app.knowledge.audit import KnowledgeAuditService
from app.knowledge.errors import (
    KNOWLEDGE_CONVERSION_VALIDATION_FAILED,
    KNOWLEDGE_MODE_INVALID,
    KnowledgeError,
)


class KnowledgeConversionError(KnowledgeError):
    """Base error for an invalid or unsafe dedicated-to-shared conversion."""

    def __init__(
        self,
        message: str,
        *,
        code: str = KNOWLEDGE_MODE_INVALID,
        status_code: int = 409,
    ) -> None:
        """Attach a stable API code without exposing tenant-scoped records."""
        super().__init__(code, message, status_code=status_code)


class KnowledgeConversionValidationError(KnowledgeConversionError):
    """Raised when cloned version assets do not match the selected source."""

    def __init__(self, message: str) -> None:
        """Mark clone validation failures as retryable conversion conflicts."""
        super().__init__(
            message,
            code=KNOWLEDGE_CONVERSION_VALIDATION_FAILED,
            status_code=409,
        )


@dataclass(frozen=True)
class KnowledgeConversionResult:
    """Stable identifiers and validation evidence produced by one conversion."""

    source_knowledge_base_id: str
    source_version_id: str
    source_agent_id: str
    shared_knowledge_base_id: str
    released_version_id: str
    team_binding_ids: tuple[str, ...]
    default_for_team_id: str | None
    source_archival_state: str
    asset_counts: dict[str, int]
    audit_event_id: str


def _required_text(value: str | None, field_name: str) -> str:
    """Normalize required conversion input and reject blank values early."""
    normalized = str(value or "").strip()
    if not normalized:
        raise KnowledgeConversionError(f"{field_name} is required.")
    return normalized


def _safe_agent_id(value: str) -> str:
    """Mirror the stable branch-label encoding used by the dedicated branch layer."""
    return "".join(character if character.isalnum() else "_" for character in value)


def _asset_counts(
    db: Session,
    *,
    tenant_id: str,
    knowledge_base_id: str,
    version_id: str,
) -> dict[str, int]:
    """Count every asset family whose equality gates conversion visibility."""
    common_filters = (
        lambda model: (
            model.tenant_id == tenant_id,
            model.knowledge_base_id == knowledge_base_id,
            model.knowledge_base_version_id == version_id,
        )
    )
    counts = {
        "documents": len(
            db.exec(select(KnowledgeDocument.id).where(*common_filters(KnowledgeDocument))).all()
        ),
        "buckets": len(
            db.exec(select(KnowledgeBucket.id).where(*common_filters(KnowledgeBucket))).all()
        ),
        "chunks": len(
            db.exec(select(KnowledgeChunk.id).where(*common_filters(KnowledgeChunk))).all()
        ),
        "suggestions": len(
            db.exec(
                select(KnowledgeDiscoverySuggestion.id).where(
                    *common_filters(KnowledgeDiscoverySuggestion)
                )
            ).all()
        ),
    }
    counts["concepts"] = len(
        db.exec(
            select(KnowledgeConcept.id).where(
                *common_filters(KnowledgeConcept),
                KnowledgeConcept.status != "deleted",
            )
        ).all()
    )
    return counts


class KnowledgeConversionService:
    """Copy, validate, expose, then archive one selected dedicated instance."""

    def __init__(self, db: Session) -> None:
        """Bind the caller's database session without committing its outer transaction."""
        self.db = db
        self.audit = KnowledgeAuditService(db)

    def convert_to_shared(
        self,
        *,
        tenant_id: str,
        source_knowledge_base_id: str,
        source_agent_id: str,
        name: str,
        change_reason: str,
        actor_user_id: str,
        source_version_id: str | None = None,
        description: str | None = None,
        team_ids: list[str] | None = None,
        default_for_team_id: str | None = None,
    ) -> KnowledgeConversionResult:
        """Create a new shared lineage atomically from one employee branch snapshot."""
        shared_name = _required_text(name, "name")
        reason = _required_text(change_reason, "change_reason")
        actor_id = _required_text(actor_user_id, "actor_user_id")
        source_base, source_branch, source_version = self._source_context(
            tenant_id=tenant_id,
            source_knowledge_base_id=source_knowledge_base_id,
            source_agent_id=source_agent_id,
            source_version_id=source_version_id,
        )
        teams = self._target_teams(
            tenant_id=tenant_id,
            team_ids=team_ids,
            default_for_team_id=default_for_team_id,
        )
        existing_name = self.db.exec(
            select(KnowledgeBase.id).where(
                KnowledgeBase.tenant_id == tenant_id,
                KnowledgeBase.name == shared_name,
            )
        ).first()
        if existing_name is not None:
            raise KnowledgeConversionError("A knowledge base with this name already exists.")

        # The savepoint contains every visibility and archival mutation. Any
        # clone or validation error rolls it all back while preserving source data.
        with self.db.begin_nested():
            shared_base = KnowledgeBase(
                tenant_id=tenant_id,
                name=shared_name,
                description=str(description or "").strip() or None,
                status="converting",
                mode="shared",
                capability_scope=source_base.capability_scope,
                metadata_json={
                    "conversion": {
                        "source_knowledge_base_id": source_base.id,
                        "source_version_id": source_version.id,
                        "source_agent_id": source_branch.agent_id,
                    }
                },
            )
            self.db.add(shared_base)
            self.db.flush()
            released = KnowledgeBaseVersion(
                tenant_id=tenant_id,
                knowledge_base_id=shared_base.id,
                version="1.0.0",
                name=shared_base.name,
                description=shared_base.description,
                status="active",
                publication_state="released",
                created_by_user_id=actor_id,
                change_reason=reason,
                published_at=utc_now(),
                capability_scope=shared_base.capability_scope,
                metadata_json={
                    "conversion": {
                        "source_knowledge_base_id": source_base.id,
                        "source_version_id": source_version.id,
                        "source_agent_id": source_branch.agent_id,
                    }
                },
            )
            self.db.add(released)
            self.db.flush()
            clone_knowledge_version_assets(
                self.db,
                tenant_id,
                source_base.id,
                source_version.id,
                released.id,
                target_knowledge_base_id=shared_base.id,
            )
            self.db.flush()

            source_counts = _asset_counts(
                self.db,
                tenant_id=tenant_id,
                knowledge_base_id=source_base.id,
                version_id=source_version.id,
            )
            target_counts = _asset_counts(
                self.db,
                tenant_id=tenant_id,
                knowledge_base_id=shared_base.id,
                version_id=released.id,
            )
            if target_counts != source_counts:
                raise KnowledgeConversionValidationError(
                    f"Converted asset counts do not match source asset counts: "
                    f"source={source_counts}, target={target_counts}"
                )

            # Only after validation may the new global head become visible and
            # the selected employee instance leave active private listings.
            shared_base.published_version_id = released.id
            shared_base.status = "active"
            shared_base.updated_at = utc_now()
            self.db.add(shared_base)
            binding_ids = self._create_team_bindings(
                teams=teams,
                shared_base=shared_base,
                actor_user_id=actor_id,
                default_for_team_id=default_for_team_id,
            )
            archive_agent_private_knowledge_branch(
                self.db,
                tenant_id=tenant_id,
                agent_id=source_agent_id,
                knowledge_base_id=source_base.id,
                converted_to_knowledge_base_id=shared_base.id,
                converted_to_version_id=released.id,
            )
            event = self.audit.append_event(
                tenant_id=tenant_id,
                knowledge_base_id=shared_base.id,
                team_id=default_for_team_id,
                knowledge_base_version_id=released.id,
                actor_type="user",
                actor_id=actor_id,
                action="dedicated_converted",
                reason=reason,
                details={
                    "source_knowledge_base_id": source_base.id,
                    "source_version_id": source_version.id,
                    "source_agent_id": source_agent_id,
                    "asset_counts": target_counts,
                    "team_ids": [team.id for team in teams],
                    "default_for_team_id": default_for_team_id,
                    "source_archival_state": "archived",
                },
            )
            self.db.flush()
            result = KnowledgeConversionResult(
                source_knowledge_base_id=source_base.id,
                source_version_id=source_version.id,
                source_agent_id=source_agent_id,
                shared_knowledge_base_id=shared_base.id,
                released_version_id=released.id,
                team_binding_ids=tuple(binding_ids),
                default_for_team_id=default_for_team_id,
                source_archival_state="archived",
                asset_counts=target_counts,
                audit_event_id=event.id,
            )
        return result

    def _source_context(
        self,
        *,
        tenant_id: str,
        source_knowledge_base_id: str,
        source_agent_id: str,
        source_version_id: str | None,
    ) -> tuple[KnowledgeBase, AgentKnowledgeBranch, KnowledgeBaseVersion]:
        """Resolve one active dedicated branch and an owned source version."""
        source = self.db.get(KnowledgeBase, source_knowledge_base_id)
        agent = self.db.get(AgentProfile, source_agent_id)
        if (
            source is None
            or source.tenant_id != tenant_id
            or source.status != "active"
            or agent is None
            or agent.tenant_id != tenant_id
            or agent.status != "active"
        ):
            raise KnowledgeConversionError(
                "Selected dedicated knowledge source is unavailable.",
                code="KNOWLEDGE_CONTEXT_MISMATCH",
                status_code=404,
            )
        if source.mode != "dedicated":
            raise KnowledgeConversionError(
                "Shared knowledge cannot be converted back to dedicated mode."
            )
        branch = self.db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == tenant_id,
                AgentKnowledgeBranch.agent_id == source_agent_id,
                AgentKnowledgeBranch.knowledge_base_id == source.id,
                AgentKnowledgeBranch.status == "active",
            )
        ).first()
        if branch is None:
            raise KnowledgeConversionError(
                "Selected employee knowledge branch is unavailable.",
                code="KNOWLEDGE_CONTEXT_MISMATCH",
                status_code=404,
            )
        version = (
            self.db.get(KnowledgeBaseVersion, source_version_id)
            if source_version_id
            else self.db.exec(
                select(KnowledgeBaseVersion).where(
                    KnowledgeBaseVersion.tenant_id == tenant_id,
                    KnowledgeBaseVersion.knowledge_base_id == source.id,
                    KnowledgeBaseVersion.version == branch.head_version,
                )
            ).first()
        )
        marker = f"-branch.{_safe_agent_id(source_agent_id)}."
        if (
            version is None
            or version.tenant_id != tenant_id
            or version.knowledge_base_id != source.id
            or version.status != "active"
            or (
                version.version not in {branch.base_version, branch.head_version}
                and marker not in version.version
            )
        ):
            raise KnowledgeConversionError(
                "Selected version does not belong to this branch.",
                code="KNOWLEDGE_CONTEXT_MISMATCH",
                status_code=404,
            )
        return source, branch, version

    def _target_teams(
        self,
        *,
        tenant_id: str,
        team_ids: list[str] | None,
        default_for_team_id: str | None,
    ) -> list[Team]:
        """Validate and de-duplicate optional target teams before any clone starts."""
        normalized_ids = list(dict.fromkeys(str(value).strip() for value in team_ids or []))
        if any(not value for value in normalized_ids):
            raise KnowledgeConversionError("Team ids cannot be blank.")
        if default_for_team_id and default_for_team_id not in normalized_ids:
            raise KnowledgeConversionError("Default team must be included in team bindings.")
        teams: list[Team] = []
        for team_id in normalized_ids:
            team = self.db.get(Team, team_id)
            if team is None or team.tenant_id != tenant_id or team.status != "active":
                raise KnowledgeConversionError(
                    "Target team is unavailable or cross-tenant.",
                    code="KNOWLEDGE_CONTEXT_MISMATCH",
                    status_code=404,
                )
            teams.append(team)
        return teams

    def _create_team_bindings(
        self,
        *,
        teams: list[Team],
        shared_base: KnowledgeBase,
        actor_user_id: str,
        default_for_team_id: str | None,
    ) -> list[str]:
        """Create initial bindings and optionally select the new base as one team's default."""
        binding_ids: list[str] = []
        for team in teams:
            binding = TeamKnowledgeBaseBinding(
                tenant_id=team.tenant_id,
                team_id=team.id,
                knowledge_base_id=shared_base.id,
                created_by_user_id=actor_user_id,
                status="active",
                revision=1,
            )
            self.db.add(binding)
            binding_ids.append(binding.id)
            if team.id == default_for_team_id:
                team.default_knowledge_base_id = shared_base.id
                team.updated_at = utc_now()
                self.db.add(team)
        return binding_ids
