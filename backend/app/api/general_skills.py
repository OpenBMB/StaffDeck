from __future__ import annotations

import base64
import binascii
import json
import queue
import re
import threading
import time
import uuid
import zipfile
from collections.abc import Callable, Iterator
from html import unescape
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlparse
from urllib.request import Request, urlopen

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import or_, type_coerce
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import load_only
from sqlmodel import Session, select

from app.agents.branching import (
    build_binding_visibility_prefetch,
    ensure_open_gallery_binding,
    ensure_private_resource_binding,
    get_agent,
    get_overall_agent,
    hide_open_gallery_binding,
    is_bound_resource_visible_for_agent,
    is_open_gallery_resource,
    mark_resource_open_gallery,
    mark_resource_private_for_agent,
    metadata_preserving_creator,
    user_creator_metadata,
)
from app.capabilities.local_general_skill import (
    GeneralSkillRuntimeSnapshot,
    local_runtime_snapshot,
)
from app.capability_scope import normalize_capability_scope
from app.core import AgentLoop
from app.db import engine, get_session
from app.db.models import (
    AgentEvent,
    AgentResourceBinding,
    ChatSession,
    GeneralSkill,
    Message,
    ModelConfig,
    User,
    new_id,
    utc_now,
)
from app.general_skills import (
    GeneralSkillClawHubImportRequest,
    GeneralSkillImportRequest,
    GeneralSkillPackageUploadRequest,
    GeneralSkillRead,
    GeneralSkillRunRequest,
    GeneralSkillRunResponse,
)
from app.general_skills.runner import GeneralSkillReader, GeneralSkillRunner
from app.general_skills.schema import (
    GeneralSkillFile,
    GeneralSkillPackagePreviewFile,
    GeneralSkillPackagePreviewResponse,
)
from app.llm.model_config_resolver import resolve_model_config_for_runtime
from app.security.auth import get_current_user
from app.security.permissions import (
    ensure_agent_scope_manager,
    ensure_open_gallery_admin,
    ensure_open_gallery_member,
    is_admin_user,
    require_agent_scope_viewer,
)
from app.security.tenant import ensure_tenant
from app.session.session_schema import ChatTurnRequest, ChatTurnResponse

router = APIRouter(
    prefix="/api/enterprise/general-skills",
    tags=["enterprise:general-skills"],
    dependencies=[Depends(get_current_user)],
)

MAX_CLAWHUB_PACKAGE_BYTES = 96 * 1024 * 1024
MAX_CLAWHUB_FILE_BYTES = 2 * 1024 * 1024
MAX_CLAWHUB_FILES = 240
REMOTE_SKILL_DOWNLOAD_TIMEOUT_SECONDS = 120
GENERAL_SKILL_STREAM_IDLE_TIMEOUT_SECONDS = 600
GENERAL_SKILL_DEBUG_CHANNEL = "skill_test"
GENERAL_SKILL_MISSING_REFERENCES_CODE = "GENERAL_SKILL_MISSING_REFERENCES"
GITHUB_HOSTS = {"github.com", "www.github.com"}
RAW_GITHUB_HOST = "raw.githubusercontent.com"
CLAWHUB_HOSTS = {"clawhub.ai", "www.clawhub.ai"}
SKILLHUB_HOSTS = {"skillhub.ai", "www.skillhub.ai"}
REMOTE_SKILLHUB_HOSTS = CLAWHUB_HOSTS | SKILLHUB_HOSTS
CLAWHUB_DOWNLOAD_ENDPOINT = "https://wry-manatee-359.convex.site/api/v1/download"
# 只吃「像路径」的字符。早先的字符类 `[^\s`'"<>|]` 会把 markdown 链接语法一起吞掉：
# `[aiapp.md](./references/products/aiapp.md)` 被整体匹配成
# `references/products/aiapp.md](./references/products/aiapp.md`，生成一个**永不可能存在**
# 的路径，于是一个文件都不缺的包也会被判定为缺失（2026-09-20 实测 dws.zip：12 条报错里 11 条是假路径）。
LOCAL_REFERENCE_PATTERN = re.compile(r"(?<![\w./-])(?:\./)?references/[\w.+@~%/-]+")


def _agent_id_or_none(agent_id: object | None) -> str | None:
    return agent_id if isinstance(agent_id, str) and agent_id else None


