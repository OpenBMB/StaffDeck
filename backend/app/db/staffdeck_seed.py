from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from sqlmodel import Session, SQLModel, select

from app.agents.branching import (
    agent_private_metadata,
    ensure_open_gallery_binding,
    open_gallery_metadata,
)
from app.capability_scope import normalize_capability_scope
from app.db.models import AgentProfile, AgentResourceBinding, AgentSkillBranch, AgentSkillBranchVersion, GeneralSkill, Skill, SkillVersion, Tool, User, utc_now

TENANT_ID = "tenant_demo"
ADMIN_USER_ID = "admin"
ADMIN_USERNAME = "admin"
ADMIN_DISPLAY_NAME = "Administrator"
SEED_SOURCE = "staffdeck_admin_gallery_seed"
FIXTURE_PATH = Path(__file__).resolve().parent / "seed_fixtures" / "staffdeck_admin_gallery_seed.json"
EXPANDED_FIXTURE_PATH = (
    Path(__file__).resolve().parent / "seed_fixtures" / "staffdeck_expanded_gallery_seed.json"
)

SELECTED_AGENT_NAMES = {
    "IT",
    "人事",
    "法务",
    "行政",
    "财务",
    "销售",
    "市场",
    "采购",
    "项目管理",
    "数据分析",
}
USER_EDITABLE_AGENT_METADATA_KEYS = {
    "avatar_image",
    "avatar_kind",
    "avatar_preset",
    "avatar_text",
    "avatar_tone",
}

JsonDict = dict[str, Any]
ModelT = TypeVar("ModelT", bound=SQLModel)


def seed_staffdeck_admin_gallery(session: Session) -> None:
    """Seed the curated StaffDeck gallery package as admin-owned resources."""

    data = _load_seed_fixtures((FIXTURE_PATH, EXPANDED_FIXTURE_PATH))
    if not data:
        return
    id_maps: dict[str, dict[str, str]] = {
        "agent": {},
        "skill": {},
        "skill_business": {},
        "general_skill": {},
        "tool": {},
        "knowledge_base": {},
        "knowledge_base_version": {},
        "knowledge_document": {},
        "knowledge_bucket": {},
    }

    agents = [
        row for row in data.get("agent_profiles", []) if row.get("name") in SELECTED_AGENT_NAMES
    ]
    selected_agent_ids = {str(row.get("id") or "") for row in agents}
    active_bindings = [
        row
        for row in data.get("agent_resource_bindings", [])
        if row.get("status") == "active" and str(row.get("agent_id") or "") in selected_agent_ids
    ]
    resource_ids = _resource_ids_by_type(active_bindings)

    _seed_agents(session, agents, id_maps)
    _seed_skills(session, data.get("skills", []), data.get("skill_versions", []), resource_ids, id_maps)
    _seed_general_skills(session, data.get("general_skills", []), resource_ids, id_maps)
    _seed_tools(session, data.get("tools", []), resource_ids, id_maps)
    from app.knowledge.http_adapter import enabled as km_enabled
    if km_enabled():
        _seed_knowledge_http(session, data, resource_ids, id_maps)
    session.flush()
    _seed_agent_resource_bindings(session, active_bindings, id_maps)
    _seed_skill_branches(
        session,
        data.get("agent_skill_branches", []),
        data.get("agent_skill_branch_versions", []),
        id_maps,
    )
    session.flush()
    _publish_gallery_resources(session, id_maps)
    _sync_seed_agents_to_current_admin(session, id_maps)


