from __future__ import annotations

import json
import queue
import re
import threading
import time
import zipfile
import base64
import binascii
from collections.abc import Iterator
from html import unescape
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlparse
from urllib.request import Request, urlopen

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select

from app.agents.branching import (
    ensure_open_gallery_binding,
    ensure_private_resource_binding,
    get_agent,
    hide_open_gallery_binding,
    is_bound_resource_visible_for_agent,
    is_open_gallery_resource,
    mark_resource_open_gallery,
    mark_resource_private_for_agent,
    metadata_preserving_creator,
    require_overall_agent,
    user_creator_metadata,
)
from app.db import get_session
from app.db.models import AgentResourceBinding, GeneralSkill, ModelConfig, User, utc_now
from app.general_skills import (
    GeneralSkillClawHubImportRequest,
    GeneralSkillImportRequest,
    GeneralSkillPackageUploadRequest,
    GeneralSkillRead,
    GeneralSkillRunRequest,
    GeneralSkillRunResponse,
)
from app.general_skills.schema import GeneralSkillFile
from app.general_skills.runner import GeneralSkillRunner
from app.llm.model_config_resolver import resolve_model_config_for_runtime
from app.security.auth import get_current_user
from app.security.permissions import (
    ensure_agent_scope_manager,
    ensure_open_gallery_admin,
    require_agent_scope_viewer,
)
from app.security.tenant import ensure_tenant

router = APIRouter(
    prefix="/api/enterprise/general-skills",
    tags=["enterprise:general-skills"],
    dependencies=[Depends(get_current_user)],
)

MAX_CLAWHUB_PACKAGE_BYTES = 96 * 1024 * 1024
MAX_CLAWHUB_FILE_BYTES = 2 * 1024 * 1024
MAX_CLAWHUB_FILES = 240
REMOTE_SKILL_DOWNLOAD_TIMEOUT_SECONDS = 120
GENERAL_SKILL_STREAM_IDLE_TIMEOUT_SECONDS = 120
GITHUB_HOSTS = {"github.com", "www.github.com"}
RAW_GITHUB_HOST = "raw.githubusercontent.com"
CLAWHUB_HOSTS = {"clawhub.ai", "www.clawhub.ai"}
SKILLHUB_HOSTS = {"skillhub.ai", "www.skillhub.ai"}
REMOTE_SKILLHUB_HOSTS = CLAWHUB_HOSTS | SKILLHUB_HOSTS
CLAWHUB_DOWNLOAD_ENDPOINT = "https://wry-manatee-359.convex.site/api/v1/download"


def _agent_id_or_none(agent_id: object | None) -> str | None:
    return agent_id if isinstance(agent_id, str) and agent_id else None