def general_skill_read(
    row: GeneralSkill,
    status_override: str | None = None,
    *,
    include_files: bool = True,
) -> GeneralSkillRead:
    # 广场等只需列表元信息的调用方可以关掉文件包内容：skill_files_json 单租户就有 MB 级体量
    # （本租户 12 条实测 6.5MB，整表查询 9.8s），而卡片只用到名称/描述/标签。
    return GeneralSkillRead(
        id=row.id,
        tenant_id=row.tenant_id,
        slug=row.slug,
        name=row.name,
        description=row.description,
        category=row.category,
        homepage=row.homepage,
        skill_markdown=row.skill_markdown,
        skill_files=(
            [GeneralSkillFile.model_validate(item) for item in _skill_files_or_markdown(row)]
            if include_files
            else []
        ),
        # 目录索引依赖文件包内容，裁剪时不参与计算
        skill_directories=_skill_directories(row) if include_files else [],
        metadata=dict(row.metadata_json or {}),
        status=status_override or row.status,
        capability_scope=normalize_capability_scope(row.capability_scope),
        download_count=row.download_count or 0,
        permissions=row.permissions_json or {},
        runtime_config=row.runtime_config_json or {},
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


# 裁剪模式下显式指定要加载的列，避免 ORM 把 skill_files_json 一起取回来。
_GENERAL_SKILL_SLIM_COLUMNS = (
    "id",
    "tenant_id",
    "slug",
    "name",
    "description",
    "category",
    "homepage",
    "skill_markdown",
    "metadata_json",
    "status",
    "capability_scope",
    "download_count",
    "permissions_json",
    "runtime_config_json",
    "created_at",
    "updated_at",
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
        ensure_open_gallery_member(request.tenant_id, current_user)
    row = None
    package_source_row = None
    inherited_capability_scope = "general"
    if lookup_slug:
        row = db.exec(
            select(GeneralSkill).where(
                GeneralSkill.tenant_id == request.tenant_id,
                GeneralSkill.slug == lookup_slug,
            )
        ).first()
        if not row:
            raise HTTPException(status_code=404, detail="General skill to update was not found")
        inherited_capability_scope = normalize_capability_scope(row.capability_scope)
        if slug != row.slug:
            raise HTTPException(status_code=400, detail="General skill slug cannot be modified")
        if is_private_agent_scope:
            if is_open_gallery_resource(db, request.tenant_id, "general_skill", row):
                package_source_row = row
                row = None
                slug = _unique_slug(db, request.tenant_id, slug)
            elif not _general_skill_editable_by_agent(db, request.tenant_id, agent.id, row):
                raise HTTPException(
                    status_code=404, detail="General skill not visible to this agent"
                )
            elif not _private_skill_owned_by_agent(
                db, request.tenant_id, row, agent.id
            ):
                package_source_row = row
                row = None
                slug = _unique_slug(db, request.tenant_id, slug)
        else:
            # Open-gallery update path: the existing skill may only be overwritten by
            # its creator or an administrator (prevents a member from clobbering another
            # member's published skill via original_slug).
            _require_gallery_skill_owner_or_admin(row, current_user)
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
            elif is_private_agent_scope and _private_skill_owned_by_agent(
                db, request.tenant_id, conflict, agent.id
            ):
                # A private skill removed from this agent keeps its entity so
                # other references remain stable; re-import should restore it.
                row = conflict
            else:
                raise HTTPException(status_code=409, detail="General skill slug already exists")
    package_source = row or package_source_row
    if package_source is not None and not request.files and request.markdown is not None:
        files = _replace_skill_markdown_in_package(package_source, request.markdown)
        markdown = _skill_markdown_from_files(files)
    requested_directories = (
        _skill_directories_from_values(request.directories, files)
        if request.directories is not None
        else None
    )
    _validate_skill_package_references(
        files, allow_missing=request.allow_missing_references
    )
    now = utc_now()
    if row:
        metadata = metadata_preserving_creator(row.metadata_json, parsed_metadata)
        directories = (
            requested_directories
            if requested_directories is not None
            else _skill_directories(row)
        )
        if directories:
            metadata["skill_directories"] = directories
        else:
            metadata.pop("skill_directories", None)
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
        # category 用「空串 = 清空」语义：调用方总是显式传当前表单值，
        # 不传（None）表示这次保存不涉及分类（如从开源平台整包导入）。
        if request.category is not None:
            row.category = _optional_text(request.category)
        row.homepage = homepage
        row.skill_markdown = markdown
        row.skill_files_json = [file.model_dump(mode="json") for file in files]
        row.metadata_json = metadata
        row.status = request.status
        if request.capability_scope is not None:
            row.capability_scope = request.capability_scope
        row.updated_at = now
    else:
        if requested_directories:
            metadata["skill_directories"] = requested_directories
        row = GeneralSkill(
            tenant_id=request.tenant_id,
            slug=slug,
            name=name,
            description=description,
            category=_optional_text(request.category),
            homepage=homepage,
            skill_markdown=markdown,
            skill_files_json=[file.model_dump(mode="json") for file in files],
            metadata_json=metadata,
            status=request.status,
            capability_scope=normalize_capability_scope(
                request.capability_scope or inherited_capability_scope
            ),
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
            revive=True,
        )
        if package_source_row is not None and package_source_row.id != row.id:
            replaced_binding = db.exec(
                select(AgentResourceBinding).where(
                    AgentResourceBinding.tenant_id == request.tenant_id,
                    AgentResourceBinding.agent_id == agent.id,
                    AgentResourceBinding.resource_type == "general_skill",
                    AgentResourceBinding.resource_id == package_source_row.id,
                    AgentResourceBinding.status != "deleted",
                )
            ).first()
            if replaced_binding is not None:
                replaced_binding.status = "deleted"
                replaced_binding.updated_at = now
                db.add(replaced_binding)
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
        capability_scope=request.capability_scope,
        current_user=current_user,
        allow_missing_references=request.allow_missing_references,
    )


@router.post("/import-clawhub", response_model=GeneralSkillRead)
def import_clawhub_skill(
    request: GeneralSkillClawHubImportRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> GeneralSkillRead:
    return import_skillhub_skill(request, db, current_user)


def _preview_package_response(filename: str, data: bytes) -> GeneralSkillPackagePreviewResponse:
    """zip / Markdown 内容 → 编辑器预览响应。JSON 与 multipart 入口共用。"""
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
    markdown = _skill_markdown_from_files(files)
    metadata = _parse_skill_metadata(markdown)
    return GeneralSkillPackagePreviewResponse(
        filename=filename,
        name=_metadata_text(metadata, "name", "title") or _source_name(f"upload:{filename}"),
        slug=_metadata_text(metadata, "slug", "id"),
        description=_metadata_text(metadata, "description", "summary"),
        homepage=_metadata_text(metadata, "homepage", "url", "source"),
        markdown=markdown,
        files=[
            GeneralSkillPackagePreviewFile(
                path=file.path,
                size=file.size,
                mime_type=file.mime_type,
                content=file.content,
            )
            for file in files
        ],
        directories=_skill_directories_from_values(
            [value for value in metadata.get("skill_directories", []) if isinstance(value, str)]
            if isinstance(metadata.get("skill_directories"), list)
            else [],
            files,
        ),
    )


@router.post("/import-package/preview", response_model=GeneralSkillPackagePreviewResponse)
def preview_general_skill_package(
    request: GeneralSkillPackageUploadRequest,
) -> GeneralSkillPackagePreviewResponse:
    """解析上传的技能包（zip / Markdown），返回将填入编辑器的全部内容，但不落库。

    与 /import-package 的差别：这里不创建技能、不消耗 slug，前端拿到响应后把
    name/slug/description/homepage/markdown/files/directories 填进新建表单，
    由用户继续编辑、预览 zip 内文件后再手动保存。解析规则与导入保持同源
    （_files_from_zip / _parse_skill_metadata），避免「预览看到的」和「实际导入的」不一致。
    """
    filename = _clean_source_filename(request.filename)
    data = _decode_base64_payload(request.content_base64)
    return _preview_package_response(filename, data)


@router.post("/import-package/preview-multipart")
def preview_general_skill_package_multipart(
    tenant_id: str = Query(...),
    file: UploadFile = File(...),
) -> GeneralSkillPackagePreviewResponse:
    """multipart 直传版预览：省掉 27MB 级 base64-JSON 的编解码与字符串驻留。

    网关限制 50MB，调用方（前端）已真正按字节流上传；这里只做大小上限与
    统一解析。大小上限与 JSON 版 _decode_base64_payload 同源（96MB）。
    """
    filename = _clean_source_filename(file.filename or "")
    data = file.file.read(MAX_CLAWHUB_PACKAGE_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded skill package is empty")
    if len(data) > MAX_CLAWHUB_PACKAGE_BYTES:
        raise HTTPException(status_code=400, detail="Uploaded skill package is too large")
    return _preview_package_response(filename, data)


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
        capability_scope=request.capability_scope,
        current_user=current_user,
        allow_missing_references=request.allow_missing_references,
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
    capability_scope: str = "general",
    current_user: object | None = None,
    allow_missing_references: bool = False,
) -> GeneralSkillRead:
    _validate_skill_package_references(
        files, allow_missing=allow_missing_references
    )
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
        capability_scope=normalize_capability_scope(capability_scope),
        permissions_json={"network": True, "python": True},
        runtime_config_json={"runtime": "python", "timeout_seconds": 12},
        created_at=now,
        updated_at=now,
    )
    if not (agent and not agent.is_overall):
        ensure_open_gallery_member(tenant_id, current_user)
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


def _list_my_general_skills(
    db: Session, tenant_id: str, current_user: User
) -> list[GeneralSkillRead]:
    """「我创建的技能」：跨作用域列出当前用户创建的技能（广场 + 挂在员工下的）。

    技能广场解耦数字员工后，成员在广场里创建的技能不再挂在任何数字员工下，
    该口径给创建者一个维护入口（编辑 / 启停 / 删除），不随当前作用域变化。

    性能：创建者过滤下沉到 SQL（metadata_json 的 owner 键，SQLite 走
    json_extract、PG 走 ->>），并把 skill_files_json 投影掉——单租户技能包
    有 MB 级体量（12 条实测 6.5MB，远程 PG 整表拉取再 Python 过滤要 9.8s），
    而维护列表只用到元信息和状态。编辑页会重新拉单条详情，不依赖本接口返回文件。
    """
    # metadata_json 声明为 JSON 列：SQLAlchemy 的 astext 只长在 postgresql.JSONB
    # 比较器上，所以用 type_coerce 把列类型临时 coerce 成 JSONB 再取 [key].astext，
    # 编译为 metadata_json ->> 'key'（PG 原生；SQLite >= 3.38 也支持 ->> 取文本，
    # 两端通用）。键不存在时表达式为 NULL，比较为假，与旧的 Python 侧「metadata
    # 里没有键就过滤掉」语义一致。
    metadata_column = type_coerce(GeneralSkill.metadata_json, JSONB)
    owner_keys = metadata_column["owner_user_id"].astext
    creator_keys = metadata_column["created_by_user_id"].astext
    rows = db.exec(
        _general_skill_list_statement(
            tenant_id,
            include_files=False,
            extra_where=(or_(owner_keys == current_user.id, creator_keys == current_user.id),),
        )
    ).all()
    owned = list(rows)
    if not owned:
        return []
    overall = get_overall_agent(db, tenant_id)
    overall_id = overall.id if overall else ""
    # 私有技能的启停状态记在宿主员工的绑定行上，广场技能记在 overall 员工的绑定行上。
    # 两类绑定一次查齐，避免逐行查询（远程 PG 单次往返 ~10ms）。
    agent_ids: set[str] = {overall_id} if overall_id else set()
    for row in owned:
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        owner_agent_id = str(metadata.get("owner_agent_id") or "")
        if owner_agent_id:
            agent_ids.add(owner_agent_id)
    binding_status: dict[tuple[str, str], str] = {}
    if agent_ids:
        for binding in db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == tenant_id,
                AgentResourceBinding.resource_type == "general_skill",
                AgentResourceBinding.agent_id.in_(agent_ids),
            )
        ).all():
            binding_status[(binding.agent_id, binding.resource_id)] = binding.status
    result: list[GeneralSkillRead] = []
    for row in owned:
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        owner_agent_id = str(metadata.get("owner_agent_id") or "")
        if not owner_agent_id:
            # 广场技能：已在广场删掉的（overall 绑定行被置为 deleted）不再回显，
            # 否则会变成一条删不掉的幽灵行。
            if binding_status.get((overall_id, row.id)) == "deleted":
                continue
            result.append(general_skill_read(row, include_files=False))
            continue
        status = binding_status.get((owner_agent_id, row.id))
        if status == "deleted":
            # 已从宿主员工移除的技能不再出现在维护列表里
            continue
        if status is None:
            result.append(general_skill_read(row, include_files=False))
            continue
        result.append(
            general_skill_read(
                row,
                status_override=(
                    "published" if status == "active" and row.status == "published" else "archived"
                ),
                include_files=False,
            )
        )
    return result


