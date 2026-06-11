from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import delete
from sqlmodel import Session, select

from app.db import engine
from app.db.models import (
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeDiscoverySuggestion,
    KnowledgeDocument,
    KnowledgeIngestJob,
    ModelConfig,
    Skill,
    Tool,
    utc_now,
)
from app.knowledge.parser import KnowledgeParseError, extract_text
from app.knowledge.schema import (
    KnowledgeBucketRead,
    KnowledgeChunkRead,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
)
from app.llm import LLMClient, LLMError
from app.skills.skill_schema import SkillCard


PROMPT_DIR = Path(__file__).resolve().parents[1] / "llm" / "prompts"
BUCKET_PROMPT = PROMPT_DIR / "knowledge_bucket_prompt.md"
DISCOVERY_PROMPT = PROMPT_DIR / "knowledge_discovery_prompt.md"
SEARCH_PROMPT = PROMPT_DIR / "knowledge_search_prompt.md"

CHUNK_CHARS = 1800
BUCKET_SECTION_CHARS = 6000


@dataclass
class IngestPayload:
    tenant_id: str
    filename: str
    content_base64: str
    title: str | None = None
    metadata: dict[str, Any] | None = None


class KnowledgeService:
    def __init__(self, db: Session):
        self.db = db

    def create_ingest_job(self, payload: IngestPayload) -> KnowledgeIngestJob:
        job = KnowledgeIngestJob(
            tenant_id=payload.tenant_id,
            filename=payload.filename,
            status="queued",
            stage="queued",
            progress=0.0,
            metadata_json={
                "content_base64": payload.content_base64,
                "title": payload.title,
                "metadata": payload.metadata or {},
            },
        )
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)
        return job

    def run_ingest_job(self, job_id: str) -> None:
        with Session(engine) as db:
            service = KnowledgeService(db)
            service._run_ingest_job(job_id)

    def _run_ingest_job(self, job_id: str) -> None:
        job = self.db.get(KnowledgeIngestJob, job_id)
        if not job:
            return
        self._update_job(job, status="running", stage="parsing", progress=0.05, started_at=utc_now())
        metadata = job.metadata_json or {}
        try:
            content = base64.b64decode(str(metadata.get("content_base64") or ""))
            text, file_type = extract_text(job.filename, content)
            normalized_text = _normalize_text(text)
            if not normalized_text:
                raise KnowledgeParseError("文档没有可用文本内容。")

            document = KnowledgeDocument(
                tenant_id=job.tenant_id,
                filename=job.filename,
                file_type=file_type,
                title=str(metadata.get("title") or Path(job.filename).stem),
                status="processing",
                metadata_json={
                    **(metadata.get("metadata") if isinstance(metadata.get("metadata"), dict) else {}),
                    "char_count": len(normalized_text),
                },
            )
            self.db.add(document)
            self.db.commit()
            self.db.refresh(document)
            job.document_id = document.id
            self._update_job(job, stage="bucketing", progress=0.25)

            buckets = self._build_buckets(job.tenant_id, document, normalized_text)
            self._update_job(job, stage="chunking", progress=0.55)
            chunk_count = self._build_chunks(job.tenant_id, document, buckets)

            document.bucket_count = len(buckets)
            document.chunk_count = chunk_count
            document.status = "ready"
            document.updated_at = utc_now()
            self.db.add(document)
            self._update_job(job, stage="discovering", progress=0.78)

            self._discover_from_document(job.tenant_id, document, buckets)
            self._update_job(job, status="succeeded", stage="done", progress=1.0, finished_at=utc_now())
            self._clear_embedded_content(job)
        except Exception as exc:  # noqa: BLE001 - persist stable job failure.
            if job.document_id:
                document = self.db.get(KnowledgeDocument, job.document_id)
                if document:
                    document.status = "failed"
                    document.error = str(exc)
                    document.updated_at = utc_now()
                    self.db.add(document)
            self._update_job(job, status="failed", stage="failed", error=str(exc), finished_at=utc_now())
            self._clear_embedded_content(job)

    def search(self, request: KnowledgeSearchRequest, model_config: ModelConfig | None = None) -> KnowledgeSearchResponse:
        query = request.query.strip()
        if not query:
            return KnowledgeSearchResponse()
        stmt = select(KnowledgeBucket).where(KnowledgeBucket.tenant_id == request.tenant_id)
        if request.document_ids:
            stmt = stmt.where(KnowledgeBucket.document_id.in_(request.document_ids))
        buckets = self.db.exec(stmt.order_by(KnowledgeBucket.created_at.desc())).all()
        if not buckets:
            return KnowledgeSearchResponse(trace=[{"phase": "no_buckets", "message": "没有可检索的知识桶"}])

        selected_ids: list[str] = []
        trace: list[dict[str, Any]] = [{"phase": "scan_buckets", "message": "正在检索知识"}]
        if model_config:
            selected_ids = self._select_buckets_with_llm(query, buckets, request.max_buckets, model_config, trace)
        if not selected_ids:
            selected_ids = [bucket.id for bucket in buckets[: request.max_buckets]]
            trace.append({"phase": "fallback_buckets", "message": "模型未返回桶选择，使用最近知识桶"})

        selected_buckets = [bucket for bucket in buckets if bucket.id in set(selected_ids)]
        chunks = self._load_chunks_for_buckets(request.tenant_id, selected_ids, request.max_chunks)
        trace.extend(
            [
                {"phase": "expand_buckets", "message": "展开知识桶", "bucket_ids": selected_ids},
                {"phase": "read_chunks", "message": "读取知识片段", "chunk_count": len(chunks)},
            ]
        )
        return KnowledgeSearchResponse(
            selected_buckets=[bucket_read(row) for row in selected_buckets],
            chunks=[chunk_read(row) for row in chunks],
            trace=trace,
        )

    def confirm_discovery(self, suggestion: KnowledgeDiscoverySuggestion) -> dict[str, Any]:
        payload = suggestion.payload_json or {}
        if suggestion.suggestion_type == "tool":
            created = self._confirm_tool(suggestion, payload)
        elif suggestion.suggestion_type == "skill":
            created = self._confirm_skill(suggestion, payload)
        else:
            created = {"status": "confirmed"}
        suggestion.status = "confirmed"
        suggestion.updated_at = utc_now()
        self.db.add(suggestion)
        self.db.commit()
        return created

    def reject_discovery(self, suggestion: KnowledgeDiscoverySuggestion) -> None:
        suggestion.status = "rejected"
        suggestion.updated_at = utc_now()
        self.db.add(suggestion)
        self.db.commit()

    def _build_buckets(
        self,
        tenant_id: str,
        document: KnowledgeDocument,
        text: str,
    ) -> list[KnowledgeBucket]:
        sections = _split_sections(text)
        model_config = self._default_model_config(tenant_id)
        llm_buckets = self._bucket_with_llm(sections, model_config) if model_config else []
        bucket_specs = llm_buckets or _fallback_bucket_specs(sections)
        self.db.exec(delete(KnowledgeBucket).where(KnowledgeBucket.document_id == document.id))
        self.db.exec(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))
        self.db.exec(delete(KnowledgeDiscoverySuggestion).where(KnowledgeDiscoverySuggestion.document_id == document.id))
        rows: list[KnowledgeBucket] = []
        for index, spec in enumerate(bucket_specs):
            content = str(spec.get("content") or "")
            if not content and isinstance(spec.get("section_indexes"), list):
                content = "\n\n".join(
                    sections[item]
                    for item in spec["section_indexes"]
                    if isinstance(item, int) and 0 <= item < len(sections)
                )
            content = content or sections[min(index, len(sections) - 1)]
            row = KnowledgeBucket(
                tenant_id=tenant_id,
                document_id=document.id,
                bucket_key=str(spec.get("bucket_key") or f"bucket_{index + 1}"),
                title=str(spec.get("title") or f"知识桶 {index + 1}"),
                summary=str(spec.get("summary") or content[:300]),
                token_estimate=max(1, len(content) // 2),
                metadata_json={"content": content[:BUCKET_SECTION_CHARS]},
            )
            self.db.add(row)
            rows.append(row)
        self.db.commit()
        for row in rows:
            self.db.refresh(row)
        return rows

    def _build_chunks(self, tenant_id: str, document: KnowledgeDocument, buckets: list[KnowledgeBucket]) -> int:
        count = 0
        for bucket in buckets:
            content = str((bucket.metadata_json or {}).get("content") or "")
            parts = _chunk_text(content, CHUNK_CHARS)
            for index, part in enumerate(parts):
                row = KnowledgeChunk(
                    tenant_id=tenant_id,
                    document_id=document.id,
                    bucket_id=bucket.id,
                    chunk_index=index,
                    content=part,
                    summary=part[:240],
                    source_ref=f"{document.filename} / {bucket.title} / chunk {index + 1}",
                )
                self.db.add(row)
                count += 1
        self.db.commit()
        return count

    def _discover_from_document(
        self,
        tenant_id: str,
        document: KnowledgeDocument,
        buckets: list[KnowledgeBucket],
    ) -> None:
        model_config = self._default_model_config(tenant_id)
        if not model_config:
            return
        payload = {
            "document": {
                "id": document.id,
                "filename": document.filename,
                "title": document.title,
                "file_type": document.file_type,
            },
            "buckets": [
                {
                    "id": bucket.id,
                    "title": bucket.title,
                    "summary": bucket.summary,
                    "excerpt": str((bucket.metadata_json or {}).get("content") or "")[:2400],
                }
                for bucket in buckets
            ],
        }
        try:
            raw = LLMClient(model_config).generate_json(DISCOVERY_PROMPT.read_text(encoding="utf-8"), payload)
        except (LLMError, Exception):
            return
        discoveries = raw.get("discoveries") if isinstance(raw, dict) else None
        if not isinstance(discoveries, list):
            return
        for item in discoveries:
            if not isinstance(item, dict):
                continue
            suggestion_type = str(item.get("suggestion_type") or "").strip()
            if suggestion_type not in {"skill", "tool", "warning"}:
                continue
            title = str(item.get("title") or "").strip() or "未命名建议"
            row = KnowledgeDiscoverySuggestion(
                tenant_id=tenant_id,
                document_id=document.id,
                bucket_id=_optional_str(item.get("bucket_id")),
                suggestion_type=suggestion_type,
                title=title,
                payload_json=item.get("payload") if isinstance(item.get("payload"), dict) else {},
                source_refs_json=item.get("source_refs") if isinstance(item.get("source_refs"), list) else [],
                reason=_optional_str(item.get("reason")),
            )
            self.db.add(row)
        self.db.commit()

    def _bucket_with_llm(
        self, sections: list[str], model_config: ModelConfig | None
    ) -> list[dict[str, Any]]:
        if not model_config:
            return []
        payload = {
            "sections": [
                {"index": index, "text": section[:BUCKET_SECTION_CHARS]}
                for index, section in enumerate(sections[:40])
            ]
        }
        try:
            raw = LLMClient(model_config).generate_json(BUCKET_PROMPT.read_text(encoding="utf-8"), payload)
        except (LLMError, Exception):
            return []
        buckets = raw.get("buckets") if isinstance(raw, dict) else None
        return [item for item in buckets if isinstance(item, dict)] if isinstance(buckets, list) else []

    def _select_buckets_with_llm(
        self,
        query: str,
        buckets: list[KnowledgeBucket],
        max_buckets: int,
        model_config: ModelConfig,
        trace: list[dict[str, Any]],
    ) -> list[str]:
        payload = {
            "query": query,
            "max_buckets": max_buckets,
            "buckets": [
                {
                    "id": bucket.id,
                    "title": bucket.title,
                    "summary": bucket.summary,
                    "document_id": bucket.document_id,
                }
                for bucket in buckets[:80]
            ],
        }
        try:
            raw = LLMClient(model_config).generate_json(SEARCH_PROMPT.read_text(encoding="utf-8"), payload)
        except (LLMError, Exception) as exc:
            trace.append({"phase": "bucket_selection_failed", "message": str(exc)})
            return []
        ids = raw.get("selected_bucket_ids") if isinstance(raw, dict) else None
        if not isinstance(ids, list):
            return []
        allowed = {bucket.id for bucket in buckets}
        return [str(item) for item in ids if str(item) in allowed][:max_buckets]

    def _load_chunks_for_buckets(self, tenant_id: str, bucket_ids: list[str], max_chunks: int) -> list[KnowledgeChunk]:
        if not bucket_ids:
            return []
        return self.db.exec(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.tenant_id == tenant_id, KnowledgeChunk.bucket_id.in_(bucket_ids))
            .order_by(KnowledgeChunk.bucket_id, KnowledgeChunk.chunk_index)
            .limit(max_chunks)
        ).all()

    def _confirm_tool(self, suggestion: KnowledgeDiscoverySuggestion, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name") or payload.get("tool_name") or "").strip()
        if not name:
            raise ValueError("工具建议缺少 name。")
        existing = self.db.exec(
            select(Tool).where(Tool.tenant_id == suggestion.tenant_id, Tool.name == name)
        ).first()
        if existing:
            existing.enabled = True
            existing.updated_at = utc_now()
            self.db.add(existing)
            self.db.commit()
            return {"status": "existing", "tool_id": existing.id}
        row = Tool(
            tenant_id=suggestion.tenant_id,
            name=name,
            display_name=_optional_str(payload.get("display_name")) or suggestion.title,
            description=_optional_str(payload.get("description") or suggestion.reason),
            method=str(payload.get("method") or "POST").upper(),
            url=str(payload.get("url") or ""),
            headers_json=payload.get("headers") if isinstance(payload.get("headers"), dict) else {},
            auth_json=payload.get("auth") if isinstance(payload.get("auth"), dict) else {},
            input_schema=payload.get("input_schema") if isinstance(payload.get("input_schema"), dict) else {},
            output_schema=payload.get("output_schema") if isinstance(payload.get("output_schema"), dict) else {},
            allowed_skills_json=payload.get("allowed_skills") if isinstance(payload.get("allowed_skills"), list) else [],
            enabled=True,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return {"status": "created", "tool_id": row.id}

    def _confirm_skill(self, suggestion: KnowledgeDiscoverySuggestion, payload: dict[str, Any]) -> dict[str, Any]:
        skill_payload = payload.get("draft_skill") if isinstance(payload.get("draft_skill"), dict) else payload
        card = SkillCard.model_validate(skill_payload)
        existing = self.db.exec(
            select(Skill).where(Skill.tenant_id == suggestion.tenant_id, Skill.skill_id == card.skill_id)
        ).first()
        if existing:
            existing.content_json = card.model_dump(mode="json")
            existing.name = card.name
            existing.version = card.version
            existing.business_domain = card.business_domain
            existing.description = card.description
            existing.updated_at = utc_now()
            self.db.add(existing)
            self.db.commit()
            return {"status": "updated", "skill_id": existing.id}
        row = Skill(
            tenant_id=suggestion.tenant_id,
            skill_id=card.skill_id,
            version=card.version,
            name=card.name,
            business_domain=card.business_domain,
            description=card.description,
            content_json=card.model_dump(mode="json"),
            status="draft",
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return {"status": "created", "skill_id": row.id}

    def _default_model_config(self, tenant_id: str) -> ModelConfig | None:
        return self.db.exec(
            select(ModelConfig).where(
                ModelConfig.tenant_id == tenant_id,
                ModelConfig.is_default == True,  # noqa: E712 - SQLModel expression.
                ModelConfig.enabled == True,  # noqa: E712
            )
        ).first()

    def _update_job(self, job: KnowledgeIngestJob, **changes: Any) -> None:
        for key, value in changes.items():
            setattr(job, key, value)
        job.updated_at = utc_now()
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)

    def _clear_embedded_content(self, job: KnowledgeIngestJob) -> None:
        metadata = dict(job.metadata_json or {})
        metadata.pop("content_base64", None)
        job.metadata_json = metadata
        job.updated_at = utc_now()
        self.db.add(job)
        self.db.commit()


def bucket_read(row: KnowledgeBucket) -> KnowledgeBucketRead:
    return KnowledgeBucketRead(
        id=row.id,
        tenant_id=row.tenant_id,
        document_id=row.document_id,
        bucket_key=row.bucket_key,
        title=row.title,
        summary=row.summary,
        token_estimate=row.token_estimate,
        metadata=row.metadata_json or {},
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def chunk_read(row: KnowledgeChunk) -> KnowledgeChunkRead:
    return KnowledgeChunkRead(
        id=row.id,
        tenant_id=row.tenant_id,
        document_id=row.document_id,
        bucket_id=row.bucket_id,
        chunk_index=row.chunk_index,
        content=row.content,
        summary=row.summary,
        source_ref=row.source_ref,
        metadata=row.metadata_json or {},
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _normalize_text(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    compact: list[str] = []
    blank = False
    for line in lines:
        if not line.strip():
            if not blank:
                compact.append("")
            blank = True
            continue
        compact.append(line)
        blank = False
    return "\n".join(compact).strip()


def _split_sections(text: str) -> list[str]:
    raw_sections: list[str] = []
    current: list[str] = []
    for line in text.split("\n"):
        is_heading = line.lstrip().startswith("#") or (
            len(line.strip()) <= 40 and line.strip().endswith(("：", ":"))
        )
        if is_heading and current:
            raw_sections.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        raw_sections.append("\n".join(current).strip())
    sections: list[str] = []
    for section in raw_sections:
        if len(section) <= BUCKET_SECTION_CHARS:
            sections.append(section)
        else:
            sections.extend(_chunk_text(section, BUCKET_SECTION_CHARS))
    return [section for section in sections if section.strip()] or [text[:BUCKET_SECTION_CHARS]]


def _chunk_text(text: str, max_chars: int) -> list[str]:
    chunks: list[str] = []
    cursor = 0
    while cursor < len(text):
        end = min(len(text), cursor + max_chars)
        if end < len(text):
            boundary = max(text.rfind("\n\n", cursor, end), text.rfind("\n", cursor, end))
            if boundary > cursor + max_chars // 2:
                end = boundary
        chunk = text[cursor:end].strip()
        if chunk:
            chunks.append(chunk)
        cursor = max(end, cursor + 1)
    return chunks or [text.strip()]


def _fallback_bucket_specs(sections: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "bucket_key": f"bucket_{index + 1}",
            "title": _guess_title(section, index),
            "summary": section[:360],
            "content": section,
        }
        for index, section in enumerate(sections)
    ]


def _guess_title(section: str, index: int) -> str:
    first_line = next((line.strip("# ").strip() for line in section.splitlines() if line.strip()), "")
    return first_line[:60] if first_line else f"知识桶 {index + 1}"


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
