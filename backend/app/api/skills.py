from __future__ import annotations

import json
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import AgentEvent, ModelConfig, Skill, SkillFeedback, SkillVersion, Tool, utc_now
from app.llm import LLMError
from app.security.tenant import ensure_tenant
from app.skills import SkillDistiller, SkillEditor
from app.skills.skill_schema import (
    SkillCard,
    SkillCreateRequest,
    SkillDistillRequest,
    SkillDistillResponse,
    SkillRead,
    SkillRewriteRequest,
    SkillRewriteResponse,
    SkillVersionRead,
    SkillUpdateRequest,
)
from app.skills.step_ids import skill_card_with_unique_step_ids

router = APIRouter(prefix="/api/enterprise/skills", tags=["enterprise:skills"])


def skill_read(row: Skill, stats: dict[str, dict[str, float | int]] | None = None) -> SkillRead:
    skill_stats = (stats or {}).get(row.skill_id, {})
    content, _warnings = skill_card_with_unique_step_ids(SkillCard.model_validate(row.content_json))
    return SkillRead(
        id=row.id,
        tenant_id=row.tenant_id,
        skill_id=row.skill_id,
        version=row.version,
        name=row.name,
        business_domain=row.business_domain,
        description=row.description,
        content=content,
        status=row.status,
        call_count=int(skill_stats.get("call_count", 0)),
        positive_feedback_count=int(skill_stats.get("positive_feedback_count", 0)),
        negative_feedback_count=int(skill_stats.get("negative_feedback_count", 0)),
        positive_rate=float(skill_stats.get("positive_rate", 0.0)),
        negative_rate=float(skill_stats.get("negative_rate", 0.0)),
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def skill_version_read(
    row: SkillVersion, stats: dict[str, dict[str, float | int]] | None = None
) -> SkillVersionRead:
    skill_stats = _stats_for(stats or {}, row.skill_id, row.version)
    content, _warnings = skill_card_with_unique_step_ids(SkillCard.model_validate(row.content_json))
    return SkillVersionRead(
        id=row.id,
        tenant_id=row.tenant_id,
        skill_id=row.skill_id,
        version=row.version,
        name=row.name,
        business_domain=row.business_domain,
        description=row.description,
        content=content,
        status=row.status,
        call_count=int(skill_stats.get("call_count", 0)),
        positive_feedback_count=int(skill_stats.get("positive_feedback_count", 0)),
        negative_feedback_count=int(skill_stats.get("negative_feedback_count", 0)),
        positive_rate=float(skill_stats.get("positive_rate", 0.0)),
        negative_rate=float(skill_stats.get("negative_rate", 0.0)),
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


@router.get("", response_model=list[SkillRead])
def list_skills(tenant_id: str = Query(...), db: Session = Depends(get_session)) -> list[SkillRead]:
    ensure_tenant(db, tenant_id)
    rows = db.exec(select(Skill).where(Skill.tenant_id == tenant_id)).all()
    stats = _skill_stats(db, tenant_id)
    return [skill_read(row, stats) for row in rows]


@router.post("", response_model=SkillRead)
def create_skill(request: SkillCreateRequest, db: Session = Depends(get_session)) -> SkillRead:
    ensure_tenant(db, request.tenant_id)
    existing = db.exec(
        select(Skill).where(
            Skill.tenant_id == request.tenant_id, Skill.skill_id == request.content.skill_id
        )
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Skill ID already exists for this tenant")
    normalized_content, _warnings = skill_card_with_unique_step_ids(request.content)
    content = normalized_content.model_dump()
    row = Skill(
        tenant_id=request.tenant_id,
        skill_id=normalized_content.skill_id,
        version=normalized_content.version,
        name=normalized_content.name,
        business_domain=normalized_content.business_domain,
        description=normalized_content.description,
        content_json=content,
        status=request.status,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    _upsert_skill_version(db, row)
    return skill_read(row, _skill_stats(db, request.tenant_id))


@router.get("/{skill_id}", response_model=SkillRead)
def get_skill(skill_id: str, tenant_id: str = Query(...), db: Session = Depends(get_session)) -> SkillRead:
    row = _get_skill(db, tenant_id, skill_id)
    return skill_read(row, _skill_stats(db, tenant_id))


@router.put("/{skill_id}", response_model=SkillRead)
def update_skill(skill_id: str, request: SkillUpdateRequest, db: Session = Depends(get_session)) -> SkillRead:
    if request.content.skill_id != skill_id:
        raise HTTPException(status_code=400, detail="Path skill_id must match content.skill_id")
    row = _get_skill(db, request.tenant_id, skill_id)
    normalized_content, _warnings = skill_card_with_unique_step_ids(request.content)
    row.version = normalized_content.version
    row.name = normalized_content.name
    row.business_domain = normalized_content.business_domain
    row.description = normalized_content.description
    row.content_json = normalized_content.model_dump()
    if request.status:
        row.status = request.status
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    _upsert_skill_version(db, row)
    return skill_read(row, _skill_stats(db, request.tenant_id))


@router.post("/{skill_id}/publish", response_model=SkillRead)
def publish_skill(skill_id: str, tenant_id: str = Query(...), db: Session = Depends(get_session)) -> SkillRead:
    row = _get_skill(db, tenant_id, skill_id)
    row.status = "published"
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    _upsert_skill_version(db, row)
    return skill_read(row, _skill_stats(db, tenant_id))


@router.post("/{skill_id}/archive", response_model=SkillRead)
def archive_skill(skill_id: str, tenant_id: str = Query(...), db: Session = Depends(get_session)) -> SkillRead:
    row = _get_skill(db, tenant_id, skill_id)
    row.status = "archived"
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    _upsert_skill_version(db, row)
    return skill_read(row, _skill_stats(db, tenant_id))


@router.delete("/{skill_id}")
def delete_skill(
    skill_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
) -> dict[str, str]:
    row = _get_skill(db, tenant_id, skill_id)
    feedback_rows = db.exec(
        select(SkillFeedback).where(
            SkillFeedback.tenant_id == tenant_id,
            SkillFeedback.skill_id == skill_id,
        )
    ).all()
    for feedback in feedback_rows:
        db.delete(feedback)
    version_rows = db.exec(
        select(SkillVersion).where(SkillVersion.tenant_id == tenant_id, SkillVersion.skill_id == skill_id)
    ).all()
    for version_row in version_rows:
        db.delete(version_row)
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


@router.get("/{skill_id}/versions", response_model=list[SkillVersionRead])
def list_skill_versions(
    skill_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
) -> list[SkillVersionRead]:
    row = _get_skill(db, tenant_id, skill_id)
    _upsert_skill_version(db, row)
    rows = db.exec(
        select(SkillVersion)
        .where(SkillVersion.tenant_id == tenant_id, SkillVersion.skill_id == skill_id)
        .order_by(SkillVersion.created_at.desc())
    ).all()
    stats = _skill_stats(db, tenant_id)
    return [skill_version_read(version_row, stats) for version_row in rows]


@router.post("/distill", response_model=SkillDistillResponse)
def distill_skill(request: SkillDistillRequest, db: Session = Depends(get_session)) -> SkillDistillResponse:
    ensure_tenant(db, request.tenant_id)
    model_config = _get_default_model(db, request.tenant_id)
    request = _with_available_tools(db, request)
    try:
        return SkillDistiller().distill(request, model_config)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/distill/stream")
def distill_skill_stream(request: SkillDistillRequest) -> StreamingResponse:
    def stream_events() -> Iterator[str]:
        with Session(get_session_engine()) as db:
            ensure_tenant(db, request.tenant_id)
            model_config = _get_default_model(db, request.tenant_id)
            enriched_request = _with_available_tools(db, request)
            yield _sse("status", {"text": "正在调用模型生成新技能"})
            for item in SkillDistiller().stream_text(enriched_request, model_config):
                yield _sse(item["event"], item["data"])

    return StreamingResponse(stream_events(), media_type="text/event-stream")


@router.post("/{skill_id}/rewrite/stream")
def rewrite_skill_stream(skill_id: str, request: SkillRewriteRequest) -> StreamingResponse:
    if request.current_skill.skill_id != skill_id:
        raise HTTPException(status_code=400, detail="Path skill_id must match current_skill.skill_id")

    def stream_events() -> Iterator[str]:
        with Session(get_session_engine()) as db:
            ensure_tenant(db, request.tenant_id)
            model_config = _get_default_model(db, request.tenant_id)
            yield _sse("status", {"text": "正在调用模型分析改写要求"})
            for item in SkillEditor().stream_text(request, model_config):
                yield _sse(item["event"], item["data"])

    return StreamingResponse(stream_events(), media_type="text/event-stream")


@router.post("/{skill_id}/rewrite", response_model=SkillRewriteResponse)
def rewrite_skill(
    skill_id: str,
    request: SkillRewriteRequest,
    db: Session = Depends(get_session),
) -> SkillRewriteResponse:
    if request.current_skill.skill_id != skill_id:
        raise HTTPException(status_code=400, detail="Path skill_id must match current_skill.skill_id")
    ensure_tenant(db, request.tenant_id)
    model_config = _get_default_model(db, request.tenant_id)
    try:
        return SkillEditor().rewrite(request, model_config)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def get_session_engine():
    from app.db import engine

    return engine


def _get_default_model(db: Session, tenant_id: str) -> ModelConfig:
    model_config = db.exec(
        select(ModelConfig).where(
            ModelConfig.tenant_id == tenant_id,
            ModelConfig.is_default == True,  # noqa: E712
            ModelConfig.enabled == True,  # noqa: E712
        )
    ).first()
    if not model_config:
        raise HTTPException(status_code=400, detail="No enabled default model config")
    return model_config


def _with_available_tools(db: Session, request: SkillDistillRequest) -> SkillDistillRequest:
    tools = db.exec(
        select(Tool).where(Tool.tenant_id == request.tenant_id, Tool.enabled == True)  # noqa: E712
    ).all()
    available_tools = [
        *request.available_tools,
        *[
            {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
            for tool in tools
        ],
    ]
    return request.model_copy(update={"available_tools": available_tools})


def _sse(event: object, data: object) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _skill_stats(db: Session, tenant_id: str) -> dict[str, dict[str, float | int]]:
    stats: dict[str, dict[str, float | int]] = {}
    current_versions = {
        row.skill_id: row.version
        for row in db.exec(select(Skill).where(Skill.tenant_id == tenant_id)).all()
    }
    events = db.exec(
        select(AgentEvent).where(
            AgentEvent.tenant_id == tenant_id,
            AgentEvent.event_type.in_(["skill_started", "skill_suspended", "skill_resumed"]),  # type: ignore[attr-defined]
        )
    ).all()
    for event in events:
        payload = event.payload_json or {}
        skill_id = str(payload.get("to_skill_id") or "")
        if not skill_id:
            continue
        skill_version = str(payload.get("to_skill_version") or payload.get("skill_version") or "") or current_versions.get(skill_id)
        _increment_call(stats, skill_id, skill_version)

    feedback_rows = db.exec(
        select(SkillFeedback).where(SkillFeedback.tenant_id == tenant_id)
    ).all()
    for feedback in feedback_rows:
        skill_version = feedback.skill_version or current_versions.get(feedback.skill_id)
        entries = [stats.setdefault(feedback.skill_id, _empty_stats())]
        if skill_version:
            entries.append(stats.setdefault(_stats_key(feedback.skill_id, skill_version), _empty_stats()))
        for entry in entries:
            if feedback.rating == "up":
                entry["positive_feedback_count"] = int(entry["positive_feedback_count"]) + 1
            elif feedback.rating == "down":
                entry["negative_feedback_count"] = int(entry["negative_feedback_count"]) + 1

    for entry in stats.values():
        positive = int(entry["positive_feedback_count"])
        negative = int(entry["negative_feedback_count"])
        total = positive + negative
        entry["positive_rate"] = round(positive / total, 4) if total else 0.0
        entry["negative_rate"] = round(negative / total, 4) if total else 0.0
    return stats


def _increment_call(stats: dict[str, dict[str, float | int]], skill_id: str, version: str | None) -> None:
    entries = [stats.setdefault(skill_id, _empty_stats())]
    if version:
        entries.append(stats.setdefault(_stats_key(skill_id, version), _empty_stats()))
    for entry in entries:
        entry["call_count"] = int(entry["call_count"]) + 1


def _stats_key(skill_id: str, version: str) -> str:
    return f"{skill_id}@{version}"


def _stats_for(stats: dict[str, dict[str, float | int]], skill_id: str, version: str) -> dict[str, float | int]:
    return stats.get(_stats_key(skill_id, version), stats.get(skill_id, {}))


def _upsert_skill_version(db: Session, row: Skill) -> SkillVersion:
    existing = db.exec(
        select(SkillVersion).where(
            SkillVersion.tenant_id == row.tenant_id,
            SkillVersion.skill_id == row.skill_id,
            SkillVersion.version == row.version,
        )
    ).first()
    if existing:
        existing.name = row.name
        existing.business_domain = row.business_domain
        existing.description = row.description
        existing.content_json = row.content_json
        existing.status = row.status
        existing.updated_at = utc_now()
        db.add(existing)
        db.commit()
        db.refresh(existing)
        return existing
    version_row = SkillVersion(
        tenant_id=row.tenant_id,
        skill_id=row.skill_id,
        version=row.version,
        name=row.name,
        business_domain=row.business_domain,
        description=row.description,
        content_json=row.content_json,
        status=row.status,
    )
    db.add(version_row)
    db.commit()
    db.refresh(version_row)
    return version_row


def _empty_stats() -> dict[str, float | int]:
    return {
        "call_count": 0,
        "positive_feedback_count": 0,
        "negative_feedback_count": 0,
        "positive_rate": 0.0,
        "negative_rate": 0.0,
    }


def _get_skill(db: Session, tenant_id: str, skill_id: str) -> Skill:
    ensure_tenant(db, tenant_id)
    row = db.exec(
        select(Skill).where(Skill.tenant_id == tenant_id, Skill.skill_id == skill_id)
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Skill not found")
    return row