@router.get(
    "", response_model=list[GeneralSkillRead], dependencies=[Depends(require_agent_scope_viewer)]
)
def list_general_skills(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    agent_id: str | None = Query(None),
    # 刻意用「标量类型 + 裸默认值」而不是 Query(False)：本函数既当 HTTP handler，又被
    # public_api/resources.py 与多处测试直接当成普通函数调用。直接调用时 FastAPI 的
    # Query()/Depends() 默认值会以哨兵对象漏进来——Query(False) 是 truthy（会误进 mine
    # 分支），Depends() 哨兵没有 .id（AttributeError）。裸默认值在直接调用时是真正的
    # False；FastAPI 对「标量 + 裸默认值」同样按 query 参数解析，HTTP 行为与 Query(False) 一致。
    mine: bool = False,
    # 同上：标量 + 裸默认值，直接函数调用时拿到真正的 True，HTTP 下按 query 参数解析。
    # 广场列表只需要元信息，传 include_files=0 可省掉 MB 级的文件包内容。
    include_files: bool = True,
    current_user: User | None = Depends(get_current_user),
) -> list[GeneralSkillRead]:
    ensure_tenant(db, tenant_id)
    agent_id = _agent_id_or_none(agent_id)
    if mine:
        # 同理：直接调用时 current_user 可能是 Depends() 哨兵，不能直接当 User 用。
        if not isinstance(current_user, User):
            raise HTTPException(status_code=401, detail="Authentication required")
        return _list_my_general_skills(db, tenant_id, current_user)
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
                _general_skill_list_statement(
                    tenant_id,
                    include_files,
                    extra_where=(GeneralSkill.id.in_([binding.resource_id for binding in bindings]),),
                )
            ).all()
        }
        # 逐绑定判定每行要打 get_overall_agent + overall 绑定查询（远程 PG ~35ms/次），
        # 这里一次预取该作用域绑定集合的外部行，判定退化为内存查找。
        prefetch = build_binding_visibility_prefetch(db, tenant_id, bindings)
        visible_rows: list[GeneralSkillRead] = []
        for binding in bindings:
            row = rows_by_id.get(binding.resource_id)
            if not row:
                continue
            if not is_bound_resource_visible_for_agent(
                db, tenant_id, "general_skill", row, binding, prefetch=prefetch
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
                    include_files=include_files,
                )
            )
        return visible_rows
    rows = db.exec(
        _general_skill_list_statement(tenant_id, include_files)
    ).all()
    rows = [row for row in rows if is_open_gallery_resource(db, tenant_id, "general_skill", row)]
    return [general_skill_read(row, include_files=include_files) for row in rows]