def general_skill_read(row: GeneralSkill, status_override: str | None = None) -> GeneralSkillRead:
    return GeneralSkillRead(
        id=row.id,
        tenant_id=row.tenant_id,
        slug=row.slug,
        name=row.name,
        description=row.description,
        homepage=row.homepage,
        skill_markdown=row.skill_markdown,
        skill_files=[
            GeneralSkillFile.model_validate(item) for item in _skill_files_or_markdown(row)
        ],
        metadata=dict(row.metadata_json or {}),
        status=status_override or row.status,
        permissions=row.permissions_json or {},
        runtime_config=row.runtime_config_json or {},
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


@router.post("/import", response_model=GeneralSkillRead)
def import_general_skill(
    request: GeneralSkillImportRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> GeneralSkillRead:
    ensure_tenant(db, request.tenant_id)
    files = _normalize_skill_files(request.files, request.markdown)
    markdown = _skill_markdown_from_files(files)
    parsed_metadata = _parse_skill_metadata(markdown)
    metadata = user_creator_metadata(current_user, parsed_metadata)
    name = (
        _optional_text(request.name)
        or _metadata_text(metadata, "name", "title")
        or "未命名通用技能"
    )
    slug = _optional_text(request.slug) or _metadata_text(metadata, "slug", "id") or _slugify(name)
    description = _optional_text(request.description) or _metadata_text(
        metadata, "description", "summary"
    )
    homepage = _optional_text(request.homepage) or _metadata_text(
        metadata, "homepage", "url", "source"
    )
    _validate_slug(slug)
    lookup_slug = _optional_text(request.original_slug)
    agent_id = _agent_id_or_none(request.agent_id)
    agent = ensure_agent_scope_manager(db, request.tenant_id, agent_id, current_user)
    is_private_agent_scope = bool(agent and not agent.is_overall)
    if not is_private_agent_scope:
        ensure_open_gallery_admin(request.tenant_id, current_user)
    row = None
    if lookup_slug:
        row = db.exec(
            select(GeneralSkill).where(
                GeneralSkill.tenant_id == request.tenant_id,
                GeneralSkill.slug == lookup_slug,
            )
        ).first()
        if not row:
            raise HTTPException(status_code=404, detail="General skill to update was not found")
        if slug != row.slug:
            raise HTTPException(status_code=400, detail="General skill slug cannot be modified")
        if is_private_agent_scope:
            if is_open_gallery_resource(db, request.tenant_id, "general_skill", row):
                row = None
                slug = _unique_slug(db, request.tenant_id, slug)
            elif not _general_skill_editable_by_agent(db, request.tenant_id, agent.id, row):
                raise HTTPException(
                    status_code=404, detail="General skill not visible to this agent"
                )
    else:
        conflict = db.exec(
            select(GeneralSkill).where(
                GeneralSkill.tenant_id == request.tenant_id,
                GeneralSkill.slug == slug,
            )
        ).first()
        if conflict:
            if is_private_agent_scope and is_open_gallery_resource(
                db,
                request.tenant_id,
                "general_skill",
                conflict,
            ):
                slug = _unique_slug(db, request.tenant_id, slug)
            else:
                raise HTTPException(status_code=409, detail="General skill slug already exists")
    now = utc_now()
    if row:
        metadata = metadata_preserving_creator(row.metadata_json, parsed_metadata)
        if slug != row.slug:
            conflict = db.exec(
                select(GeneralSkill).where(
                    GeneralSkill.tenant_id == request.tenant_id,
                    GeneralSkill.slug == slug,
                )
            ).first()
            if conflict:
                if is_private_agent_scope and is_open_gallery_resource(
                    db,
                    request.tenant_id,
                    "general_skill",
                    conflict,
                ):
                    slug = _unique_slug(db, request.tenant_id, slug)
                else:
                    raise HTTPException(status_code=409, detail="General skill slug already exists")
        row.slug = slug
        row.name = name
        row.description = description
        row.homepage = homepage
        row.skill_markdown = markdown
        row.skill_files_json = [file.model_dump(mode="json") for file in files]
        row.metadata_json = metadata
        row.status = request.status
        row.updated_at = now
    else:
        row = GeneralSkill(
            tenant_id=request.tenant_id,
            slug=slug,
            name=name,
            description=description,
            homepage=homepage,
            skill_markdown=markdown,
            skill_files_json=[file.model_dump(mode="json") for file in files],
            metadata_json=metadata,
            status=request.status,
            permissions_json={"network": True, "python": True},
            runtime_config_json={"runtime": "python", "timeout_seconds": 12},
            created_at=now,
            updated_at=now,
        )
    if is_private_agent_scope:
        mark_resource_private_for_agent(row, agent.id, metadata)
    else:
        mark_resource_open_gallery(row, metadata)
    db.add(row)
    db.flush()
    if is_private_agent_scope:
        ensure_private_resource_binding(
            db,
            request.tenant_id,
            agent.id,
            "general_skill",
            row.id,
            "active" if request.status == "published" else "inactive",
            metadata_json=metadata,
        )
    else:
        ensure_open_gallery_binding(
            db,
            request.tenant_id,
            "general_skill",
            row.id,
            "active" if request.status == "published" else "inactive",
            metadata_json=metadata,
        )
    db.commit()
    db.refresh(row)
    return general_skill_read(row)


@router.post("/import-skillhub", response_model=GeneralSkillRead)
def import_skillhub_skill(
    request: GeneralSkillClawHubImportRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> GeneralSkillRead:
    ensure_tenant(db, request.tenant_id)
    raw_files = _load_clawhub_source(request.source)
    files = _normalize_skill_files(raw_files, None)
    return _create_imported_general_skill(
        db,
        tenant_id=request.tenant_id,
        files=files,
        import_source=request.source,
        agent_id=request.agent_id,
        status=request.status,
        name=request.name,
        slug=request.slug,
        description=request.description,
        homepage=request.homepage,
        current_user=current_user,
    )


@router.post("/import-clawhub", response_model=GeneralSkillRead)
def import_clawhub_skill(
    request: GeneralSkillClawHubImportRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> GeneralSkillRead:
    return import_skillhub_skill(request, db, current_user)


@router.post("/import-package", response_model=GeneralSkillRead)
def import_general_skill_package(
    request: GeneralSkillPackageUploadRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> GeneralSkillRead:
    ensure_tenant(db, request.tenant_id)
    filename = _clean_source_filename(request.filename)
    data = _decode_base64_payload(request.content_base64)
    if filename.lower().endswith(".zip"):
        raw_files = _files_from_zip(data)
    elif filename.lower().endswith((".md", ".markdown", ".txt")):
        text = _decode_text(data)
        raw_files = [
            GeneralSkillFile(
                path="SKILL.md",
                content=text,
                size=len(data),
                mime_type=_guess_mime_type(filename),
            )
        ]
    else:
        raise HTTPException(
            status_code=400, detail="Uploaded skill package must be a .zip or Markdown file"
        )
    files = _normalize_skill_files(raw_files, None)
    return _create_imported_general_skill(
        db,
        tenant_id=request.tenant_id,
        files=files,
        import_source=f"upload:{filename}",
        agent_id=request.agent_id,
        status=request.status,
        name=request.name,
        slug=request.slug,
        description=request.description,
        homepage=request.homepage,
        current_user=current_user,
    )


def _create_imported_general_skill(
    db: Session,
    *,
    tenant_id: str,
    files: list[GeneralSkillFile],
    import_source: str,
    agent_id: str | None,
    status: str,
    name: str | None = None,
    slug: str | None = None,
    description: str | None = None,
    homepage: str | None = None,
    current_user: object | None = None,
) -> GeneralSkillRead:
    markdown = _skill_markdown_from_files(files)
    metadata = _parse_skill_metadata(markdown)
    resolved_name = (
        _optional_text(name)
        or _metadata_text(metadata, "name", "title")
        or _source_name(import_source)
    )
    source_slug = _clawhub_slug_from_source(import_source)
    slug_base = (
        _optional_text(slug)
        or _metadata_text(metadata, "slug", "id")
        or source_slug
        or _slugify(resolved_name)
    )
    resolved_slug = _unique_slug(db, tenant_id, slug_base)
    resolved_description = _optional_text(description) or _metadata_text(
        metadata, "description", "summary"
    )
    resolved_homepage = (
        _optional_text(homepage)
        or _metadata_text(metadata, "homepage", "url", "source")
        or _clawhub_homepage_from_source(import_source)
    )
    _validate_slug(resolved_slug)
    now = utc_now()
    resolved_agent_id = _agent_id_or_none(agent_id)
    agent = ensure_agent_scope_manager(db, tenant_id, resolved_agent_id, current_user)
    row = GeneralSkill(
        tenant_id=tenant_id,
        slug=resolved_slug,
        name=resolved_name,
        description=resolved_description,
        homepage=resolved_homepage,
        skill_markdown=markdown,
        skill_files_json=[file.model_dump(mode="json") for file in files],
        metadata_json=user_creator_metadata(
            current_user, {**metadata, "import_source": import_source}
        ),
        status=status,
        permissions_json={"network": True, "python": True},
        runtime_config_json={"runtime": "python", "timeout_seconds": 12},
        created_at=now,
        updated_at=now,
    )
    if not (agent and not agent.is_overall):
        ensure_open_gallery_admin(tenant_id, current_user)
    if agent and not agent.is_overall:
        mark_resource_private_for_agent(row, agent.id, row.metadata_json or {})
    else:
        mark_resource_open_gallery(row, row.metadata_json or {})
    db.add(row)
    db.flush()
    if agent and not agent.is_overall:
        ensure_private_resource_binding(
            db,
            tenant_id,
            agent.id,
            "general_skill",
            row.id,
            "active" if status == "published" else "inactive",
            metadata_json=row.metadata_json or {},
        )
    else:
        ensure_open_gallery_binding(
            db,
            tenant_id,
            "general_skill",
            row.id,
            "active" if status == "published" else "inactive",
            metadata_json=row.metadata_json or {},
        )
    db.commit()
    db.refresh(row)
    return general_skill_read(row)


@router.get(
    "", response_model=list[GeneralSkillRead], dependencies=[Depends(require_agent_scope_viewer)]
)
def list_general_skills(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    agent_id: str | None = Query(None),
) -> list[GeneralSkillRead]:
    ensure_tenant(db, tenant_id)
    agent_id = _agent_id_or_none(agent_id)
    agent = get_agent(db, tenant_id, agent_id)
    if agent and not agent.is_overall:
        bindings = db.exec(
            select(AgentResourceBinding)
            .where(
                AgentResourceBinding.tenant_id == tenant_id,
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_type == "general_skill",
            )
            .order_by(AgentResourceBinding.updated_at.desc())
        ).all()
        if not bindings:
            return []
        rows_by_id = {
            row.id: row
            for row in db.exec(
                select(GeneralSkill).where(
                    GeneralSkill.tenant_id == tenant_id,
                    GeneralSkill.id.in_([binding.resource_id for binding in bindings]),
                )
            ).all()
        }
        visible_rows: list[GeneralSkillRead] = []
        for binding in bindings:
            row = rows_by_id.get(binding.resource_id)
            if not row:
                continue
            if not is_bound_resource_visible_for_agent(
                db, tenant_id, "general_skill", row, binding
            ):
                continue
            visible_rows.append(
                general_skill_read(
                    row,
                    status_override=(
                        "published"
                        if binding.status == "active" and row.status == "published"
                        else "archived"
                    ),
                )
            )
        return visible_rows
    rows = db.exec(
        select(GeneralSkill)
        .where(GeneralSkill.tenant_id == tenant_id)
        .order_by(GeneralSkill.updated_at.desc())
    ).all()
    rows = [row for row in rows if is_open_gallery_resource(db, tenant_id, "general_skill", row)]
    return [general_skill_read(row) for row in rows]


@router.get(
    "/{slug}", response_model=GeneralSkillRead, dependencies=[Depends(require_agent_scope_viewer)]
)
def get_general_skill(
    slug: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    agent_id: str | None = Query(None),
) -> GeneralSkillRead:
    row = _get_general_skill(db, tenant_id, slug)
    _ensure_general_skill_visible(db, tenant_id, row, agent_id)
    return general_skill_read(row)


@router.post("/{slug}/publish", response_model=GeneralSkillRead)
def publish_general_skill(
    slug: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    agent_id: str | None = Query(None),
    current_user: User = Depends(get_current_user),
) -> GeneralSkillRead:
    row = _get_general_skill(db, tenant_id, slug)
    agent_id = _agent_id_or_none(agent_id)
    agent = ensure_agent_scope_manager(db, tenant_id, agent_id, current_user)
    if agent and not agent.is_overall:
        binding = _ensure_general_skill_binding(
            db,
            tenant_id,
            agent.id,
            row.id,
            metadata_json=row.metadata_json or {},
        )
        binding.status = "active"
        binding.updated_at = utc_now()
        db.add(binding)
        db.commit()
        return general_skill_read(row, status_override="published")
    ensure_open_gallery_admin(tenant_id, current_user)
    row.status = "published"
    mark_resource_open_gallery(row, row.metadata_json or {})
    row.updated_at = utc_now()
    db.add(row)
    db.flush()
    ensure_open_gallery_binding(
        db,
        tenant_id,
        "general_skill",
        row.id,
        "active",
        metadata_json=row.metadata_json or {},
    )
    db.commit()
    db.refresh(row)
    return general_skill_read(row)


@router.post("/{slug}/archive", response_model=GeneralSkillRead)
def archive_general_skill(
    slug: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    agent_id: str | None = Query(None),
    current_user: User = Depends(get_current_user),
) -> GeneralSkillRead:
    row = _get_general_skill(db, tenant_id, slug)
    agent_id = _agent_id_or_none(agent_id)
    agent = ensure_agent_scope_manager(db, tenant_id, agent_id, current_user)
    if agent and not agent.is_overall:
        binding = _ensure_general_skill_binding(db, tenant_id, agent.id, row.id)
        binding.status = "inactive"
        binding.updated_at = utc_now()
        db.add(binding)
        db.commit()
        return general_skill_read(row, status_override="archived")
    ensure_open_gallery_admin(tenant_id, current_user)
    row.status = "archived"
    row.updated_at = utc_now()
    db.add(row)
    db.flush()
    ensure_open_gallery_binding(db, tenant_id, "general_skill", row.id, "inactive")
    db.commit()
    db.refresh(row)
    return general_skill_read(row)


@router.delete("/{slug}")
def delete_general_skill(
    slug: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    agent_id: str | None = Query(None),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    agent_id = _agent_id_or_none(agent_id)
    row = _get_general_skill(db, tenant_id, slug)
    agent = ensure_agent_scope_manager(db, tenant_id, agent_id, current_user)
    if agent and not agent.is_overall:
        binding = _ensure_general_skill_binding(db, tenant_id, agent.id, row.id)
        binding.status = "deleted"
        binding.updated_at = utc_now()
        db.add(binding)
        db.commit()
        return {"status": "hidden", "slug": slug}
    if agent and agent.is_overall:
        if not is_open_gallery_resource(db, tenant_id, "general_skill", row):
            raise HTTPException(status_code=404, detail="General skill not visible in open gallery")
        ensure_open_gallery_admin(tenant_id, current_user)
        hide_open_gallery_binding(db, tenant_id, "general_skill", row.id)
        db.commit()
        return {"status": "hidden", "slug": slug}

    require_overall_agent(db, tenant_id, agent_id)
    ensure_open_gallery_admin(tenant_id, current_user)
    db.delete(row)
    db.commit()
    return {"status": "deleted", "slug": slug}


@router.post("/{slug}/run", response_model=GeneralSkillRunResponse)
def run_general_skill(
    slug: str,
    request: GeneralSkillRunRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> GeneralSkillRunResponse:
    skill = _get_general_skill(db, request.tenant_id, slug)
    if skill.status != "published":
        raise HTTPException(status_code=400, detail="General skill is not published")
    require_agent_scope_viewer(request.tenant_id, request.agent_id, current_user, db)
    _ensure_general_skill_visible(db, request.tenant_id, skill, request.agent_id)
    model_config = _get_request_model(db, request.tenant_id, request.model_config_id)
    return GeneralSkillRunner().run(
        skill, request.query, model_config, current_user.id, request.max_attempts
    )


@router.post("/{slug}/run/stream")
def run_general_skill_stream(
    slug: str,
    request: GeneralSkillRunRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    skill = _get_general_skill(db, request.tenant_id, slug)
    if skill.status != "published":
        raise HTTPException(status_code=400, detail="General skill is not published")
    require_agent_scope_viewer(request.tenant_id, request.agent_id, current_user, db)
    _ensure_general_skill_visible(db, request.tenant_id, skill, request.agent_id)
    model_config = _get_request_model(db, request.tenant_id, request.model_config_id)
    skill_snapshot = _general_skill_snapshot(skill)
    model_snapshot = model_config

    def stream_events() -> Iterator[str]:
        events: queue.Queue[tuple[str, dict[str, object]] | None] = queue.Queue()

        def sink(item: dict[str, object]) -> None:
            events.put(("trace", item))

        def worker() -> None:
            try:
                response = GeneralSkillRunner().run(
                    skill_snapshot,
                    request.query,
                    model_snapshot,
                    current_user.id,
                    request.max_attempts,
                    sink,
                )
                events.put(("complete", response.model_dump(mode="json")))
            except Exception as exc:  # pragma: no cover - defensive stream boundary
                events.put(("error", {"message": str(exc)}))
            finally:
                events.put(None)

        threading.Thread(target=worker, daemon=True).start()
        yield _sse(
            "stream_started",
            {"skill_slug": skill_snapshot.slug, "max_attempts": request.max_attempts},
        )
        last_worker_event_at = time.monotonic()
        while True:
            try:
                item = events.get(timeout=5)
            except queue.Empty:
                if (
                    time.monotonic() - last_worker_event_at
                    > GENERAL_SKILL_STREAM_IDLE_TIMEOUT_SECONDS
                ):
                    yield _sse(
                        "error",
                        {
                            "message": "通用技能运行超时，请检查模型配置或稍后重试。",
                            "code": "general_skill_stream_timeout",
                        },
                    )
                    return
                yield _sse("heartbeat", {"phase": "running"})
                continue
            if item is None:
                return
            last_worker_event_at = time.monotonic()
            event, payload = item
            yield _sse(event, payload)

    return StreamingResponse(stream_events(), media_type="text/event-stream")


def _get_general_skill(db: Session, tenant_id: str, slug: str) -> GeneralSkill:
    ensure_tenant(db, tenant_id)
    row = db.exec(
        select(GeneralSkill).where(GeneralSkill.tenant_id == tenant_id, GeneralSkill.slug == slug)
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="General skill not found")
    return row


def _ensure_general_skill_visible(
    db: Session,
    tenant_id: str,
    row: GeneralSkill,
    agent_id: str | None,
) -> None:
    agent = get_agent(db, tenant_id, _agent_id_or_none(agent_id))
    if not agent or agent.is_overall:
        if is_open_gallery_resource(db, tenant_id, "general_skill", row):
            return
        raise HTTPException(status_code=404, detail="General skill not visible in open gallery")
    binding = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == agent.id,
            AgentResourceBinding.resource_type == "general_skill",
            AgentResourceBinding.resource_id == row.id,
        )
    ).first()
    if not binding or not is_bound_resource_visible_for_agent(
        db, tenant_id, "general_skill", row, binding
    ):
        raise HTTPException(status_code=404, detail="General skill not visible to this agent")


def _ensure_general_skill_binding(
    db: Session,
    tenant_id: str,
    agent_id: str,
    general_skill_id: str,
    metadata_json: dict[str, object] | None = None,
) -> AgentResourceBinding:
    row = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == agent_id,
            AgentResourceBinding.resource_type == "general_skill",
            AgentResourceBinding.resource_id == general_skill_id,
        )
    ).first()
    metadata = {
        **(metadata_json or {}),
        "scope": "agent_private",
        "visibility": "agent_private",
        "owner_agent_id": agent_id,
        "created_from_agent": True,
    }
    if row:
        merged_metadata = {
            **(row.metadata_json or {}),
            **metadata,
        }
        row.metadata_json = metadata_preserving_creator(
            row.metadata_json,
            merged_metadata,
        )
        return row
    row = AgentResourceBinding(
        tenant_id=tenant_id,
        agent_id=agent_id,
        resource_type="general_skill",
        resource_id=general_skill_id,
        status="active",
        metadata_json=metadata,
    )
    db.add(row)
    db.flush()
    return row


def _general_skill_editable_by_agent(
    db: Session, tenant_id: str, agent_id: str, row: GeneralSkill
) -> bool:
    metadata = row.metadata_json or {}
    if metadata.get("owner_agent_id") == agent_id and metadata.get("scope") == "agent_private":
        return True
    binding = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == agent_id,
            AgentResourceBinding.resource_type == "general_skill",
            AgentResourceBinding.resource_id == row.id,
            AgentResourceBinding.status != "deleted",
        )
    ).first()
    return bool(
        binding
        and not is_open_gallery_resource(db, tenant_id, "general_skill", row)
        and is_bound_resource_visible_for_agent(db, tenant_id, "general_skill", row, binding)
    )


def _get_default_model(db: Session, tenant_id: str) -> ModelConfig:
    model_config = db.exec(
        select(ModelConfig).where(
            ModelConfig.tenant_id == tenant_id,
            ModelConfig.is_default == True,  # noqa: E712
            ModelConfig.enabled == True,  # noqa: E712
        )
    ).first()
    if not model_config:
        raise HTTPException(status_code=400, detail="No default model config")
    return _model_runtime_config(db, tenant_id, model_config)


def _get_request_model(
    db: Session, tenant_id: str, model_config_id: str | None = None
) -> ModelConfig:
    if not model_config_id:
        return _get_default_model(db, tenant_id)
    model_config = db.get(ModelConfig, model_config_id)
    if not model_config or model_config.tenant_id != tenant_id or not model_config.enabled:
        raise HTTPException(status_code=404, detail="Model config not found")
    return _model_runtime_config(db, tenant_id, model_config)


def _general_skill_snapshot(row: GeneralSkill) -> SimpleNamespace:
    return SimpleNamespace(
        tenant_id=row.tenant_id,
        slug=row.slug,
        name=row.name,
        description=row.description,
        homepage=row.homepage,
        skill_markdown=row.skill_markdown,
        skill_files_json=_skill_files_or_markdown(row),
        metadata_json=row.metadata_json or {},
        permissions_json=row.permissions_json or {},
        runtime_config_json=row.runtime_config_json or {},
        status=row.status,
    )


def _model_runtime_config(db: Session, tenant_id: str, row: ModelConfig):
    return resolve_model_config_for_runtime(db, tenant_id, row.id)


def _required_text(value: str | None, field: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail=f"General skill {field} cannot be empty")
    return cleaned


def _optional_text(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


def _normalize_skill_files(
    requested_files: list[GeneralSkillFile],
    markdown: str | None,
) -> list[GeneralSkillFile]:
    if not requested_files:
        content = _required_text(markdown, "markdown")
        return [
            GeneralSkillFile(path="SKILL.md", content=content, size=len(content.encode("utf-8")))
        ]
    cleaned_files: list[GeneralSkillFile] = []
    for file in requested_files:
        path = _clean_package_path(file.path)
        content = file.content or ""
        cleaned_files.append(
            GeneralSkillFile(
                path=path,
                content=content,
                size=file.size if file.size is not None else len(content.encode("utf-8")),
                mime_type=file.mime_type,
            )
        )
    skill_file = _find_skill_file(cleaned_files)
    if not skill_file:
        raise HTTPException(status_code=400, detail="General skill folder must contain SKILL.md")
    base_dir = skill_file.path.rsplit("/", 1)[0] if "/" in skill_file.path else ""
    if not base_dir:
        return cleaned_files
    normalized: list[GeneralSkillFile] = []
    prefix = f"{base_dir}/"
    for file in cleaned_files:
        if file.path == base_dir or not file.path.startswith(prefix):
            continue
        normalized.append(file.model_copy(update={"path": file.path[len(prefix) :]}))
    return normalized


def _clean_package_path(path: str) -> str:
    cleaned = str(path or "").replace("\\", "/").strip().strip("/")
    parts = [part for part in cleaned.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise HTTPException(status_code=400, detail=f"Invalid general skill file path: {path}")
    return "/".join(parts)


def _find_skill_file(files: list[GeneralSkillFile]) -> GeneralSkillFile | None:
    return next(
        (file for file in files if file.path.rsplit("/", 1)[-1].lower() == "skill.md"), None
    )


def _skill_markdown_from_files(files: list[GeneralSkillFile]) -> str:
    skill_file = _find_skill_file(files)
    if not skill_file or not skill_file.content.strip():
        raise HTTPException(status_code=400, detail="General skill SKILL.md cannot be empty")
    return skill_file.content


def _skill_files_or_markdown(row: GeneralSkill) -> list[dict[str, object]]:
    files = row.skill_files_json or []
    if files:
        return files
    return [
        {
            "path": "SKILL.md",
            "content": row.skill_markdown,
            "size": len(row.skill_markdown.encode("utf-8")),
        }
    ]


def _parse_skill_metadata(markdown: str) -> dict[str, object]:
    lines = markdown.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    metadata: dict[str, object] = {}
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            return metadata
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        if not key:
            continue
        metadata[key] = _parse_metadata_value(value.strip())
    return metadata


def _parse_metadata_value(value: str) -> object:
    cleaned = value.strip().strip("'\"")
    if cleaned.startswith("[") and cleaned.endswith("]"):
        return [item.strip().strip("'\"") for item in cleaned[1:-1].split(",") if item.strip()]
    return cleaned


def _metadata_text(metadata: dict[str, object], *keys: str) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-_")
    return slug or "general-skill"


def _unique_slug(db: Session, tenant_id: str, base_slug: str) -> str:
    base = _slugify(base_slug)
    candidate = base
    suffix = 2
    while db.exec(
        select(GeneralSkill).where(
            GeneralSkill.tenant_id == tenant_id, GeneralSkill.slug == candidate
        )
    ).first():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _source_name(source: str) -> str:
    parsed = urlparse(source)
    path = parsed.path if parsed.scheme else source
    cleaned = path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".zip").removesuffix(".md")
    if cleaned.startswith("upload:"):
        cleaned = cleaned.removeprefix("upload:")
    return cleaned or "开源平台通用技能"


def _clean_source_filename(filename: str) -> str:
    cleaned = str(filename or "").replace("\\", "/").strip().rsplit("/", 1)[-1]
    if not cleaned:
        raise HTTPException(status_code=400, detail="Uploaded skill package filename is required")
    return cleaned


def _decode_base64_payload(value: str) -> bytes:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Uploaded skill package content is required")
    if "," in cleaned and cleaned[:80].lower().startswith("data:"):
        cleaned = cleaned.split(",", 1)[1]
    try:
        data = base64.b64decode(cleaned, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail="Uploaded skill package content is not valid base64"
        ) from exc
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded skill package is empty")
    if len(data) > MAX_CLAWHUB_PACKAGE_BYTES:
        raise HTTPException(status_code=400, detail="Uploaded skill package is too large")
    return data


def _load_clawhub_source(source: str) -> list[GeneralSkillFile]:
    cleaned = _required_text(source, "source")
    clawhub_slug = _clawhub_slug_from_source(cleaned)
    if clawhub_slug:
        source_url = cleaned if cleaned.startswith(("http://", "https://")) else None
        return _load_clawhub_skill_package(clawhub_slug, source_url=source_url)
    if cleaned.startswith(("http://", "https://")):
        return _load_remote_skill_source(cleaned)
    if _looks_like_github_shorthand(cleaned):
        return _load_remote_skill_source(f"https://github.com/{cleaned}")
    raise HTTPException(
        status_code=400,
        detail="开源平台来源必须是开源平台 slug、GitHub URL、raw SKILL.md URL、zip URL 或 owner/repo 路径",
    )


def _clawhub_slug_from_source(source: str) -> str | None:
    cleaned = source.strip()
    if not cleaned:
        return None
    if cleaned.startswith(("http://", "https://")):
        parsed = urlparse(cleaned)
        if parsed.netloc not in REMOTE_SKILLHUB_HOSTS:
            return None
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) >= 2:
            slug = parts[1]
        elif len(parts) == 1:
            slug = parts[0]
        else:
            return None
        return _valid_clawhub_slug(slug)
    if "/" not in cleaned:
        return _valid_clawhub_slug(cleaned)
    return None


def _valid_clawhub_slug(value: str) -> str | None:
    slug = value.strip().removesuffix(".zip").removesuffix(".md")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,127}", slug):
        return slug
    return None


def _clawhub_homepage_from_source(source: str) -> str | None:
    cleaned = source.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme and parsed.netloc in REMOTE_SKILLHUB_HOSTS:
        return cleaned
    slug = _clawhub_slug_from_source(cleaned)
    if slug:
        return f"https://skillhub.ai/{slug}"
    return None


def _clawhub_download_url(slug: str) -> str:
    return f"{CLAWHUB_DOWNLOAD_ENDPOINT}?slug={quote(slug, safe='')}"


def _load_clawhub_skill_package(slug: str, source_url: str | None = None) -> list[GeneralSkillFile]:
    download_url = _clawhub_download_url(slug)
    try:
        return _load_remote_skill_source(download_url)
    except HTTPException as download_error:
        if source_url:
            try:
                return _load_remote_skill_source(source_url)
            except HTTPException:
                pass
        raise download_error


def _looks_like_github_shorthand(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/.+)?", value.strip()))


def _load_remote_skill_source(url: str, visited: set[str] | None = None) -> list[GeneralSkillFile]:
    normalized_url = url.strip()
    parsed = urlparse(normalized_url)
    if not parsed.scheme or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Remote skill source must be a valid URL")
    visited = visited or set()
    if normalized_url in visited:
        raise HTTPException(status_code=400, detail="Remote skill source redirects to itself")
    if len(visited) >= 5:
        raise HTTPException(
            status_code=400, detail="Remote skill source contains too many indirections"
        )
    visited.add(normalized_url)
    if parsed.netloc in GITHUB_HOSTS or parsed.netloc == RAW_GITHUB_HOST:
        return _load_github_skill_source(parsed)
    data, content_type = _download_url(normalized_url)
    lower_content_type = content_type.lower()
    if parsed.path.lower().endswith(".zip") or "zip" in lower_content_type:
        return _files_from_zip(data)
    text = _decode_text(data)
    if _looks_like_html_response(text, lower_content_type):
        linked_source = _extract_skill_source_from_html(text, normalized_url)
        if linked_source:
            return _load_remote_skill_source(linked_source, visited)
        raise HTTPException(
            status_code=400,
            detail=(
                "开源平台页面没有暴露可下载的技能包或 GitHub 目录。"
                "HTML 页面不会被当作 SKILL.md 导入。"
            ),
        )
    if _looks_like_markdown_source(parsed.path, lower_content_type):
        file_name = unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1]) or "SKILL.md"
        if not file_name.lower().endswith(".md"):
            file_name = "SKILL.md"
        return [
            GeneralSkillFile(
                path=_clean_package_path(file_name),
                content=text,
                size=len(data),
                mime_type=content_type or "text/markdown",
            )
        ]
    raise HTTPException(
        status_code=400,
        detail="Remote source must be a zip package, GitHub skill directory, or raw Markdown skill file",
    )


