"""Shared definition normalization and publication for internal/public SOP APIs."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from types import SimpleNamespace

from fastapi import HTTPException
from sqlmodel import select

from staffdeck_harness.sop.runtime_client import RuntimeClient

LEGACY_WRITE_MODE_ENV = "SOP_RUNTIME_LEGACY_WRITES"


def legacy_sop_writes_disabled() -> bool:
    """Return whether the migration cutover has closed StaffDeck SOP writes."""
    return os.environ.get(LEGACY_WRITE_MODE_ENV, "compat").strip().lower() in {
        "disabled",
        "closed",
        "runtime_only",
    }


def ensure_legacy_sop_writes_enabled() -> None:
    if legacy_sop_writes_disabled():
        raise HTTPException(status_code=409, detail="SOP_LEGACY_WRITES_DISABLED")


def runtime_sop_id(tenant_id, agent_id, sop_id):
    return json.dumps(["staffdeck", tenant_id, agent_id, sop_id], separators=(",", ":"))


class SopManagementService:
    @staticmethod
    def scope(db, tenant_id, agent_id):
        from app.agents.branching import get_agent

        agent = get_agent(db, tenant_id, agent_id)
        if agent_id and agent is None:
            raise HTTPException(404, "Agent not found")
        return agent.id if agent and not agent.is_overall else ""

    @classmethod
    def rows(cls, db, tenant_id, agent_id, *, published_only=False):
        from app.db.models import SopDefinitionRecord

        query = select(SopDefinitionRecord).where(
            SopDefinitionRecord.tenant_id == tenant_id,
            SopDefinitionRecord.agent_id == cls.scope(db, tenant_id, agent_id),
            SopDefinitionRecord.status != "deleted",
        )
        if published_only:
            query = query.where(SopDefinitionRecord.status == "published")
        return list(db.exec(query.order_by(SopDefinitionRecord.updated_at.desc())).all())

    @classmethod
    def get(cls, db, tenant_id, agent_id, sop_id):
        row = next((r for r in cls.rows(db, tenant_id, agent_id) if r.skill_id == sop_id), None)
        if row is None:
            raise HTTPException(404, "SOP not found")
        return row

    @classmethod
    def save(
        cls,
        db,
        tenant_id,
        agent_id,
        content,
        *,
        status=None,
        create=False,
        metadata=None,
        commit=True,
    ):
        from app.db.models import SopDefinitionRecord, SopVersionRecord, utc_now

        value = cls.normalize(content)
        rows = cls.rows(db, tenant_id, agent_id)
        row = next((r for r in rows if r.skill_id == value["skill_id"]), None)
        if (status or (row.status if row else "draft")) == "published":
            catalog = {r.skill_id: r for r in rows if r.status == "published"}
            for node in value.get("nodes", []):
                if node.get("type") == "subflow" and not node.get("sub_sop_version"):
                    child = catalog.get(node.get("sub_sop_id"))
                    if child is None:
                        raise HTTPException(409, "SOP_CHILD_UNPUBLISHED")
                    node["sub_sop_version"] = child.version
        if create and row:
            raise HTTPException(409, "SOP ID already exists")
        if row is None:
            row = SopDefinitionRecord(
                tenant_id=tenant_id,
                agent_id=cls.scope(db, tenant_id, agent_id),
                skill_id=value["skill_id"],
                name=value["name"],
                content_json=value,
            )
        prior_version = db.exec(
            select(SopVersionRecord).where(
                SopVersionRecord.tenant_id == tenant_id,
                SopVersionRecord.agent_id == row.agent_id,
                SopVersionRecord.skill_id == value["skill_id"],
                SopVersionRecord.version == value["version"],
            )
        ).first()
        if prior_version and prior_version.content_json != value:
            raise HTTPException(409, "SOP_VERSION_CONFLICT")
        # Published versions are immutable in both management and Runtime stores.
        if (
            row.status == "published"
            and row.version == value["version"]
            and row.content_json != value
        ):
            raise HTTPException(409, "SOP_VERSION_CONFLICT")
        row.version, row.name = value["version"], value["name"]
        row.business_domain, row.description = (
            value.get("business_domain"),
            value.get("description"),
        )
        row.content_json, row.updated_at = value, utc_now()
        row.status = status or row.status
        if metadata is not None:
            row.metadata_json = deepcopy(metadata)
        try:
            db.add(row)
            db.flush()
            cls.bind(db, row)
            if row.status == "published":
                cls.publish_row(db, row, agent_id)
            if commit:
                db.commit()
        except Exception:
            db.rollback()
            raise
        db.refresh(row)
        return row

    @staticmethod
    def bind(db, row):
        from app.agents.branching import _ensure_binding, get_overall_agent

        agent_id = row.agent_id
        if not agent_id:
            overall = get_overall_agent(db, row.tenant_id)
            agent_id = overall.id if overall else None
        if agent_id:
            _ensure_binding(
                db,
                row.tenant_id,
                agent_id,
                "skill",
                row.id,
                "active" if row.status == "published" else "inactive",
                metadata_json=row.metadata_json,
            )

    @classmethod
    def copy(
        cls, db, tenant_id, source_agent_id, target_agent_id, sop_id, *, path=(), version=None
    ):
        from app.agents.branching import (
            agent_private_metadata,
            next_global_version,
            open_gallery_metadata,
        )

        source = cls.get(db, tenant_id, source_agent_id, sop_id)
        if version and source.version != version:
            source = next(
                (
                    r
                    for r in cls.versions(db, tenant_id, source_agent_id, sop_id)
                    if r.version == version
                ),
                None,
            )
            if source is None:
                raise HTTPException(404, "SOP version not found")
        if sop_id in path or len(path) >= 8:
            raise HTTPException(409, "INVALID_SOP_NESTING")
        if source.status != "published":
            raise HTTPException(400, "Disabled SOP cannot be learned")
        current = next(
            (r for r in cls.rows(db, tenant_id, target_agent_id) if r.skill_id == sop_id), None
        )
        content = deepcopy(source.content_json)
        for node in content.get("nodes", []):
            if node.get("type") == "subflow":
                child = cls.copy(
                    db,
                    tenant_id,
                    source_agent_id,
                    target_agent_id,
                    node["sub_sop_id"],
                    path=(*path, sop_id),
                    version=node.get("sub_sop_version"),
                )
                node["sub_sop_version"] = child.version
        if current and current.content_json != content:
            content["version"] = next_global_version(current.version)
            while any(
                v.version == content["version"]
                for v in cls.versions(db, tenant_id, target_agent_id, sop_id)
            ):
                content["version"] = next_global_version(content["version"])
        scope = cls.scope(db, tenant_id, target_agent_id)
        metadata = (
            agent_private_metadata(scope, source.metadata_json)
            if scope
            else open_gallery_metadata(source.metadata_json)
        )
        return cls.save(
            db,
            tenant_id,
            target_agent_id,
            content,
            status="published",
            metadata=metadata,
            commit=False,
        )

    @classmethod
    def copy_scope(cls, db, tenant_id, source_agent_id, target_agent_id):
        rows = cls.rows(db, tenant_id, source_agent_id, published_only=True)
        for source in rows:
            cls.copy(db, tenant_id, source_agent_id, target_agent_id, source.skill_id)

    @classmethod
    def rollback(cls, db, tenant_id, agent_id, sop_id, version):
        from app.agents.branching import next_global_version

        current = cls.get(db, tenant_id, agent_id, sop_id)
        selected = next(
            (r for r in cls.versions(db, tenant_id, agent_id, sop_id) if r.version == version), None
        )
        if selected is None:
            raise HTTPException(404, "SOP version not found")
        content = {
            **deepcopy(selected.content_json),
            "version": next_global_version(current.version),
        }
        return cls.save(db, tenant_id, agent_id, content, status=current.status)

    @classmethod
    def publish_row(cls, db, row, agent_id):
        from app.db.models import SopVersionRecord
        from app.skills.nesting import validate_sop_nesting

        validate_sop_nesting(row.skill_id, row.content_json, cls.rows(db, row.tenant_id, agent_id))
        version = db.exec(
            select(SopVersionRecord).where(
                SopVersionRecord.tenant_id == row.tenant_id,
                SopVersionRecord.agent_id == row.agent_id,
                SopVersionRecord.skill_id == row.skill_id,
                SopVersionRecord.version == row.version,
            )
        ).first()
        if version and version.content_json != row.content_json:
            raise HTTPException(409, "SOP_VERSION_CONFLICT")
        from app.config import get_settings
        from staffdeck_harness.composition.sources import validate_configuration
        from staffdeck_harness.contracts.sources import SourceContext
        from staffdeck_harness.modules.registry import get_registry

        validate_configuration(
            get_registry(get_settings()),
            db,
            SourceContext(row.tenant_id, agent_id),
            sop_id=row.skill_id,
        )
        cls.publish(
            db,
            row.tenant_id,
            agent_id,
            [
                SimpleNamespace(
                    skill_id=row.skill_id,
                    version=row.version,
                    name=row.name,
                    content=row.content_json,
                )
            ],
        )
        if not version:
            db.add(SopVersionRecord(**row.model_dump(exclude={"id"})))

    @classmethod
    def versions(cls, db, tenant_id, agent_id, sop_id):
        from app.db.models import SopVersionRecord

        return list(
            db.exec(
                select(SopVersionRecord)
                .where(
                    SopVersionRecord.tenant_id == tenant_id,
                    SopVersionRecord.agent_id == cls.scope(db, tenant_id, agent_id),
                    SopVersionRecord.skill_id == sop_id,
                )
                .order_by(SopVersionRecord.created_at.desc())
            ).all()
        )

    @staticmethod
    def normalize(content):
        from staffdeck_sop_core import SopDefinition
        from staffdeck_sop_core.graph import validate_graph

        value = deepcopy(content)
        definition = SopDefinition.from_value(value)
        validate_graph(definition.content)
        value["definition_schema"] = "sop.definition/v1"
        value["capability_requirements"] = definition.to_dict()["capability_requirements"]
        return value

    @staticmethod
    def snapshot(view, tenant_id, agent_id):
        from staffdeck_sop_core import SopDefinition

        content = deepcopy(dict(view.content))
        content = runtime_content(content)
        for node in content.get("nodes", []):
            if node.get("sub_sop_id"):
                node["sub_sop_id"] = runtime_sop_id(tenant_id, agent_id, node["sub_sop_id"])
        return SopDefinition.from_value(
            {
                "sop_id": runtime_sop_id(tenant_id, agent_id, view.skill_id),
                "version": view.version,
                "name": view.name,
                "content": content,
                "source": f"staffdeck:{tenant_id}:{agent_id or 'gallery'}",
            }
        ).to_dict()

    @classmethod
    def publish(cls, db, tenant_id, agent_id, views=None):
        endpoint = os.environ.get("SOP_RUNTIME_ENDPOINT")
        if not endpoint:
            if legacy_sop_writes_disabled():
                raise HTTPException(503, "SOP_RUNTIME_ENDPOINT_REQUIRED")
            return []
        if views is None:
            from app.agents.branching import visible_published_skills

            views = [
                SimpleNamespace(
                    skill_id=r.skill_id, version=r.version, name=r.name, content=r.content_json
                )
                for r in visible_published_skills(db, tenant_id, agent_id)
            ]
        from app.agents.branching import get_overall_agent, visible_published_skills

        catalog = {row.skill_id: row for row in visible_published_skills(db, tenant_id, agent_id)}
        scopes = [agent_id]
        if not cls.scope(db, tenant_id, agent_id):
            overall = get_overall_agent(db, tenant_id)
            scopes = list(dict.fromkeys([None, overall.id if overall else None]))
        client = RuntimeClient(endpoint, publish_token=os.environ.get("SOP_PUBLISH_TOKEN"))
        results = []
        for view in views:
            for scope in scopes:
                snapshot = cls.snapshot(view, tenant_id, scope)
                for node in snapshot["content"].get("nodes", []):
                    if node.get("type") == "subflow":
                        child_id = json.loads(node["sub_sop_id"])[-1]
                        child = catalog.get(child_id)
                        if child is None:
                            raise HTTPException(409, "SOP_CHILD_UNPUBLISHED")
                        node["sub_sop_version"] = node.get("sub_sop_version") or child.version
                response = client.request("/v1/definitions", snapshot)
                if scope == scopes[0]:
                    results.append(response)
        return results


def migrate_published(db, tenant_id, agent_id):
    """Read legacy definitions once, publish snapshots, then store new management rows."""
    from app.agents.branching import visible_skill_rows

    if legacy_sop_writes_disabled():
        raise HTTPException(409, "SOP_MIGRATION_REQUIRES_COMPAT_MODE")
    rows = visible_skill_rows(db, tenant_id, agent_id, include_inactive=True)
    views = [
        SimpleNamespace(skill_id=r.skill_id, version=r.version, name=r.name, content=r.content_json)
        for r in rows
        if r.status == "published"
    ]
    published = SopManagementService.publish(db, tenant_id, agent_id, views)
    from app.db.models import SopDefinitionRecord, SopVersionRecord

    for row in rows:
        content = SopManagementService.normalize(dict(row.content_json))
        for node in content.get("nodes", []):
            if node.get("type") == "subflow" and not node.get("sub_sop_version"):
                child = next(
                    (
                        r
                        for r in rows
                        if r.skill_id == node["sub_sop_id"] and r.status == "published"
                    ),
                    None,
                )
                if child is None:
                    raise HTTPException(409, "SOP_CHILD_UNPUBLISHED")
                node["sub_sop_version"] = child.version
        scope = SopManagementService.scope(db, tenant_id, agent_id)
        existing = next(
            (
                r
                for r in SopManagementService.rows(db, tenant_id, agent_id)
                if r.skill_id == row.skill_id
            ),
            None,
        )
        from staffdeck_harness.composition.staff import _sop_binding_row

        binding = _sop_binding_row(db, tenant_id, agent_id, row) if agent_id else None
        values = {
            "tenant_id": tenant_id,
            "agent_id": scope,
            "skill_id": row.skill_id,
            "version": row.version,
            "name": row.name,
            "business_domain": row.business_domain,
            "description": row.description,
            "content_json": content,
            "status": row.status,
            "metadata_json": deepcopy(dict(binding.metadata_json or {}) if binding else {}),
        }
        if existing is None:
            existing = SopDefinitionRecord(**values)
            db.add(existing)
        SopManagementService.bind(db, existing)
        if row.status == "published" and not any(
            v.version == row.version
            for v in SopManagementService.versions(db, tenant_id, agent_id, row.skill_id)
        ):
            db.add(SopVersionRecord(**values))
        db.flush()
        from app.agents.branching import branch_versions
        from app.db.models import SkillVersion

        history = (
            branch_versions(db, tenant_id, scope, row.skill_id)
            if scope
            else db.exec(
                select(SkillVersion).where(
                    SkillVersion.tenant_id == tenant_id, SkillVersion.skill_id == row.skill_id
                )
            ).all()
        )
        for previous in history:
            if any(
                v.version == previous.version
                for v in SopManagementService.versions(db, tenant_id, agent_id, row.skill_id)
            ):
                continue
            old_content = SopManagementService.normalize(dict(previous.content_json))
            db.add(
                SopVersionRecord(
                    **{
                        **values,
                        "version": previous.version,
                        "name": old_content.get("name", row.name),
                        "content_json": old_content,
                        "status": getattr(previous, "status", "published"),
                    }
                )
            )
            db.flush()
    from app.db.models import APISOPDraft, SopDraftRecord

    for draft in db.exec(
        select(APISOPDraft).where(
            APISOPDraft.tenant_id == tenant_id, APISOPDraft.agent_id == agent_id
        )
    ).all():
        if db.get(SopDraftRecord, draft.id) is None:
            value = draft.model_dump()
            value["content_json"] = SopManagementService.normalize(value["content_json"])
            from app.public_api.utils import etag_for

            value["etag"] = etag_for(value["content_json"])
            db.add(SopDraftRecord(**value))
    db.commit()
    return published


def normalize_sop_write(content):
    ensure_legacy_sop_writes_enabled()
    # Management remains usable until the independent package is installed for cutover.
    if not os.environ.get("SOP_RUNTIME_ENDPOINT"):
        return content
    return SopManagementService.normalize(content)


def runtime_content(content):
    # StaffDeck executes knowledge nodes through its own knowledge_search tool.
    for node in content.get("nodes", []):
        if node.get("type") == "knowledge_query":
            actions = list(node.get("allowed_actions") or [])
            node["allowed_actions"] = list(dict.fromkeys([*actions, "call_tool:knowledge_search"]))
            for requirement in content.get("capability_requirements", []):
                if (
                    requirement.get("node_id") == node.get("node_id")
                    and requirement.get("capability_id") == "knowledge_query"
                ):
                    requirement["capability_id"] = "knowledge_search"
    return content