def _general_skill_list_statement(
    tenant_id: str,
    include_files: bool,
    extra_where: tuple = (),
):
    """按 include_files 决定是否投影掉 skill_files_json；extra_where 供按绑定 id 取行的分支复用。"""
    statement = (
        select(GeneralSkill)
        .where(GeneralSkill.tenant_id == tenant_id, *extra_where)
        .order_by(GeneralSkill.updated_at.desc())
    )
    if include_files:
        return statement
    return statement.options(
        load_only(*(getattr(GeneralSkill, name) for name in _GENERAL_SKILL_SLIM_COLUMNS))
    )


@router.get(
    "/{slug}", response_model=GeneralSkillRead, dependencies=[Depends(require_agent_scope_viewer)]
)
def get_general_skill(
    slug: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    agent_id: str | None = Query(None),
    # 编辑器两段加载：include_files=0 时只返回元信息 + SKILL.md（markdown 在 slim 列里，
    # 表单可立即渲染）；skill_files/skill_directories 置空，由调用方随后拉完整详情补齐。
    include_files: bool = Query(True),
) -> GeneralSkillRead:
    row = _get_general_skill(db, tenant_id, slug)
    _ensure_general_skill_visible(db, tenant_id, row, agent_id)
    return general_skill_read(row, include_files=include_files)


