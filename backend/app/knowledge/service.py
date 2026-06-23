from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import delete
from sqlmodel import Session, select

from app.db import engine
from app.db.models import (
    KnowledgeBucket,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeConcept,
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
from app.knowledge.okf import (
    build_okf_for_document,
    okf_citations_for_concepts,
    search_concepts,
    selected_concept_cards,
    upsert_concepts,
)
from app.llm import LLMClient, LLMError
from app.skills.skill_schema import SkillCard


PROMPT_DIR = Path(__file__).resolve().parents[1] / "llm" / "prompts"
BUCKET_PROMPT = PROMPT_DIR / "knowledge_bucket_prompt.md"
DISCOVERY_PROMPT = PROMPT_DIR / "knowledge_discovery_prompt.md"
SEARCH_PROMPT = PROMPT_DIR / "knowledge_search_prompt.md"
DOCUMENT_ROUTE_PROMPT = PROMPT_DIR / "knowledge_document_route_prompt.md"

SECTION_TARGET_CHARS = 1400
EVIDENCE_CHUNK_CHARS = 900
BUCKET_SECTION_CHARS = 6000
PARAGRAPH_GROUP_CHARS = 4200
SEARCH_DOCUMENT_LIMIT = 40
SEARCH_BUCKET_LIMIT = 80
GENERIC_SEARCH_TERMS = {
    "请根据",
    "根据",
    "业务",
    "资料",
    "业务资料",
    "说明",
    "用户",
    "服务",
    "服务人员",
    "人员",
    "应该",
    "应该怎么",
    "怎么",
    "怎么处理",
    "处理",
    "回答",
    "必须",
    "知识",
    "引用",
    "知识引用",
    "规则",
    "文档",
    "信息",
    "相关",
    "基于",
}

INGEST_STAGES: list[dict[str, Any]] = [
    {"key": "queued", "label": "排队中", "progress": 0.0},
    {"key": "parsing", "label": "解析原始资料", "progress": 0.08},
    {"key": "normalizing", "label": "规范化 Source", "progress": 0.16},
    {"key": "documenting", "label": "写入 Source Document", "progress": 0.24},
    {"key": "bucketing", "label": "规划 Wiki 概念", "progress": 0.36},
    {"key": "bucket_writing", "label": "写入 OKF Wiki", "progress": 0.48},
    {"key": "chunking", "label": "生成证据层", "progress": 0.62},
    {"key": "summarizing", "label": "刷新知识桶", "progress": 0.74},
    {"key": "discovering", "label": "发现 SOP/工具", "progress": 0.88},
    {"key": "done", "label": "完成入库", "progress": 1.0},
]

INGEST_STAGE_BY_KEY = {stage["key"]: stage for stage in INGEST_STAGES}


@dataclass
class IngestPayload:
    tenant_id: str
    knowledge_base_id: str
    filename: str
    content_base64: str
    knowledge_base_version_id: str | None = None
    title: str | None = None
    metadata: dict[str, Any] | None = None


class KnowledgeService:
    def __init__(self, db: Session):
        self.db = db

    def create_ingest_job(self, payload: IngestPayload) -> KnowledgeIngestJob:
        job = KnowledgeIngestJob(
            tenant_id=payload.tenant_id,
            knowledge_base_id=payload.knowledge_base_id,
            knowledge_base_version_id=payload.knowledge_base_version_id
            or _default_knowledge_base_version_id(payload.knowledge_base_id),
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
        self._update_ingest_stage(
            job,
            "parsing",
            status="running",
            started_at=utc_now(),
            detail="正在识别文件格式并抽取正文",
        )
        metadata = job.metadata_json or {}
        try:
            content = base64.b64decode(str(metadata.get("content_base64") or ""))
            text, file_type = extract_text(job.filename, content)
            self._update_ingest_stage(
                job,
                "normalizing",
                detail=f"已抽取 {file_type} 文本，正在清理空行和段落",
            )
            normalized_text = _normalize_text(text)
            if not normalized_text:
                raise KnowledgeParseError("文档没有可用文本内容。")

            self._update_ingest_stage(
                job,
                "documenting",
                detail=f"已获得 {len(normalized_text):,} 字符，正在识别章节导航树",
                stats={"char_count": len(normalized_text), "file_type": file_type},
            )
            section_nodes = _build_section_nodes(normalized_text)
            document_card = _build_document_card(
                title=str(metadata.get("title") or Path(job.filename).stem),
                filename=job.filename,
                file_type=file_type,
                text=normalized_text,
                section_nodes=section_nodes,
            )
            document = KnowledgeDocument(
                tenant_id=job.tenant_id,
                knowledge_base_id=job.knowledge_base_id,
                knowledge_base_version_id=job.knowledge_base_version_id,
                filename=job.filename,
                file_type=file_type,
                title=str(metadata.get("title") or Path(job.filename).stem),
                status="processing",
                metadata_json={
                    **(metadata.get("metadata") if isinstance(metadata.get("metadata"), dict) else {}),
                    "char_count": len(normalized_text),
                    "document_card": document_card,
                    "section_tree": section_nodes,
                    "section_stats": {
                        "section_count": len(section_nodes),
                        "paragraph_count": len(_paragraph_blocks(normalized_text)),
                    },
                },
            )
            self.db.add(document)
            self.db.commit()
            self.db.refresh(document)
            job.document_id = document.id
            self._update_ingest_stage(
                job,
                "bucketing",
                detail="正在按目录结构、章节语义和任务用途规划 OKF Wiki 概念",
                document_id=document.id,
                stats={"section_count": len(section_nodes)},
            )

            buckets = self._build_buckets(
                job.tenant_id,
                job.knowledge_base_id,
                document,
                normalized_text,
                section_nodes,
                document_card,
            )
            self._update_ingest_stage(
                job,
                "bucket_writing",
                detail=f"已规划 {len(buckets)} 个知识主题，正在写入 OKF Wiki 与知识桶",
                stats={"bucket_count": len(buckets), "section_count": len(section_nodes)},
            )
            self._update_ingest_stage(
                job,
                "chunking",
                detail="正在从 OKF Wiki 与原始资料回填证据片段",
                stats={"bucket_count": len(buckets)},
            )
            chunk_count = self._build_chunks(job.tenant_id, job.knowledge_base_id, document, buckets, section_nodes)
            okf_concepts = build_okf_for_document(document, section_nodes, buckets)
            concept_rows = upsert_concepts(
                self.db,
                job.tenant_id,
                job.knowledge_base_id,
                document.knowledge_base_version_id,
                okf_concepts,
            )

            document.bucket_count = len(buckets)
            document.chunk_count = chunk_count
            document.status = "ready"
            document.metadata_json = {
                **(document.metadata_json or {}),
                "chunk_stats": {
                    "total_chunks": chunk_count,
                    "chunk_count": chunk_count,
                    "target_chars": EVIDENCE_CHUNK_CHARS,
                    "section_target_chars": SECTION_TARGET_CHARS,
                },
                "bucket_quality": [
                    {
                        "bucket_id": bucket.id,
                        "title": bucket.title,
                        "quality": (bucket.metadata_json or {}).get("quality", {}),
                    }
                    for bucket in buckets
                ],
                "okf": {
                    "version": "0.1",
                    "concept_count": len(concept_rows),
                    "concept_types": sorted({row.concept_type for row in concept_rows}),
                },
            }
            document.updated_at = utc_now()
            self.db.add(document)
            self._update_ingest_stage(
                job,
                "summarizing",
                detail=f"已生成 {chunk_count} 个证据片段，正在刷新知识桶与片段摘要",
                stats={"concept_count": len(concept_rows), "bucket_count": len(buckets), "chunk_count": chunk_count},
            )
            self._update_ingest_stage(
                job,
                "discovering",
                detail="正在从 OKF Wiki 和证据层发现可确认的 SOP/工具建议",
                stats={"bucket_count": len(buckets), "chunk_count": chunk_count},
            )

            self._discover_from_document(job.tenant_id, job.knowledge_base_id, document, buckets)
            self._update_ingest_stage(
                job,
                "done",
                status="succeeded",
                finished_at=utc_now(),
                detail=f"完成入库：{len(concept_rows)} 个 Wiki 概念，{len(buckets)} 个知识桶，{chunk_count} 个证据片段",
                stats={
                    "concept_count": len(concept_rows),
                    "bucket_count": len(buckets),
                    "chunk_count": chunk_count,
                },
            )
            self._clear_embedded_content(job)
        except Exception as exc:  # noqa: BLE001 - persist stable job failure.
            if job.document_id:
                document = self.db.get(KnowledgeDocument, job.document_id)
                if document:
                    document.status = "failed"
                    document.error = str(exc)
                    document.updated_at = utc_now()
                    self.db.add(document)
            self._update_ingest_stage(
                job,
                "failed",
                status="failed",
                error=str(exc),
                finished_at=utc_now(),
                detail=str(exc),
            )
            self._clear_embedded_content(job)

    def search(self, request: KnowledgeSearchRequest, model_config: ModelConfig | None = None) -> KnowledgeSearchResponse:
        query = request.query.strip()
        if not query:
            return KnowledgeSearchResponse()
        route_trace: list[dict[str, Any]] = []
        if request.agent_id and not request.knowledge_base_ids and not request.knowledge_base_version_ids:
            route_trace.append({"phase": "no_visible_knowledge", "message": "当前智能体没有可见知识"})
            return KnowledgeSearchResponse(trace=route_trace, route_trace=route_trace)

        concepts = self._load_concepts_for_search(request)
        selected_concepts = search_concepts(query, concepts, max(request.max_buckets, 4))
        selected_concept_payload = selected_concept_cards(selected_concepts)
        okf_citations = okf_citations_for_concepts(selected_concepts)
        if concepts:
            route_trace.append(
                {
                    "phase": "okf_concept_route",
                    "message": "正在选择 OKF Wiki 概念",
                    "candidate_count": len(concepts),
                    "selected_count": len(selected_concepts),
                }
            )

        documents = self._load_documents_for_search(request)
        if not documents and not selected_concepts:
            route_trace.append({"phase": "no_documents", "message": "没有可检索的知识文档或 OKF 概念"})
            return KnowledgeSearchResponse(trace=route_trace, route_trace=route_trace)
        if not documents:
            route_trace.append({"phase": "okf_only", "message": "仅命中 OKF Wiki 概念"})
            return KnowledgeSearchResponse(
                trace=route_trace,
                route_trace=route_trace,
                selected_concepts=selected_concept_payload,
                okf_citations=okf_citations,
            )

        route_trace.append(
            {
                "phase": "document_route",
                "message": "正在选择知识文档",
                "candidate_count": len(documents),
                "mode": request.mode,
            }
        )
        selected_document_ids: list[str] = []
        if model_config:
            selected_document_ids = self._select_documents_with_llm(query, documents, 5, model_config, route_trace)
        if not selected_document_ids:
            selected_document_ids = [row.id for row in _score_documents(query, documents)[:5]]
            route_trace.append({"phase": "document_route_fallback", "message": "按文档卡相关性选择知识文档"})
        concept_document_ids = [
            str(ref.get("document_id"))
            for concept in selected_concepts
            for ref in (concept.source_refs_json or [])
            if isinstance(ref, dict) and ref.get("document_id")
        ]
        selected_document_ids = _unique_strings(selected_document_ids + concept_document_ids[:3])

        selected_documents = [row for row in documents if row.id in set(selected_document_ids)]
        selected_document_cards = [_document_card_for_search(row) for row in selected_documents]

        buckets = self._load_buckets_for_search(request, selected_document_ids)
        if not buckets:
            route_trace.append({"phase": "no_buckets", "message": "所选文档没有可展开的知识桶"})
            return KnowledgeSearchResponse(
                trace=route_trace,
                route_trace=route_trace,
                selected_documents=selected_document_cards,
                selected_concepts=selected_concept_payload,
                okf_citations=okf_citations,
            )

        selected_ids: list[str] = []
        route_trace.append(
            {
                "phase": "bucket_route",
                "message": "正在选择知识桶",
                "candidate_count": len(buckets),
                "selected_document_ids": selected_document_ids,
            }
        )
        if model_config:
            selected_ids = self._select_buckets_with_llm(query, buckets, request.max_buckets, model_config, route_trace)
        if not selected_ids:
            selected_ids = [bucket.id for bucket in _score_buckets(query, buckets)[: request.max_buckets]]
            route_trace.append({"phase": "bucket_route_fallback", "message": "按桶摘要相关性选择知识桶"})

        bucket_by_id = {bucket.id: bucket for bucket in buckets}
        selected_buckets = [bucket_by_id[bucket_id] for bucket_id in selected_ids if bucket_id in bucket_by_id]
        expanded_sections = _expand_sections(selected_documents, selected_buckets, request.max_depth)
        route_trace.append(
            {
                "phase": "section_expand",
                "message": "正在展开章节",
                "section_count": len(expanded_sections),
            }
        )
        chunks = self._load_chunks_for_buckets(request.tenant_id, selected_ids, max(request.max_chunks * 3, request.max_chunks))
        ranked_chunks = _rank_chunks(query, chunks, selected_buckets, expanded_sections)[: request.max_chunks]
        evidence_pack = _build_evidence_pack(query, ranked_chunks) if request.need_evidence_pack else []
        route_trace.extend(
            [
                {"phase": "read_chunks", "message": "读取知识片段", "chunk_count": len(ranked_chunks)},
                {"phase": "evidence_pack", "message": "整理证据包", "evidence_count": len(evidence_pack)},
            ]
        )
        return KnowledgeSearchResponse(
            selected_buckets=[bucket_read(row) for row in selected_buckets],
            chunks=[chunk_read(row) for row in ranked_chunks],
            trace=route_trace,
            route_trace=route_trace,
            selected_documents=selected_document_cards,
            selected_concepts=selected_concept_payload,
            expanded_sections=expanded_sections,
            okf_citations=okf_citations,
            evidence_pack=evidence_pack,
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
        knowledge_base_id: str,
        document: KnowledgeDocument,
        text: str,
        section_nodes: list[dict[str, Any]],
        document_card: dict[str, Any],
    ) -> list[KnowledgeBucket]:
        model_config = self._default_model_config(tenant_id)
        structure_buckets = _structure_bucket_specs(section_nodes)
        llm_buckets = self._bucket_with_llm(section_nodes, model_config) if model_config else []
        bucket_specs = _unique_bucket_specs(structure_buckets + _normalize_llm_bucket_specs(llm_buckets, section_nodes))
        if not bucket_specs:
            bucket_specs = _fallback_bucket_specs(_split_sections(text), section_nodes)
        self.db.exec(delete(KnowledgeBucket).where(KnowledgeBucket.document_id == document.id))
        self.db.exec(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))
        self.db.exec(delete(KnowledgeDiscoverySuggestion).where(KnowledgeDiscoverySuggestion.document_id == document.id))
        rows: list[KnowledgeBucket] = []
        for index, spec in enumerate(bucket_specs):
            content = str(spec.get("content") or "")
            section_ids = [str(item) for item in spec.get("section_ids", []) if item]
            if not content and section_ids:
                section_by_id = {str(node.get("section_id")): node for node in section_nodes}
                content = "\n\n".join(
                    str(section_by_id[section_id].get("content") or "")
                    for section_id in section_ids
                    if section_id in section_by_id
                )
            content = content or text[:BUCKET_SECTION_CHARS]
            quality = _bucket_quality(spec, section_ids, content)
            row = KnowledgeBucket(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base_id,
                knowledge_base_version_id=document.knowledge_base_version_id,
                document_id=document.id,
                bucket_key=str(spec.get("bucket_key") or f"bucket_{index + 1}"),
                title=str(spec.get("title") or f"知识桶 {index + 1}"),
                summary=str(spec.get("summary") or content[:300]),
                token_estimate=max(1, len(content) // 2),
                metadata_json={
                    "content": content[:BUCKET_SECTION_CHARS],
                    "bucket_type": str(spec.get("bucket_type") or "structure"),
                    "section_ids": section_ids,
                    "section_paths": spec.get("section_paths") if isinstance(spec.get("section_paths"), list) else [],
                    "representative_chunk_ids": [],
                    "applicable_query_types": spec.get("applicable_query_types") if isinstance(spec.get("applicable_query_types"), list) else [],
                    "quality": quality,
                    "document_card": {
                        "title": document_card.get("title"),
                        "summary": document_card.get("summary"),
                    },
                },
            )
            self.db.add(row)
            rows.append(row)
        self.db.commit()
        for row in rows:
            self.db.refresh(row)
        return rows

    def _build_chunks(
        self,
        tenant_id: str,
        knowledge_base_id: str,
        document: KnowledgeDocument,
        buckets: list[KnowledgeBucket],
        section_nodes: list[dict[str, Any]],
    ) -> int:
        count = 0
        chunk_ids_by_bucket: dict[str, list[str]] = {}
        section_by_id = {str(node.get("section_id")): node for node in section_nodes}
        for bucket in buckets:
            metadata = dict(bucket.metadata_json or {})
            section_ids = [str(item) for item in metadata.get("section_ids", []) if item]
            section_sources = [section_by_id[section_id] for section_id in section_ids if section_id in section_by_id]
            if not section_sources:
                section_sources = [
                    {
                        "section_id": f"{bucket.bucket_key}_content",
                        "path": bucket.title,
                        "title": bucket.title,
                        "content": str(metadata.get("content") or ""),
                    }
                ]
            local_index = 0
            for section in section_sources:
                content = str(section.get("content") or "")
                parts = _chunk_text(content, EVIDENCE_CHUNK_CHARS)
                for part in parts:
                    source_path = f"{document.filename} / {section.get('path') or bucket.title} / evidence {local_index + 1}"
                    row = KnowledgeChunk(
                        tenant_id=tenant_id,
                        knowledge_base_id=knowledge_base_id,
                        knowledge_base_version_id=document.knowledge_base_version_id,
                        document_id=document.id,
                        bucket_id=bucket.id,
                        chunk_index=local_index,
                        content=part,
                        summary=_summarize_text(part, 180),
                        source_ref=source_path,
                        metadata_json={
                            "node_type": "evidence_chunk",
                            "section_id": section.get("section_id"),
                            "section_path": section.get("path"),
                            "section_title": section.get("title"),
                            "bucket_title": bucket.title,
                            "source_span": section.get("source_span") or {},
                            "context_window": _summarize_text(content, 260),
                        },
                    )
                    self.db.add(row)
                    self.db.flush()
                    chunk_ids_by_bucket.setdefault(bucket.id, []).append(row.id)
                    count += 1
                    local_index += 1
        for bucket in buckets:
            metadata = dict(bucket.metadata_json or {})
            metadata["representative_chunk_ids"] = chunk_ids_by_bucket.get(bucket.id, [])[:3]
            metadata["chunk_count"] = len(chunk_ids_by_bucket.get(bucket.id, []))
            bucket.metadata_json = metadata
            bucket.updated_at = utc_now()
            self.db.add(bucket)
        self.db.commit()
        return count

    def _discover_from_document(
        self,
        tenant_id: str,
        knowledge_base_id: str,
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
                knowledge_base_id=knowledge_base_id,
                knowledge_base_version_id=document.knowledge_base_version_id,
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
        self, section_nodes: list[dict[str, Any]], model_config: ModelConfig | None
    ) -> list[dict[str, Any]]:
        if not model_config:
            return []
        payload = {
            "sections": [
                {
                    "section_id": node.get("section_id"),
                    "path": node.get("path"),
                    "title": node.get("title"),
                    "summary": node.get("summary"),
                    "excerpt": str(node.get("content") or "")[:1800],
                }
                for node in section_nodes[:60]
            ]
        }
        try:
            raw = LLMClient(model_config).generate_json(BUCKET_PROMPT.read_text(encoding="utf-8"), payload)
        except (LLMError, Exception):
            return []
        buckets = raw.get("buckets") if isinstance(raw, dict) else None
        return [item for item in buckets if isinstance(item, dict)] if isinstance(buckets, list) else []

    def _load_documents_for_search(self, request: KnowledgeSearchRequest) -> list[KnowledgeDocument]:
        stmt = select(KnowledgeDocument).where(
            KnowledgeDocument.tenant_id == request.tenant_id,
            KnowledgeDocument.status == "ready",
        )
        if request.knowledge_base_ids:
            stmt = stmt.where(KnowledgeDocument.knowledge_base_id.in_(request.knowledge_base_ids))
        if request.knowledge_base_version_ids:
            stmt = stmt.where(KnowledgeDocument.knowledge_base_version_id.in_(request.knowledge_base_version_ids))
        if request.document_ids:
            stmt = stmt.where(KnowledgeDocument.id.in_(request.document_ids))
        return self.db.exec(stmt.order_by(KnowledgeDocument.updated_at.desc()).limit(SEARCH_DOCUMENT_LIMIT)).all()

    def _load_concepts_for_search(self, request: KnowledgeSearchRequest) -> list[KnowledgeConcept]:
        stmt = select(KnowledgeConcept).where(
            KnowledgeConcept.tenant_id == request.tenant_id,
            KnowledgeConcept.status == "active",
        )
        if request.knowledge_base_ids:
            stmt = stmt.where(KnowledgeConcept.knowledge_base_id.in_(request.knowledge_base_ids))
        if request.knowledge_base_version_ids:
            stmt = stmt.where(KnowledgeConcept.knowledge_base_version_id.in_(request.knowledge_base_version_ids))
        if request.document_ids:
            stmt = stmt.where(KnowledgeConcept.document_id.in_(request.document_ids))
        return self.db.exec(stmt.order_by(KnowledgeConcept.updated_at.desc()).limit(120)).all()

    def _load_buckets_for_search(self, request: KnowledgeSearchRequest, document_ids: list[str]) -> list[KnowledgeBucket]:
        if not document_ids:
            return []
        stmt = select(KnowledgeBucket).where(
            KnowledgeBucket.tenant_id == request.tenant_id,
            KnowledgeBucket.document_id.in_(document_ids),
        )
        if request.knowledge_base_ids:
            stmt = stmt.where(KnowledgeBucket.knowledge_base_id.in_(request.knowledge_base_ids))
        if request.knowledge_base_version_ids:
            stmt = stmt.where(KnowledgeBucket.knowledge_base_version_id.in_(request.knowledge_base_version_ids))
        return self.db.exec(stmt.order_by(KnowledgeBucket.created_at.asc()).limit(SEARCH_BUCKET_LIMIT)).all()

    def _select_documents_with_llm(
        self,
        query: str,
        documents: list[KnowledgeDocument],
        max_documents: int,
        model_config: ModelConfig,
        trace: list[dict[str, Any]],
    ) -> list[str]:
        payload = {
            "query": query,
            "max_documents": max_documents,
            "documents": [_document_card_for_search(row) for row in documents],
        }
        try:
            raw = LLMClient(model_config).generate_json(DOCUMENT_ROUTE_PROMPT.read_text(encoding="utf-8"), payload)
        except (LLMError, Exception) as exc:
            trace.append({"phase": "document_route_failed", "message": str(exc)})
            return []
        ids = raw.get("selected_document_ids") if isinstance(raw, dict) else None
        if not isinstance(ids, list):
            return []
        allowed = {row.id for row in documents}
        return [str(item) for item in ids if str(item) in allowed][:max_documents]

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
                    "bucket_type": (bucket.metadata_json or {}).get("bucket_type"),
                    "section_paths": (bucket.metadata_json or {}).get("section_paths", []),
                    "quality": (bucket.metadata_json or {}).get("quality", {}),
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
            bucket=str(payload.get("bucket") or "知识自发现工具").strip() or "知识自发现工具",
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

    def ensure_default_knowledge_base(self, tenant_id: str) -> KnowledgeBase:
        existing = self.db.exec(
            select(KnowledgeBase)
            .where(KnowledgeBase.tenant_id == tenant_id)
            .order_by(KnowledgeBase.created_at.asc())
        ).first()
        if existing:
            return existing
        row = KnowledgeBase(
            id=f"kb_{tenant_id}_default",
            tenant_id=tenant_id,
            name="默认知识库",
            description="系统默认知识库",
            status="active",
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def _update_job(self, job: KnowledgeIngestJob, **changes: Any) -> None:
        for key, value in changes.items():
            setattr(job, key, value)
        job.updated_at = utc_now()
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)

    def _update_ingest_stage(
        self,
        job: KnowledgeIngestJob,
        stage: str,
        detail: str = "",
        stats: dict[str, Any] | None = None,
        **changes: Any,
    ) -> None:
        stage_def = INGEST_STAGE_BY_KEY.get(stage)
        progress = float(stage_def["progress"]) if stage_def else float(job.progress or 0.0)
        metadata = dict(job.metadata_json or {})
        metadata["stage_label"] = str(stage_def["label"] if stage_def else stage)
        metadata["stage_detail"] = detail
        if stats is not None:
            metadata["stage_stats"] = stats
        metadata["ingest_steps"] = _ingest_steps_for(stage, progress, changes.get("status") or job.status)
        job.metadata_json = metadata
        self._update_job(job, stage=stage, progress=progress, **changes)

    def _clear_embedded_content(self, job: KnowledgeIngestJob) -> None:
        metadata = dict(job.metadata_json or {})
        metadata.pop("content_base64", None)
        job.metadata_json = metadata
        job.updated_at = utc_now()
        self.db.add(job)
        self.db.commit()


def bucket_read(row: KnowledgeBucket) -> KnowledgeBucketRead:
    metadata = row.metadata_json or {}
    return KnowledgeBucketRead(
        id=row.id,
        tenant_id=row.tenant_id,
        knowledge_base_id=row.knowledge_base_id,
        document_id=row.document_id,
        bucket_key=row.bucket_key,
        title=row.title,
        summary=row.summary,
        token_estimate=row.token_estimate,
        chunk_count=int(metadata.get("chunk_count") or len(metadata.get("representative_chunk_ids") or []) or 0),
        status="ready",
        metadata=metadata,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def chunk_read(row: KnowledgeChunk) -> KnowledgeChunkRead:
    return KnowledgeChunkRead(
        id=row.id,
        tenant_id=row.tenant_id,
        knowledge_base_id=row.knowledge_base_id,
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
    paragraphs = _paragraph_blocks(text)
    if not paragraphs:
        return [text[:BUCKET_SECTION_CHARS]]

    sections: list[str] = []
    current: list[str] = []
    current_len = 0

    for paragraph in paragraphs:
        parts = _split_large_paragraph(paragraph, BUCKET_SECTION_CHARS)
        for part in parts:
            is_heading = _looks_like_heading(part)
            projected = current_len + len(part) + (2 if current else 0)
            if current and (is_heading or projected > PARAGRAPH_GROUP_CHARS):
                sections.append("\n\n".join(current).strip())
                current = []
                current_len = 0
            current.append(part)
            current_len += len(part) + (2 if current_len else 0)

    if current:
        sections.append("\n\n".join(current).strip())
    return [section for section in sections if section.strip()] or [text[:BUCKET_SECTION_CHARS]]


def _chunk_text(text: str, max_chars: int) -> list[str]:
    paragraphs = _paragraph_blocks(text)
    if paragraphs:
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for paragraph in paragraphs:
            for part in _split_large_paragraph(paragraph, max_chars):
                projected = current_len + len(part) + (2 if current else 0)
                if current and projected > max_chars:
                    chunks.append("\n\n".join(current).strip())
                    current = []
                    current_len = 0
                current.append(part)
                current_len += len(part) + (2 if current_len else 0)
        if current:
            chunks.append("\n\n".join(current).strip())
        return [chunk for chunk in chunks if chunk.strip()]

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


def _paragraph_blocks(text: str) -> list[str]:
    normalized = _normalize_text(text)
    if not normalized:
        return []
    blocks = [block.strip() for block in re.split(r"\n\s*\n+", normalized) if block.strip()]
    if len(blocks) > 1:
        return blocks
    # Plain exported documents often lose blank lines. Fall back to single lines
    # so long documents still get incremental discovery instead of one huge block.
    return [line.strip() for line in normalized.splitlines() if line.strip()]


def _looks_like_heading(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("#") or (
        len(stripped) <= 48 and stripped.endswith(("：", ":"))
    )


def _split_large_paragraph(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    sentences = [item.strip() for item in re.split(r"(?<=[。！？.!?；;])\s*", text) if item.strip()]
    if len(sentences) <= 1:
        return _hard_split_text(text, max_chars)
    parts: list[str] = []
    current: list[str] = []
    current_len = 0
    for sentence in sentences:
        if len(sentence) > max_chars:
            if current:
                parts.append("".join(current).strip())
                current = []
                current_len = 0
            parts.extend(_hard_split_text(sentence, max_chars))
            continue
        if current and current_len + len(sentence) > max_chars:
            parts.append("".join(current).strip())
            current = []
            current_len = 0
        current.append(sentence)
        current_len += len(sentence)
    if current:
        parts.append("".join(current).strip())
    return [part for part in parts if part.strip()]


def _hard_split_text(text: str, max_chars: int) -> list[str]:
    return [text[index:index + max_chars].strip() for index in range(0, len(text), max_chars) if text[index:index + max_chars].strip()]


def _ingest_steps_for(stage: str, progress: float, status: str) -> list[dict[str, Any]]:
    if stage == "failed" or status == "failed":
        return [
            {
                **item,
                "status": "done" if float(item["progress"]) < progress else "pending",
            }
            for item in INGEST_STAGES
        ]
    return [
        {
            **item,
            "status": (
                "running"
                if item["key"] == stage
                else "done"
                if float(item["progress"]) < progress or (stage == "done" and item["key"] == "done")
                else "pending"
            ),
        }
        for item in INGEST_STAGES
    ]


def _build_section_nodes(text: str) -> list[dict[str, Any]]:
    paragraphs = _paragraph_blocks(text)
    if not paragraphs:
        return [
            {
                "section_id": "sec_1",
                "node_type": "section",
                "level": 1,
                "title": "全文",
                "parent_id": None,
                "path": "全文",
                "section_order": 1,
                "summary": _summarize_text(text, 260),
                "content": text,
                "source_span": {"start_paragraph": 0, "end_paragraph": 0},
                "anchor_entities": _extract_anchor_entities(text),
            }
        ]

    nodes: list[dict[str, Any]] = []
    stack: dict[int, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    current_parts: list[str] = []
    current_start = 0

    def flush(end_paragraph: int) -> None:
        nonlocal current, current_parts, current_start
        if current is None:
            return
        content = "\n\n".join(part for part in current_parts if part.strip()).strip()
        if not content:
            content = str(current.get("title") or "")
        current["content"] = content
        current["summary"] = _section_summary(current.get("title"), content)
        current["anchor_entities"] = _extract_anchor_entities(content)
        current["source_span"] = {"start_paragraph": current_start, "end_paragraph": end_paragraph}
        nodes.append(current)
        current = None
        current_parts = []

    def start_section(title: str, level: int, paragraph_index: int, parent_id: str | None = None) -> dict[str, Any]:
        section_id = f"sec_{len(nodes) + 1}"
        parent = next((stack[item] for item in sorted(stack, reverse=True) if item < level), None)
        resolved_parent_id = parent_id if parent_id is not None else (parent.get("section_id") if parent else None)
        parent_path = str(parent.get("path") or "") if parent else ""
        path = f"{parent_path} / {title}" if parent_path else title
        return {
            "section_id": section_id,
            "node_type": "section",
            "level": level,
            "title": title,
            "parent_id": resolved_parent_id,
            "path": path,
            "section_order": len(nodes) + 1,
            "summary": "",
            "content": "",
            "source_span": {"start_paragraph": paragraph_index, "end_paragraph": paragraph_index},
            "anchor_entities": [],
        }

    for index, paragraph in enumerate(paragraphs):
        heading = _heading_info(paragraph)
        if heading:
            flush(index - 1)
            level, title = heading
            current = start_section(title, level, index)
            current_parts = [paragraph]
            current_start = index
            stack = {key: value for key, value in stack.items() if key < level}
            stack[level] = current
            continue

        if current is None:
            current = start_section(f"段落组 {len(nodes) + 1}", 1, index)
            current_parts = []
            current_start = index

        projected = sum(len(part) for part in current_parts) + len(paragraph)
        if current_parts and projected > SECTION_TARGET_CHARS:
            parent_id = str(current.get("parent_id") or "")
            title = str(current.get("title") or f"段落组 {len(nodes) + 1}")
            level = int(current.get("level") or 1)
            flush(index - 1)
            current = start_section(title, level, index, parent_id or None)
            current_parts = []
            current_start = index
        current_parts.append(paragraph)

    flush(len(paragraphs) - 1)
    if not nodes:
        return _build_section_nodes(text[:SECTION_TARGET_CHARS])
    return nodes


def _build_document_card(
    title: str,
    filename: str,
    file_type: str,
    text: str,
    section_nodes: list[dict[str, Any]],
) -> dict[str, Any]:
    outline = [
        {
            "section_id": node.get("section_id"),
            "title": node.get("title"),
            "path": node.get("path"),
            "level": node.get("level"),
            "summary": node.get("summary"),
        }
        for node in section_nodes[:60]
    ]
    summary_source = "\n".join(str(node.get("summary") or "") for node in section_nodes[:12])
    entities = _extract_anchor_entities(text[:12000])
    use_cases = [
        str(node.get("title"))
        for node in section_nodes
        if node.get("title")
    ][:8]
    return {
        "title": title,
        "filename": filename,
        "file_type": file_type,
        "summary": _summarize_text(summary_source or text, 520),
        "outline": outline,
        "applicable_scenarios": use_cases,
        "key_entities": entities[:24],
        "char_count": len(text),
        "section_count": len(section_nodes),
    }


def _heading_info(text: str) -> tuple[int, str] | None:
    stripped = text.strip()
    if not stripped:
        return None
    markdown = re.match(r"^(#{1,6})\s+(.+)$", stripped)
    if markdown:
        return len(markdown.group(1)), markdown.group(2).strip()
    chapter = re.match(r"^第[一二三四五六七八九十百千万0-9]+[章节篇部分]\s*[：:\-、]?\s*(.+)?$", stripped)
    if chapter:
        return 1, (chapter.group(1) or stripped).strip()
    numbered = re.match(r"^(\d+(?:\.\d+){0,4})[、.\s]+(.{2,80})$", stripped)
    if numbered:
        return min(numbered.group(1).count(".") + 1, 5), numbered.group(2).strip()
    if _looks_like_heading(stripped):
        return 2, stripped.strip("# ：:")
    return None


def _section_summary(title: Any, content: str) -> str:
    prefix = str(title or "").strip()
    summary = _summarize_text(content, 260)
    if prefix and prefix not in summary:
        return f"{prefix}：{summary}"[:320]
    return summary


def _summarize_text(text: str, max_chars: int) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    if len(compact) <= max_chars:
        return compact
    end = compact.rfind("。", 0, max_chars)
    if end < max_chars // 2:
        end = compact.rfind("；", 0, max_chars)
    if end < max_chars // 2:
        end = max_chars
    return compact[:end].strip() + "..."


def _extract_anchor_entities(text: str) -> list[str]:
    candidates: list[str] = []
    patterns = [
        r"[A-Za-z][A-Za-z0-9_.:/-]{2,}",
        r"[\u4e00-\u9fff]{2,12}",
        r"\d+(?:\.\d+)+",
    ]
    for pattern in patterns:
        candidates.extend(re.findall(pattern, text or ""))
    seen: set[str] = set()
    result: list[str] = []
    for item in candidates:
        value = str(item).strip(".,，。；;：:()（）[]【】")
        if len(value) < 2 or value in seen:
            continue
        seen.add(value)
        result.append(value)
        if len(result) >= 40:
            break
    return result


def _structure_bucket_specs(section_nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not section_nodes:
        return []
    top_groups: dict[str, list[dict[str, Any]]] = {}
    for node in section_nodes:
        path = str(node.get("path") or node.get("title") or "未命名章节")
        top = path.split(" / ")[0].strip() or "未命名章节"
        top_groups.setdefault(top, []).append(node)
    specs: list[dict[str, Any]] = []
    for index, (title, nodes) in enumerate(top_groups.items()):
        content = "\n\n".join(str(node.get("content") or "") for node in nodes)
        section_paths = [str(node.get("path") or node.get("title") or "") for node in nodes]
        specs.append(
            {
                "bucket_key": _safe_key(title, fallback=f"structure_{index + 1}"),
                "title": title,
                "summary": _summarize_text("\n".join(str(node.get("summary") or "") for node in nodes), 420),
                "content": content[:BUCKET_SECTION_CHARS],
                "section_ids": [str(node.get("section_id")) for node in nodes if node.get("section_id")],
                "section_paths": section_paths,
                "bucket_type": "structure",
                "applicable_query_types": ["answer", "policy_check"],
            }
        )
    return specs


def _normalize_llm_bucket_specs(
    buckets: list[dict[str, Any]],
    section_nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    section_by_index = {index: node for index, node in enumerate(section_nodes)}
    section_by_id = {str(node.get("section_id")): node for node in section_nodes}
    for index, item in enumerate(buckets):
        section_ids = [str(value) for value in item.get("section_ids", []) if value]
        if not section_ids and isinstance(item.get("section_indexes"), list):
            section_ids = [
                str(section_by_index[int(value)].get("section_id"))
                for value in item.get("section_indexes", [])
                if isinstance(value, int) and value in section_by_index
            ]
        nodes = [section_by_id[section_id] for section_id in section_ids if section_id in section_by_id]
        content = "\n\n".join(str(node.get("content") or "") for node in nodes)
        title = str(item.get("title") or f"任务桶 {index + 1}")
        specs.append(
            {
                "bucket_key": str(item.get("bucket_key") or _safe_key(title, f"task_{index + 1}")),
                "title": title,
                "summary": str(item.get("summary") or _summarize_text(content, 420)),
                "content": content[:BUCKET_SECTION_CHARS],
                "section_ids": section_ids,
                "section_paths": [str(node.get("path") or node.get("title") or "") for node in nodes],
                "bucket_type": str(item.get("bucket_type") or "task"),
                "applicable_query_types": item.get("applicable_query_types")
                if isinstance(item.get("applicable_query_types"), list)
                else ["answer", "policy_check", "tool_discovery", "skill_discovery"],
            }
        )
    return specs


def _unique_bucket_specs(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_sections: set[tuple[str, ...]] = set()
    for item in specs:
        key = str(item.get("bucket_key") or "").strip()
        section_ids = tuple(sorted(str(value) for value in item.get("section_ids", []) if value))
        signature = section_ids or (key,)
        if key in seen_keys or signature in seen_sections:
            continue
        seen_keys.add(key)
        seen_sections.add(signature)
        result.append(item)
    return result


def _unique_strings(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _bucket_quality(spec: dict[str, Any], section_ids: list[str], content: str) -> dict[str, Any]:
    warnings: list[str] = []
    if not section_ids:
        warnings.append("missing_source_section")
    if not str(spec.get("summary") or "").strip():
        warnings.append("missing_summary")
    if len(content.strip()) < 40:
        warnings.append("content_too_short")
    return {
        "status": "warning" if warnings else "ready",
        "warnings": warnings,
        "has_source_sections": bool(section_ids),
        "has_summary": bool(str(spec.get("summary") or "").strip()),
        "content_chars": len(content or ""),
    }


def _fallback_bucket_specs(sections: list[str], section_nodes: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    if section_nodes:
        return _structure_bucket_specs(section_nodes)
    return [
        {
            "bucket_key": f"bucket_{index + 1}",
            "title": _guess_title(section, index),
            "summary": section[:360],
            "content": section,
            "bucket_type": "structure",
            "section_ids": [],
            "section_paths": [],
        }
        for index, section in enumerate(sections)
    ]


def _document_card_for_search(row: KnowledgeDocument) -> dict[str, Any]:
    metadata = row.metadata_json or {}
    card = metadata.get("document_card") if isinstance(metadata.get("document_card"), dict) else {}
    return {
        "id": row.id,
        "knowledge_base_id": row.knowledge_base_id,
        "title": card.get("title") or row.title or row.filename,
        "filename": row.filename,
        "file_type": row.file_type,
        "summary": card.get("summary") or "",
        "outline": card.get("outline") if isinstance(card.get("outline"), list) else [],
        "key_entities": card.get("key_entities") if isinstance(card.get("key_entities"), list) else [],
        "section_count": card.get("section_count"),
        "chunk_count": row.chunk_count,
        "updated_at": row.updated_at.isoformat(),
    }


def _score_documents(query: str, documents: list[KnowledgeDocument]) -> list[KnowledgeDocument]:
    return sorted(
        documents,
        key=lambda row: _score_text(
            query,
            " ".join(
                [
                    row.title or "",
                    row.filename,
                    json.dumps((row.metadata_json or {}).get("document_card", {}), ensure_ascii=False)[:4000],
                ]
            ),
        ),
        reverse=True,
    )


def _score_buckets(query: str, buckets: list[KnowledgeBucket]) -> list[KnowledgeBucket]:
    return sorted(
        buckets,
        key=lambda row: _score_text(
            query,
            " ".join(
                [
                    row.title,
                    row.summary,
                    json.dumps(row.metadata_json or {}, ensure_ascii=False)[:3000],
                ]
            ),
        ),
        reverse=True,
    )


def _rank_chunks(
    query: str,
    chunks: list[KnowledgeChunk],
    selected_buckets: list[KnowledgeBucket],
    expanded_sections: list[dict[str, Any]],
) -> list[KnowledgeChunk]:
    bucket_rank = {bucket.id: index for index, bucket in enumerate(selected_buckets)}
    section_ids = {str(item.get("section_id")) for item in expanded_sections if item.get("section_id")}

    def score(chunk: KnowledgeChunk) -> tuple[float, int, int]:
        metadata = chunk.metadata_json or {}
        section_bonus = 2 if str(metadata.get("section_id")) in section_ids else 0
        text_score = _score_text(query, f"{chunk.summary or ''} {chunk.content}")
        return (text_score + section_bonus, -bucket_rank.get(chunk.bucket_id, 999), -chunk.chunk_index)

    return sorted(chunks, key=score, reverse=True)


def _build_evidence_pack(query: str, chunks: list[KnowledgeChunk]) -> list[dict[str, Any]]:
    return [
        {
            "chunk_id": chunk.id,
            "document_id": chunk.document_id,
            "bucket_id": chunk.bucket_id,
            "source_path": chunk.source_ref,
            "section_path": (chunk.metadata_json or {}).get("section_path"),
            "summary": chunk.summary,
            "excerpt": chunk.content[:1200],
            "confidence_reason": "片段摘要、章节路径或正文与查询相关"
            if _score_text(query, f"{chunk.summary or ''} {chunk.content}") > 0
            else "来自已选知识桶的候选证据",
        }
        for chunk in chunks
    ]


def _expand_sections(
    documents: list[KnowledgeDocument],
    buckets: list[KnowledgeBucket],
    max_depth: int,
) -> list[dict[str, Any]]:
    nodes_by_doc: dict[str, dict[str, dict[str, Any]]] = {}
    for document in documents:
        tree = (document.metadata_json or {}).get("section_tree")
        if isinstance(tree, list):
            nodes_by_doc[document.id] = {
                str(node.get("section_id")): node for node in tree if isinstance(node, dict) and node.get("section_id")
            }
    wanted: dict[tuple[str, str], dict[str, Any]] = {}
    for bucket in buckets:
        metadata = bucket.metadata_json or {}
        section_ids = [str(value) for value in metadata.get("section_ids", []) if value]
        doc_nodes = nodes_by_doc.get(bucket.document_id, {})
        for section_id in section_ids:
            node = doc_nodes.get(section_id)
            if node:
                _collect_section_with_children(bucket.document_id, node, doc_nodes, max_depth, wanted, bucket.title)
    return list(wanted.values())


def _collect_section_with_children(
    document_id: str,
    node: dict[str, Any],
    all_nodes: dict[str, dict[str, Any]],
    max_depth: int,
    result: dict[tuple[str, str], dict[str, Any]],
    reason: str,
) -> None:
    section_id = str(node.get("section_id") or "")
    if not section_id:
        return
    result[(document_id, section_id)] = {
        "document_id": document_id,
        "section_id": section_id,
        "title": node.get("title"),
        "path": node.get("path"),
        "summary": node.get("summary"),
        "level": node.get("level"),
        "source_span": node.get("source_span") or {},
        "reason": f"命中知识桶：{reason}",
    }
    if max_depth <= 0:
        return
    children = [child for child in all_nodes.values() if child.get("parent_id") == section_id]
    for child in children:
        _collect_section_with_children(document_id, child, all_nodes, max_depth - 1, result, reason)


def _safe_key(text: str, fallback: str) -> str:
    ascii_words = re.findall(r"[A-Za-z0-9]+", text)
    if ascii_words:
        key = "_".join(ascii_words).lower()
    else:
        key = "bucket_" + str(abs(hash(text)) % 100000)
    key = re.sub(r"_+", "_", key).strip("_")
    return key[:64] or fallback


def _query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for term in re.findall(r"[A-Za-z0-9_.-]{2,}|[\u4e00-\u9fff]{2,}", query or ""):
        normalized = term.lower()
        terms.append(normalized)
        if re.fullmatch(r"[\u4e00-\u9fff]{3,}", normalized):
            for size in (4, 3, 2):
                if len(normalized) <= size:
                    continue
                terms.extend(normalized[index : index + size] for index in range(0, len(normalized) - size + 1))
    result: list[str] = []
    seen: set[str] = set()
    for term in terms:
        normalized = term.lower().strip()
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result[:96]


def _score_text(query: str, text: str) -> float:
    haystack = (text or "").lower()
    score = 0.0
    if query and query.lower() in haystack:
        score += 5.0
    specific_hit = False
    for term in _query_terms(query):
        count = haystack.count(term)
        if count:
            is_generic = term in GENERIC_SEARCH_TERMS
            if is_generic:
                score += min(0.75, count * 0.2)
                continue
            specific_hit = True
            term_weight = 1.4
            if re.fullmatch(r"[\u4e00-\u9fff]{3,}", term):
                term_weight = 2.2
            if re.fullmatch(r"[\u4e00-\u9fff]{4,}", term):
                term_weight = 3.0
            if len(term) >= 5:
                term_weight = 3.4
            score += min(8.0, count * term_weight)
    if not specific_hit and score:
        return score * 0.12
    return score


def _guess_title(section: str, index: int) -> str:
    first_line = next((line.strip("# ").strip() for line in section.splitlines() if line.strip()), "")
    return first_line[:60] if first_line else f"知识桶 {index + 1}"


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _default_knowledge_base_version_id(knowledge_base_id: str) -> str:
    return f"kbver_{knowledge_base_id}_1_0_0"