def _load_github_skill_source(parsed) -> list[GeneralSkillFile]:
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if parsed.netloc == RAW_GITHUB_HOST:
        if len(parts) < 4:
            raise HTTPException(
                status_code=400,
                detail="Raw GitHub source must include owner, repo, branch and path",
            )
        owner, repo, branch = parts[0], parts[1], parts[2]
        file_path = "/".join(parts[3:])
        data, content_type = _download_url(parsed.geturl())
        return [
            GeneralSkillFile(
                path=file_path.rsplit("/", 1)[-1] or "SKILL.md",
                content=_decode_text(data),
                size=len(data),
                mime_type=content_type or "text/markdown",
            )
        ]
    if len(parts) < 2:
        raise HTTPException(
            status_code=400, detail="GitHub source must include owner and repository"
        )
    owner, repo = parts[0], parts[1].removesuffix(".git")
    if len(parts) >= 3 and parts[2] == "archive":
        data, _ = _download_url(parsed.geturl())
        return _files_from_zip(data)
    if len(parts) >= 5 and parts[2] in {"blob", "raw"}:
        branch = parts[3]
        file_path = "/".join(parts[4:])
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{file_path}"
        data, content_type = _download_url(raw_url)
        return [
            GeneralSkillFile(
                path=file_path.rsplit("/", 1)[-1] or "SKILL.md",
                content=_decode_text(data),
                size=len(data),
                mime_type=content_type or "text/markdown",
            )
        ]
    if len(parts) >= 5 and parts[2] == "tree":
        branch = parts[3]
        subtree = "/".join(parts[4:])
        return _download_github_directory(owner, repo, branch, subtree)
    subtree = "/".join(parts[2:]) if len(parts) > 2 else ""
    errors: list[str] = []
    for branch in ["main", "master"]:
        try:
            return _download_github_directory(owner, repo, branch, subtree)
        except HTTPException as exc:
            errors.append(str(exc.detail))
    return _download_github_archive(owner, repo, ["main", "master"], subtree)