def _seed_knowledge_http(session, data, resource_ids, id_maps):
    import base64
    import time
    from app.knowledge.http_adapter import StaffDeckKnowledgeAdapter

    admin = session.exec(select(User).where(
        User.tenant_id == TENANT_ID, User.username == ADMIN_USERNAME,
    )).one()
    gallery = StaffDeckKnowledgeAdapter(session, TENANT_ID, None, admin, write=True)
    gallery.ensure_space()
    existing = {row["name"]: row for row in gallery.call("GET", "/knowledge-bases")}
    for row in data.get("knowledge_bases", []):
        if row["id"] not in resource_ids.get("knowledge_base", set()):
            continue
        base = existing.get(row["name"])
        if base is None:
            base = gallery.call("POST", "/knowledge-bases", {
                "space_id": gallery.space_id, "name": row["name"],
                "description": row.get("description"),
                "capability_scope": normalize_capability_scope(row.get("capability_scope")),
                "metadata": {"seed_source": SEED_SOURCE, "source_resource_id": row["id"]},
            }, scoped=False)
            existing[row["name"]] = base
        id_maps["knowledge_base"][row["id"]] = base["id"]
        docs = gallery.call("GET", f"/knowledge-bases/{base['id']}/documents")
        jobs = gallery.call("GET", "/jobs?limit=200")
        sources = {(item.get("metadata", {}).get("source_document_id")) for item in docs}
        sources.update(item.get("metadata", {}).get("metadata", {}).get("source_document_id")
                       for item in jobs if item["status"] not in {"failed", "cancelled"})
        for document in data.get("knowledge_documents", []):
            if document["knowledge_base_id"] != row["id"] or document["id"] in sources:
                continue
            content = (document.get("metadata_json") or {}).get("raw_text")
            if not content:
                content = "\n\n".join(item.get("content", "") for item in data.get("knowledge_chunks", [])
                                     if item["document_id"] == document["id"])
            if content:
                job = gallery.call("POST", f"/knowledge-bases/{base['id']}/documents", {
                    "space_id": gallery.space_id, "filename": document.get("filename") or "seed.md",
                    "content_base64": base64.b64encode(content.encode()).decode(),
                    "title": document.get("title"),
                    "metadata": {"source_document_id": document["id"], "seed_source": SEED_SOURCE},
                }, scoped=False)
                for _ in range(1200):
                    job = gallery.call("GET", f"/jobs/{job['id']}")
                    if job["status"] in {"succeeded", "failed", "cancelled"}:
                        break
                    time.sleep(.1)
                if job["status"] != "succeeded":
                    raise ValueError(f"Knowledge seed ingestion failed: {job['id']}")
        gallery.call("POST", f"/knowledge-bases/{base['id']}/versions/publish", {})


def _load_seed_fixtures(paths: Iterable[Path]) -> JsonDict:
    merged: JsonDict = {}
    for path in paths:
        fixture = json.loads(path.read_text(encoding="utf-8"))
        for key, value in fixture.items():
            if isinstance(value, list):
                merged.setdefault(key, []).extend(value)
            elif key not in merged:
                merged[key] = value
    return merged