@router.get("/{slug}/package", dependencies=[Depends(require_agent_scope_viewer)])
def download_general_skill_package(
    slug: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
) -> Response:
    """下载技能 zip 包；包内容与导入格式同构，可直接再导入。"""
    row = _get_general_skill(db, tenant_id, slug)
    _ensure_general_skill_visible(db, tenant_id, row, agent_id)
    # 累计下载次数：广场下载与免登录分享下载都计数
    row.download_count = (row.download_count or 0) + 1
    db.add(row)
    db.commit()
    archive = _general_skill_package_archive(row)
    # 中文技能名不能进 `filename=`（latin-1 头会炸），故用 slug 兜底 + RFC 5987 的
    # `filename*=` 携带真实名，浏览器优先取后者。
    fallback_filename = f"{row.slug}.zip"
    filename = f"{row.name or row.slug}.zip"
    return Response(
        content=archive,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"{fallback_filename}\"; "
                f"filename*=UTF-8''{quote(filename, safe='')}"
            )
        },
    )


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
    _require_gallery_skill_owner_or_admin(row, current_user)
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
    _require_gallery_skill_owner_or_admin(row, current_user)
    row.status = "archived"
    row.updated_at = utc_now()
    db.add(row)
    db.flush()
    ensure_open_gallery_binding(db, tenant_id, "general_skill", row.id, "inactive")
    db.commit()
    db.refresh(row)
    return general_skill_read(row)