def _download_github_directory(
    owner: str, repo: str, branch: str, subtree: str = ""
) -> list[GeneralSkillFile]:
    try:
        return _download_github_directory_contents(owner, repo, branch, subtree)
    except HTTPException as api_error:
        try:
            return _download_github_archive(owner, repo, [branch], subtree)
        except HTTPException:
            raise api_error


def _download_github_directory_contents(
    owner: str, repo: str, branch: str, subtree: str = ""
) -> list[GeneralSkillFile]:
    normalized_subtree = subtree.strip("/")
    files: list[GeneralSkillFile] = []
    visited_dirs: set[str] = set()

    def walk(path: str) -> None:
        if len(files) >= MAX_CLAWHUB_FILES:
            return
        if path in visited_dirs:
            return
        visited_dirs.add(path)
        api_path = quote(path, safe="/")
        api_url = f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}/contents"
        if api_path:
            api_url = f"{api_url}/{api_path}"
        api_url = f"{api_url}?ref={quote(branch, safe='')}"
        payload = _download_json(api_url)
        entries = payload if isinstance(payload, list) else [payload]
        for entry in entries:
            if len(files) >= MAX_CLAWHUB_FILES:
                break
            if not isinstance(entry, dict):
                continue
            item_type = str(entry.get("type") or "")
            item_path = str(entry.get("path") or "").strip("/")
            if not item_path or _skip_package_path(item_path):
                continue
            if item_type == "dir":
                walk(item_path)
                continue
            if item_type != "file":
                continue
            size = int(entry.get("size") or 0)
            if size > MAX_CLAWHUB_FILE_BYTES:
                continue
            download_url = str(entry.get("download_url") or "")
            if not download_url:
                continue
            relative = item_path
            if normalized_subtree and item_path.startswith(f"{normalized_subtree}/"):
                relative = item_path[len(normalized_subtree) + 1 :]
            data, content_type = _download_url(download_url)
            if len(data) > MAX_CLAWHUB_FILE_BYTES:
                continue
            files.append(
                GeneralSkillFile(
                    path=_clean_package_path(relative),
                    content=_decode_text(data),
                    size=len(data),
                    mime_type=content_type or _guess_mime_type(relative),
                )
            )

    walk(normalized_subtree)
    if not _find_skill_file(files):
        raise HTTPException(status_code=400, detail="GitHub directory does not contain SKILL.md")
    return files