def _seed_agents(session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]) -> None:
    for row in rows:
        source_id = str(row.get("id") or "")
        name = str(row.get("name") or "").strip()
        if not source_id or not name:
            continue
        existing_by_id = session.get(AgentProfile, source_id)
        existing_by_name = session.exec(
            select(AgentProfile).where(AgentProfile.tenant_id == TENANT_ID, AgentProfile.name == name)
        ).first()
        existing = _seed_update_target(existing_by_id, existing_by_name, source_id)
        metadata = _agent_metadata(row.get("metadata_json"))
        if existing:
            existing_metadata = existing.metadata_json or {}
            metadata.update(
                {
                    key: existing_metadata[key]
                    for key in USER_EDITABLE_AGENT_METADATA_KEYS
                    if key in existing_metadata
                }
            )
        payload = {
            "tenant_id": TENANT_ID,
            "name": name,
            "description": row.get("description"),
            "persona_prompt": row.get("persona_prompt"),
            "is_overall": bool(row.get("is_overall", False)),
            "status": row.get("status") or "active",
            "metadata_json": metadata,
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
            id_maps["agent"][source_id] = existing.id
        elif existing_by_id or existing_by_name:
            continue
        else:
            agent = AgentProfile(id=source_id, **payload)
            session.add(agent)
            id_maps["agent"][source_id] = source_id


def _seed_skills(
    session: Session,
    skill_rows: Iterable[JsonDict],
    version_rows: Iterable[JsonDict],
    resource_ids: dict[str, set[str]],
    id_maps: dict[str, dict[str, str]],
) -> None:
    selected_ids = resource_ids.get("skill", set())
    selected_skill_ids: set[str] = set()
    for row in skill_rows:
        source_id = str(row.get("id") or "")
        if source_id not in selected_ids:
            continue
        skill_id = str(row.get("skill_id") or "").strip()
        if not skill_id:
            continue
        existing_by_id = session.get(Skill, source_id)
        existing_by_skill_id = session.exec(
            select(Skill).where(Skill.tenant_id == TENANT_ID, Skill.skill_id == skill_id)
        ).first()
        existing = _seed_update_target(existing_by_id, existing_by_skill_id, source_id)
        content = _json_object(row.get("content_json"))
        _apply_curated_skill_runtime_defaults(skill_id, content)
        content.update({"skill_id": skill_id, "name": row.get("name"), "version": row.get("version") or "1.0.0"})
        payload = {
            "tenant_id": TENANT_ID,
            "skill_id": skill_id,
            "version": row.get("version") or "1.0.0",
            "name": row.get("name") or skill_id,
            "business_domain": row.get("business_domain"),
            "description": row.get("description"),
            "content_json": content,
            "status": "published",
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
            target_id = existing.id
        elif existing_by_id or existing_by_skill_id:
            continue
        else:
            skill = Skill(id=source_id, **payload)
            session.add(skill)
            target_id = source_id
        id_maps["skill"][source_id] = target_id
        id_maps["skill_business"][skill_id] = skill_id
        selected_skill_ids.add(skill_id)
    session.flush()

    for row in version_rows:
        skill_id = str(row.get("skill_id") or "").strip()
        if skill_id not in selected_skill_ids:
            continue
        version = str(row.get("version") or "1.0.0")
        source_id = str(row.get("id") or "")
        existing_by_id = session.get(SkillVersion, source_id)
        existing_by_version = session.exec(
            select(SkillVersion).where(
                SkillVersion.tenant_id == TENANT_ID,
                SkillVersion.skill_id == skill_id,
                SkillVersion.version == version,
            )
        ).first()
        existing = _seed_update_target(existing_by_id, existing_by_version, source_id)
        content = _json_object(row.get("content_json"))
        _apply_curated_skill_runtime_defaults(skill_id, content)
        content.update({"skill_id": skill_id, "name": row.get("name"), "version": version})
        payload = {
            "tenant_id": TENANT_ID,
            "skill_id": skill_id,
            "version": version,
            "name": row.get("name") or skill_id,
            "business_domain": row.get("business_domain"),
            "description": row.get("description"),
            "content_json": content,
            "status": "published",
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
        elif existing_by_id or existing_by_version:
            continue
        else:
            session.add(SkillVersion(id=source_id, **payload))


def _seed_general_skills(
    session: Session,
    rows: Iterable[JsonDict],
    resource_ids: dict[str, set[str]],
    id_maps: dict[str, dict[str, str]],
) -> None:
    selected_ids = resource_ids.get("general_skill", set())
    for row in rows:
        source_id = str(row.get("id") or "")
        if source_id not in selected_ids:
            continue
        slug = str(row.get("slug") or "").strip()
        if not slug:
            continue
        existing_by_id = session.get(GeneralSkill, source_id)
        existing_by_slug = session.exec(
            select(GeneralSkill).where(GeneralSkill.tenant_id == TENANT_ID, GeneralSkill.slug == slug)
        ).first()
        existing = _seed_update_target(existing_by_id, existing_by_slug, source_id)
        payload = {
            "tenant_id": TENANT_ID,
            "slug": slug,
            "name": row.get("name") or slug,
            "description": row.get("description"),
            "homepage": row.get("homepage"),
            "skill_markdown": row.get("skill_markdown") or "",
            "skill_files_json": _json_list(row.get("skill_files_json")),
            "metadata_json": _open_gallery_seed_metadata(row.get("metadata_json")),
            "status": "published",
            "capability_scope": normalize_capability_scope(
                row.get("capability_scope")
                if row.get("capability_scope") is not None
                else getattr(existing, "capability_scope", None)
            ),
            "permissions_json": _json_object(row.get("permissions_json")),
            "runtime_config_json": _json_object(row.get("runtime_config_json")),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
            id_maps["general_skill"][source_id] = existing.id
        elif existing_by_id or existing_by_slug:
            continue
        else:
            session.add(GeneralSkill(id=source_id, **payload))
            id_maps["general_skill"][source_id] = source_id


def _seed_tools(
    session: Session,
    rows: Iterable[JsonDict],
    resource_ids: dict[str, set[str]],
    id_maps: dict[str, dict[str, str]],
) -> None:
    selected_ids = resource_ids.get("tool", set())
    for row in rows:
        source_id = str(row.get("id") or "")
        if source_id not in selected_ids:
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        existing_by_id = session.get(Tool, source_id)
        existing_by_name = session.exec(
            select(Tool).where(Tool.tenant_id == TENANT_ID, Tool.name == name)
        ).first()
        existing = _seed_update_target(existing_by_id, existing_by_name, source_id)
        config = _json_object(row.get("config_json"))
        if name == "contract.archive_query":
            config = {**config, "execution": {"timeout_seconds": 20}}
        payload = {
            "tenant_id": TENANT_ID,
            "name": name,
            "display_name": row.get("display_name"),
            "description": row.get("description"),
            "bucket": row.get("bucket") or "未分桶",
            "tool_type": row.get("tool_type") or "http",
            "method": row.get("method") or "POST",
            "url": row.get("url") or "",
            "headers_json": _json_object(row.get("headers_json")),
            "auth_json": _json_object(row.get("auth_json")),
            "config_json": config,
            "input_schema": _json_object(row.get("input_schema")),
            "output_schema": _json_object(row.get("output_schema")),
            "allowed_skills_json": _json_list(row.get("allowed_skills_json")),
            "mcp_server_id": row.get("mcp_server_id"),
            "capability_scope": normalize_capability_scope(
                row.get("capability_scope")
                if row.get("capability_scope") is not None
                else getattr(existing, "capability_scope", None)
            ),
            "capability_scope_inherited": bool(
                row.get("capability_scope_inherited")
                if row.get("capability_scope_inherited") is not None
                else getattr(existing, "capability_scope_inherited", True)
            ),
            "enabled": bool(row.get("enabled", True)),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
            id_maps["tool"][source_id] = existing.id
        elif existing_by_id or existing_by_name:
            continue
        else:
            session.add(Tool(id=source_id, **payload))
            id_maps["tool"][source_id] = source_id


def _apply_curated_skill_runtime_defaults(skill_id: str, content: JsonDict) -> None:
    if skill_id != "leave_apply_v1":
        return
    nodes = content.get("nodes")
    if not isinstance(nodes, list):
        return
    for node in nodes:
        if not isinstance(node, dict) or node.get("node_id") != "check_policy":
            continue
        scope = _json_object(node.get("knowledge_scope"))
        scope.setdefault("query_fields", ["leave_type"])
        node["knowledge_scope"] = scope


















def _seed_agent_resource_bindings(
    session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]
) -> None:
    for row in rows:
        agent_id = id_maps["agent"].get(str(row.get("agent_id") or ""))
        resource_type = str(row.get("resource_type") or "")
        resource_id = _mapped_resource_id(resource_type, str(row.get("resource_id") or ""), id_maps)
        if not agent_id or not resource_id:
            continue
        from app.knowledge.http_adapter import StaffDeckKnowledgeAdapter, enabled as km_enabled
        if resource_type == "knowledge_base" and km_enabled():
            admin = session.exec(select(User).where(
                User.tenant_id == TENANT_ID, User.username == ADMIN_USERNAME,
            )).one()
            source = StaffDeckKnowledgeAdapter(session, TENANT_ID, None, admin)
            target = StaffDeckKnowledgeAdapter(session, TENANT_ID, agent_id, admin, write=True)
            target.ensure_space()
            source.call("POST", f"/knowledge-bases/{resource_id}/bindings", {"space_id": target.space_id})
            continue
        metadata = agent_private_metadata(agent_id, _seed_metadata(row.get("metadata_json")))
        existing = session.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == TENANT_ID,
                AgentResourceBinding.agent_id == agent_id,
                AgentResourceBinding.resource_type == resource_type,
                AgentResourceBinding.resource_id == resource_id,
            )
        ).first()
        payload = {
            "tenant_id": TENANT_ID,
            "agent_id": agent_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "status": "active",
            "metadata_json": metadata,
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
        else:
            session.add(AgentResourceBinding(id=str(row.get("id") or ""), **payload))


def _seed_skill_branches(
    session: Session,
    branch_rows: Iterable[JsonDict],
    version_rows: Iterable[JsonDict],
    id_maps: dict[str, dict[str, str]],
) -> None:
    branch_keys: set[tuple[str, str]] = set()
    for row in branch_rows:
        agent_id = id_maps["agent"].get(str(row.get("agent_id") or ""))
        skill_id = str(row.get("skill_id") or "")
        if not agent_id or skill_id not in id_maps["skill_business"]:
            continue
        branch_keys.add((agent_id, skill_id))
        existing = session.get(AgentSkillBranch, str(row.get("id") or "")) or session.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == TENANT_ID,
                AgentSkillBranch.agent_id == agent_id,
                AgentSkillBranch.skill_id == skill_id,
            )
        ).first()
        payload = {
            "tenant_id": TENANT_ID,
            "agent_id": agent_id,
            "skill_id": skill_id,
            "source_skill_id": row.get("source_skill_id") or skill_id,
            "base_version": row.get("base_version") or "1.0.0",
            "head_version": row.get("head_version") or row.get("base_version") or "1.0.0",
            "content_json": _json_object(row.get("content_json")),
            "status": row.get("status") or "active",
            "sync_state": row.get("sync_state") or "synced",
            "metadata_json": agent_private_metadata(agent_id, _seed_metadata(row.get("metadata_json"))),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
        else:
            session.add(AgentSkillBranch(id=str(row.get("id") or ""), **payload))
    session.flush()

    for row in version_rows:
        agent_id = id_maps["agent"].get(str(row.get("agent_id") or ""))
        skill_id = str(row.get("skill_id") or "")
        version = str(row.get("version") or "1.0.0")
        if not agent_id or (agent_id, skill_id) not in branch_keys:
            continue
        existing = session.get(AgentSkillBranchVersion, str(row.get("id") or "")) or session.exec(
            select(AgentSkillBranchVersion).where(
                AgentSkillBranchVersion.tenant_id == TENANT_ID,
                AgentSkillBranchVersion.agent_id == agent_id,
                AgentSkillBranchVersion.skill_id == skill_id,
                AgentSkillBranchVersion.version == version,
            )
        ).first()
        payload = {
            "tenant_id": TENANT_ID,
            "agent_id": agent_id,
            "skill_id": skill_id,
            "source_skill_id": row.get("source_skill_id") or skill_id,
            "version": version,
            "base_version": row.get("base_version") or "1.0.0",
            "content_json": _json_object(row.get("content_json")),
            "status": row.get("status") or "active",
            "sync_state": row.get("sync_state") or "synced",
            "change_summary": row.get("change_summary"),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
        else:
            session.add(AgentSkillBranchVersion(id=str(row.get("id") or ""), **payload))






def _publish_gallery_resources(session: Session, id_maps: dict[str, dict[str, str]]) -> None:
    metadata = _open_gallery_seed_metadata({})
    for resource_type, model, map_key in (
        ("skill", Skill, "skill"),
        ("general_skill", GeneralSkill, "general_skill"),
        ("tool", Tool, "tool"),
    ):
        from app.knowledge.http_adapter import enabled as km_enabled
        if resource_type == "knowledge_base" and km_enabled():
            continue
        for resource_id in set(id_maps[map_key].values()):
            resource = session.get(model, resource_id)
            if resource is None:
                continue
            if hasattr(resource, "metadata_json"):
                resource.metadata_json = _open_gallery_seed_metadata(getattr(resource, "metadata_json", None))
                resource.updated_at = utc_now()
                session.add(resource)
            ensure_open_gallery_binding(
                session,
                TENANT_ID,
                resource_type,
                resource_id,
                "active",
                metadata_json=metadata,
            )


def _sync_seed_agents_to_current_admin(
    session: Session, id_maps: dict[str, dict[str, str]]
) -> None:
    admin = session.exec(
        select(User).where(User.tenant_id == TENANT_ID, User.username == ADMIN_USERNAME)
    ).first()
    if not admin:
        return
    admin_metadata = {
        "owner_user_id": admin.id,
        "owner_username": admin.username,
        "owner_display_name": admin.display_name or ADMIN_DISPLAY_NAME,
        "created_by_user_id": admin.id,
        "created_by_username": admin.username,
        "created_by": admin.username,
        "created_by_display_name": admin.display_name or ADMIN_DISPLAY_NAME,
        "creator_name": admin.username,
    }
    for agent_id in set(id_maps["agent"].values()):
        agent = session.get(AgentProfile, agent_id)
        if agent is None:
            continue
        metadata = dict(agent.metadata_json or {})
        metadata.update(admin_metadata)
        metadata["published_to_gallery"] = True
        metadata["gallery_published_by"] = admin.username
        metadata["seed_source"] = SEED_SOURCE
        metadata["managed_by_seed"] = True
        agent.metadata_json = metadata
        agent.updated_at = utc_now()
        session.add(agent)


def _resource_ids_by_type(rows: Iterable[JsonDict]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for row in rows:
        resource_type = str(row.get("resource_type") or "")
        resource_id = str(row.get("resource_id") or "")
        if resource_type and resource_id:
            result.setdefault(resource_type, set()).add(resource_id)
    return result


def _mapped_resource_id(
    resource_type: str, source_id: str, id_maps: dict[str, dict[str, str]]
) -> str | None:
    map_key = {
        "skill": "skill",
        "general_skill": "general_skill",
        "tool": "tool",
        "knowledge_base": "knowledge_base",
    }.get(resource_type)
    if map_key is None:
        return None
    return id_maps[map_key].get(source_id)


def _agent_metadata(value: Any) -> JsonDict:
    metadata = _seed_metadata(value)
    metadata.update(
        {
            "published_to_gallery": True,
            "gallery_published_by": ADMIN_USERNAME,
            "seed_source": SEED_SOURCE,
            "managed_by_seed": True,
        }
    )
    metadata.pop("scope", None)
    metadata.pop("visibility", None)
    metadata.pop("owner_agent_id", None)
    metadata.pop("created_from_agent", None)
    return metadata


def _open_gallery_seed_metadata(value: Any) -> JsonDict:
    metadata = open_gallery_metadata(_seed_metadata(value))
    metadata["seed_source"] = SEED_SOURCE
    metadata["managed_by_seed"] = True
    return metadata


def _seed_metadata(value: Any) -> JsonDict:
    metadata = _json_object(value)
    metadata.update(
        {
            "owner_user_id": ADMIN_USER_ID,
            "owner_username": ADMIN_USERNAME,
            "owner_display_name": ADMIN_DISPLAY_NAME,
            "created_by_user_id": ADMIN_USER_ID,
            "created_by_username": ADMIN_USERNAME,
            "created_by": ADMIN_USERNAME,
            "created_by_display_name": ADMIN_DISPLAY_NAME,
            "creator_name": ADMIN_USERNAME,
        }
    )
    return metadata


def _seed_update_target(
    existing_by_id: ModelT | None,
    existing_by_key: ModelT | None,
    source_id: str,
) -> ModelT | None:
    if (
        existing_by_id is not None
        and existing_by_key is not None
        and getattr(existing_by_id, "id", None) != getattr(existing_by_key, "id", None)
    ):
        return existing_by_key if _is_seed_managed(existing_by_key, source_id) else None
    for candidate in (existing_by_id, existing_by_key):
        if candidate is not None and _is_seed_managed(candidate, source_id):
            return candidate
    return None


def _is_seed_managed(row: object, source_id: str) -> bool:
    if getattr(row, "id", None) == source_id:
        return True
    metadata = getattr(row, "metadata_json", None) or {}
    if not isinstance(metadata, dict):
        return False
    return metadata.get("seed_source") == SEED_SOURCE or metadata.get("managed_by_seed") is True


def _json_object(value: Any) -> JsonDict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return list(parsed) if isinstance(parsed, list) else []
    return []


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _apply_payload(row: Any, payload: JsonDict) -> None:
    for key, value in payload.items():
        setattr(row, key, value)