@router.post("/{slug}/publish-to-gallery", response_model=GeneralSkillRead)
def publish_general_skill_to_gallery(
    slug: str,
    tenant_id: str = Query(...),
    agent_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> GeneralSkillRead:
    """Promote an agent-private skill to the tenant's open gallery."""
    row = _get_general_skill(db, tenant_id, slug)
    agent = ensure_agent_scope_manager(db, tenant_id, agent_id, current_user)
    if agent is None or agent.is_overall:
        raise HTTPException(status_code=400, detail="A non-overall agent is required")
    if is_open_gallery_resource(db, tenant_id, "general_skill", row):
        return general_skill_read(row)
    if not _private_skill_owned_by_agent(db, tenant_id, row, agent.id):
        raise HTTPException(status_code=403, detail="Only the skill owner can publish it")

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
        revive=True,
    )
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
    row = _get_general_skill_slim(db, tenant_id, slug)
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

    # Open-gallery skill deletion is limited to the creator or an admin. This also
    # fixes the prior defect where the no-agent_id hard-delete branch called
    # require_overall_agent(..., None) and blocked even administrators (issue: an
    # admin could not delete a global resource without passing an overall agent_id).
    _require_gallery_skill_owner_or_admin(row, current_user)
    overall = get_overall_agent(db, tenant_id)
    if overall is None:
        db.delete(row)
        db.commit()
        return {"status": "deleted", "slug": slug}
    if not is_open_gallery_resource(db, tenant_id, "general_skill", row):
        return {"status": "hidden", "slug": slug}
    hide_open_gallery_binding(db, tenant_id, "general_skill", row.id)
    db.commit()
    return {"status": "hidden", "slug": slug}


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
    skill_snapshot = _general_skill_snapshot(skill)
    return _run_general_skill_operation(
        skill_snapshot,
        request,
        model_config,
        current_user.id,
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
    skill_snapshot = _general_skill_snapshot(skill)
    # Validate the explicitly selected model before starting a durable Harness
    # turn. AgentLoop resolves the same config again in its worker session.
    _get_request_model(db, request.tenant_id, request.model_config_id)
    if request.operation == "read":
        model_config = _get_request_model(db, request.tenant_id, request.model_config_id)

        def read_stream_events() -> Iterator[str]:
            response = _run_general_skill_operation(
                skill_snapshot,
                request,
                model_config,
                current_user.id,
            )
            yield _sse("complete", response.model_dump(mode="json"))

        return StreamingResponse(read_stream_events(), media_type="text/event-stream")

    session_id = new_id("session")
    client_turn_id = f"skill-test-{uuid.uuid4().hex}"
    debug_session = ChatSession(
        id=session_id,
        tenant_id=request.tenant_id,
        user_id=current_user.id,
        agent_id=request.agent_id,
        title=f"技能测试 · {skill.name}",
        channel=GENERAL_SKILL_DEBUG_CHANNEL,
    )
    db.add(debug_session)
    db.commit()

    harness_request = ChatTurnRequest(
        tenant_id=request.tenant_id,
        session_id=session_id,
        agent_id=request.agent_id,
        model_config_id=request.model_config_id,
        client_turn_id=client_turn_id,
        user_id=current_user.id,
        message=f"/skill {skill.slug} {request.query.strip()}",
        channel=GENERAL_SKILL_DEBUG_CHANNEL,
        message_visibility="internal",
        debug=True,
    )

    def stream_events() -> Iterator[str]:
        terminal: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)

        def worker() -> None:
            try:
                with Session(engine) as worker_db:
                    response = AgentLoop(worker_db).handle_turn(harness_request)
                    assistant = worker_db.exec(
                        select(Message)
                        .where(
                            Message.tenant_id == request.tenant_id,
                            Message.session_id == session_id,
                            Message.role == "assistant",
                        )
                        .order_by(Message.created_at.desc())
                    ).first()
                    terminal.put(
                        (
                            "complete",
                            _harness_skill_run_response(
                                skill.slug,
                                response,
                                dict(assistant.metadata_json or {}) if assistant else {},
                            ),
                        )
                    )
            except Exception as exc:  # pragma: no cover - defensive stream boundary
                terminal.put(("error", {"message": str(exc)}))

        threading.Thread(target=worker, daemon=True).start()
        yield _sse(
            "stream_started",
            {
                "skill_slug": skill_snapshot.slug,
                "operation": request.operation,
                "execution_engine": "harness_v2",
                "session_id": session_id,
                "client_turn_id": client_turn_id,
                "idle_timeout_seconds": GENERAL_SKILL_STREAM_IDLE_TIMEOUT_SECONDS,
            },
        )
        last_event_at = time.monotonic()
        last_heartbeat_at = 0.0
        cursor: tuple[object, str] | None = None
        pending_terminal: tuple[str, object] | None = None
        with Session(engine) as poll_db:
            while True:
                rows = _skill_debug_events_after(
                    poll_db,
                    request.tenant_id,
                    session_id,
                    cursor,
                )
                for row in rows:
                    cursor = (row.created_at, row.id)
                    last_event_at = time.monotonic()
                    yield _sse("trace", _skill_debug_trace(row))

                if pending_terminal is None:
                    try:
                        pending_terminal = terminal.get_nowait()
                    except queue.Empty:
                        # The worker is still running; keep polling persisted trace events.
                        pass
                if pending_terminal is not None and not rows:
                    event, payload = pending_terminal
                    if isinstance(payload, GeneralSkillRunResponse):
                        payload = payload.model_dump(mode="json")
                    yield _sse(event, payload)
                    return

                now = time.monotonic()
                if now - last_event_at > GENERAL_SKILL_STREAM_IDLE_TIMEOUT_SECONDS:
                    yield _sse(
                        "error",
                        {
                            "message": "技能测试 10 分钟内未收到新的执行事件，请检查模型配置或稍后重试。",
                            "code": "general_skill_stream_timeout",
                            "session_id": session_id,
                            "client_turn_id": client_turn_id,
                        },
                    )
                    return
                if now - last_heartbeat_at >= 5:
                    last_heartbeat_at = now
                    yield _sse(
                        "heartbeat",
                        {
                            "phase": "harness_v2",
                            "session_id": session_id,
                            "client_turn_id": client_turn_id,
                        },
                    )
                time.sleep(0.1)

    return StreamingResponse(stream_events(), media_type="text/event-stream")