def _download_github_archive(
    owner: str, repo: str, branches: list[str], subtree: str = ""
) -> list[GeneralSkillFile]:
    errors: list[str] = []
    for branch in branches:
        archive_url = f"https://github.com/{owner}/{repo}/archive/refs/heads/{branch}.zip"
        try:
            data, _ = _download_url(archive_url)
            return _files_from_zip(data, subtree=subtree)
        except HTTPException as exc:
            errors.append(str(exc.detail))
    raise HTTPException(
        status_code=400, detail=f"Unable to download GitHub skill package: {'; '.join(errors)}"
    )


def _download_json(url: str) -> object:
    data, _ = _download_url(url)
    try:
        return json.loads(_decode_text(data))
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400, detail="Remote GitHub API returned invalid JSON"
        ) from exc


def _looks_like_markdown_source(path: str, content_type: str) -> bool:
    lower_path = path.lower()
    lower_content_type = content_type.lower()
    return (
        lower_path.endswith(".md")
        or lower_path.endswith("/skill")
        or "text/markdown" in lower_content_type
        or "text/plain" in lower_content_type
    )


def _looks_like_html_response(text: str, content_type: str) -> bool:
    stripped = text.lstrip().lower()
    return (
        "text/html" in content_type
        or stripped.startswith("<!doctype html")
        or stripped.startswith("<html")
    )


def _extract_skill_source_from_html(text: str, base_url: str) -> str | None:
    normalized = unescape(text).replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    candidates: list[str] = []
    candidates.extend(re.findall(r"https?://[^\s\"'<>]+", normalized))
    for match in re.finditer(
        r"""(?:href|src)\s*=\s*["']([^"']+)["']""", normalized, flags=re.IGNORECASE
    ):
        candidates.append(urljoin(base_url, match.group(1)))
    seen: set[str] = set()
    for candidate in candidates:
        cleaned = candidate.strip().rstrip("),.;]")
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        parsed = urlparse(cleaned)
        if not parsed.scheme or not parsed.netloc:
            continue
        lower_path = parsed.path.lower()
        if parsed.netloc == RAW_GITHUB_HOST:
            return cleaned
        if _is_clawhub_download_url(parsed):
            return cleaned
        if parsed.netloc in GITHUB_HOSTS and (
            "/tree/" in lower_path
            or "/blob/" in lower_path
            or lower_path.endswith(".zip")
            or "/archive/" in lower_path
        ):
            return cleaned
        if lower_path.endswith(".zip"):
            return cleaned
    return None


def _is_clawhub_download_url(parsed) -> bool:
    path = parsed.path.lower().rstrip("/")
    return path.endswith("/api/v1/download") and "slug=" in parsed.query.lower()