def _skill_debug_events_after(
    db: Session,
    tenant_id: str,
    session_id: str,
    cursor: tuple[object, str] | None,
) -> list[AgentEvent]:
    statement = select(AgentEvent).where(
        AgentEvent.tenant_id == tenant_id,
        AgentEvent.session_id == session_id,
    )
    if cursor is not None:
        created_at, event_id = cursor
        statement = statement.where(
            (AgentEvent.created_at > created_at)
            | ((AgentEvent.created_at == created_at) & (AgentEvent.id > event_id))
        )
    db.expire_all()
    return list(
        db.exec(statement.order_by(AgentEvent.created_at, AgentEvent.id).limit(200)).all()
    )


def _skill_debug_trace(row: AgentEvent) -> dict[str, object]:
    payload = dict(row.payload_json or {})
    phase = str(payload.get("phase") or row.event_type)
    message = str(payload.get("text") or payload.get("message") or phase)
    return {
        **payload,
        "event_type": row.event_type,
        "phase": phase,
        "message": message,
        "event_id": row.id,
        "execution_engine": "harness_v2",
    }


def _harness_skill_run_response(
    slug: str,
    response: ChatTurnResponse,
    assistant_metadata: dict[str, object],
) -> GeneralSkillRunResponse:
    citations = assistant_metadata.get("knowledge_citations")
    artifacts = assistant_metadata.get("harness_artifacts")
    success = not bool(response.runtime_error_code)
    return GeneralSkillRunResponse(
        skill_slug=slug,
        operation="execute",
        structured_result={
            "success": success,
            "execution_engine": "harness_v2",
            "session_id": response.session_id,
            "runtime_error_code": response.runtime_error_code,
            "step_result": (
                response.step_result.model_dump(mode="json")
                if response.step_result is not None
                else None
            ),
            "citations": citations if isinstance(citations, list) else [],
        },
        artifacts=artifacts if isinstance(artifacts, list) else [],
        stderr=response.reply if response.runtime_error_code else "",
        reply=response.reply,
    )


def _run_general_skill_operation(
    skill: GeneralSkillRuntimeSnapshot,
    request: GeneralSkillRunRequest,
    model_config: ModelConfig,
    user_id: str,
    event_sink: Callable[[dict[str, object]], None] | None = None,
) -> GeneralSkillRunResponse:
    if request.operation == "read":
        response = GeneralSkillReader().read(skill, request.query, model_config)
        if event_sink is not None:
            for trace_item in response.execution_trace:
                event_sink(trace_item)
        return response
    return GeneralSkillRunner().run(
        skill,
        request.query,
        model_config,
        user_id,
        request.max_attempts,
        event_sink,
    )


def _get_general_skill(db: Session, tenant_id: str, slug: str) -> GeneralSkill:
    ensure_tenant(db, tenant_id)
    row = db.exec(
        select(GeneralSkill).where(GeneralSkill.tenant_id == tenant_id, GeneralSkill.slug == slug)
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="General skill not found")
    return row


def _get_general_skill_slim(db: Session, tenant_id: str, slug: str) -> GeneralSkill:
    """_get_general_skill 的裁剪版：投影掉 skill_files_json。

    供不需要文件内容的操作（删除）使用——单行文件包有 MB 级体量，TOAST 解压
    是删除接口延迟的大头；metadata / status 等元信息在 slim 列里都保留。
    """
    ensure_tenant(db, tenant_id)
    row = db.exec(
        _general_skill_list_statement(
            tenant_id,
            include_files=False,
            extra_where=(GeneralSkill.slug == slug,),
        )
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="General skill not found")
    return row


def _require_gallery_skill_owner_or_admin(row: GeneralSkill, current_user: User) -> None:
    """Open-gallery general_skill mutation/deletion is limited to the creator or an admin.

    After decoupling, any member may *publish* into the open gallery, but removing or
    archiving a gallery skill is restricted to its creator or a tenant administrator so a
    member cannot take down someone else's published skill.
    """
    if is_admin_user(current_user):
        return
    owner_id = (row.metadata_json or {}).get("owner_user_id")
    if owner_id == current_user.id:
        return
    raise HTTPException(
        status_code=403,
        detail="Only the creator or administrator can manage this open-gallery skill",
    )


def _private_skill_owned_by_agent(
    db: Session, tenant_id: str, row: GeneralSkill, agent_id: str
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
        )
    ).first()
    binding_metadata = binding.metadata_json if binding else {}
    return bool(
        binding
        and binding_metadata.get("scope") == "agent_private"
        and binding_metadata.get("owner_agent_id") == agent_id
    )


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


def _general_skill_snapshot(row: GeneralSkill) -> GeneralSkillRuntimeSnapshot:
    return local_runtime_snapshot(row)


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


def _replace_skill_markdown_in_package(
    row: GeneralSkill,
    markdown: str,
) -> list[GeneralSkillFile]:
    existing = [
        GeneralSkillFile.model_validate(item) for item in _skill_files_or_markdown(row)
    ]
    files = _normalize_skill_files(existing, None)
    updated: list[GeneralSkillFile] = []
    for file in files:
        if file.path.rsplit("/", 1)[-1].lower() == "skill.md":
            encoded = markdown.encode("utf-8")
            updated.append(
                file.model_copy(
                    update={
                        "content": markdown,
                        "size": len(encoded),
                        "mime_type": file.mime_type or "text/markdown",
                    }
                )
            )
        else:
            updated.append(file)
    return updated


def _validate_skill_package_references(
    files: list[GeneralSkillFile], *, allow_missing: bool = False
) -> None:
    """校验 SKILL.md 里 `references/...` 形式的本地引用是否都进包了。

    `allow_missing=True` 表示用户在二次确认弹窗里选择了「仍要导入」——缺文档的技能
    会降级（按需读取时读不到），但不再阻断导入。
    """
    skill_file = _find_skill_file(files)
    if skill_file is None or allow_missing:
        return
    package_paths = {file.path.replace("\\", "/").removeprefix("./") for file in files}
    content = skill_file.content
    referenced_paths: set[str] = set()
    for match in LOCAL_REFERENCE_PATTERN.finditer(content):
        # 通配引用（`references/products/*.md`）匹配会在 `*` 前停住，按后缀判断跳过
        if content[match.end() : match.end() + 1] in {"*", "{"}:
            continue
        raw_path = unquote(match.group(0)).replace("\\", "/")
        raw_path = raw_path.split("#", 1)[0].split("?", 1)[0]
        raw_path = raw_path.rstrip(")]}.,;:!?，。；：！？")
        if any(marker in raw_path for marker in ("*", "{", "}")):
            continue
        # 尾部的 `/` 是「目录引用」标记（如 `references/products/`），**不能** strip 掉：
        # 否则目录会被当成一个不存在的文件名去比对，包再完整也会误报缺失。
        normalized = raw_path.removeprefix("./").lstrip("/")
        if not normalized or normalized.endswith("/"):
            continue
        referenced_paths.add(normalized)
    missing = sorted(referenced_paths - package_paths)
    if missing:
        # 结构化 detail：前端据此判定「缺引用」并弹二次确认，确认后带
        # allow_missing_references=true 重试。message 保持原英文文案不变。
        raise HTTPException(
            status_code=400,
            detail={
                "code": GENERAL_SKILL_MISSING_REFERENCES_CODE,
                "message": (
                    "General skill package is missing files referenced by SKILL.md: "
                    + ", ".join(missing)
                ),
                "paths": missing,
            },
        )


def _skill_directories_from_values(
    values: list[str],
    files: list[GeneralSkillFile],
) -> list[str]:
    file_paths = {file.path for file in files}
    directories: list[str] = []
    seen: set[str] = set()
    for value in values:
        path = _clean_package_path(value)
        if path in file_paths or any(path.startswith(f"{file_path}/") for file_path in file_paths):
            raise HTTPException(
                status_code=400,
                detail=f"General skill directory conflicts with a file path: {value}",
            )
        if path not in seen:
            seen.add(path)
            directories.append(path)
    return directories


def _skill_directories(row: GeneralSkill) -> list[str]:
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    values = metadata.get("skill_directories")
    if not isinstance(values, list):
        return []
    files = [GeneralSkillFile.model_validate(item) for item in _skill_files_or_markdown(row)]
    valid_values = [str(value) for value in values if isinstance(value, str) and value.strip()]
    try:
        return _skill_directories_from_values(valid_values, files)
    except HTTPException:
        return []


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


def _general_skill_package_archive(row: GeneralSkill) -> bytes:
    """把库里存的技能文件重新打成 zip（与导入格式同构，可直接再导入）。

    两处刻意的取舍：

    - **按 UTF-8 回写**。存储层把每个文件都存成文本（导入时 `_files_from_zip` 走
      `_decode_text`，非 UTF-8 字节已被 `errors="replace"` 破坏），所以二进制附件天然
      有损。这是既有存储模型的限制，不是本端点引入的。
    - **zip 根目录即技能根目录**。导入时 `_normalize_skill_files` 已经剥掉 SKILL.md 所在的
      外层目录，库里存的 path 是技能根相对路径，因此导出的包再导入一次是等价的。
    """
    files = [GeneralSkillFile.model_validate(item) for item in _skill_files_or_markdown(row)]
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        # 空目录不会从文件列表里推导出来，必须显式写目录项才不会丢
        for directory in _skill_directories(row):
            archive.writestr(f"{directory}/", b"")
        for file in files:
            archive.writestr(_clean_package_path(file.path), (file.content or "").encode("utf-8"))
    return buffer.getvalue()


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