def _download_url(url: str) -> tuple[bytes, str]:
    try:
        request = Request(url, headers={"User-Agent": "StaffDeck-GeneralSkillImporter/1.0"})
        with urlopen(request, timeout=REMOTE_SKILL_DOWNLOAD_TIMEOUT_SECONDS) as response:  # noqa: S310 - user-confirmed import source
            content_type = response.headers.get("content-type", "")
            data = response.read(MAX_CLAWHUB_PACKAGE_BYTES + 1)
    except HTTPError as exc:
        raise HTTPException(
            status_code=400, detail=f"Download failed with HTTP {exc.code}"
        ) from exc
    except URLError as exc:
        raise HTTPException(status_code=400, detail=f"Download failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=400, detail="Download timed out") from exc
    if len(data) > MAX_CLAWHUB_PACKAGE_BYTES:
        raise HTTPException(status_code=400, detail="General skill package is too large")
    return data, content_type


def _files_from_zip(data: bytes, subtree: str = "") -> list[GeneralSkillFile]:
    normalized_subtree = subtree.strip("/")
    with zipfile.ZipFile(BytesIO(data)) as archive:
        names = [
            name
            for name in archive.namelist()
            if not name.endswith("/") and not _skip_package_path(name)
        ]
        skill_candidates = [name for name in names if name.rsplit("/", 1)[-1].lower() == "skill.md"]
        if normalized_subtree:
            skill_candidates = [
                name
                for name in skill_candidates
                if _zip_relative_path(name, normalized_subtree) is not None
            ]
        if not skill_candidates:
            raise HTTPException(status_code=400, detail="Package does not contain SKILL.md")
        base = skill_candidates[0].rsplit("/", 1)[0] if "/" in skill_candidates[0] else ""
        files: list[GeneralSkillFile] = []
        for name in names:
            if base:
                if not name.startswith(f"{base}/"):
                    continue
                relative = name[len(base) + 1 :]
            else:
                relative = name
            if not relative or relative.endswith("/"):
                continue
            info = archive.getinfo(name)
            if info.file_size > MAX_CLAWHUB_FILE_BYTES:
                continue
            if len(files) >= MAX_CLAWHUB_FILES:
                break
            content = _decode_text(archive.read(name))
            files.append(
                GeneralSkillFile(
                    path=relative,
                    content=content,
                    size=info.file_size,
                    mime_type=_guess_mime_type(relative),
                )
            )
    return files


def _zip_relative_path(name: str, subtree: str) -> str | None:
    parts = name.split("/")
    for index in range(1, len(parts)):
        candidate = "/".join(parts[index:])
        if candidate == subtree or candidate.startswith(f"{subtree}/"):
            return candidate
    return None


def _skip_package_path(path: str) -> bool:
    parts = path.split("/")
    return any(
        part in {"__MACOSX", ".git", "node_modules", ".venv", "dist", "build"} for part in parts
    )


def _decode_text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def _guess_mime_type(path: str) -> str:
    lower = path.lower()
    if lower.endswith(".md"):
        return "text/markdown"
    if lower.endswith((".py", ".sh", ".js", ".ts", ".json", ".txt", ".yaml", ".yml")):
        return "text/plain"
    return "text/plain"


def _validate_slug(value: str) -> None:
    if any(char.isspace() for char in value) or "/" in value:
        raise HTTPException(
            status_code=400, detail="General skill slug cannot contain spaces or slashes"
        )


def _sse(event: object, data: object) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"
