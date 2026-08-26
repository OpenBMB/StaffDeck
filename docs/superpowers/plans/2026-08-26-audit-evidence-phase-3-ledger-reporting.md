# 审核证据 Phase 3 台账与分章节报告 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把审核项目全部文本块处理为可追踪证据台账，按审核要素检索知识并分章节生成报告，最终由覆盖 Gate 决定能否发布。

**Architecture:** 后端逐块调用模型提取结构化事实并立即持久化，不让单次模型请求承载全文。审核要素驱动材料证据和知识证据的汇总；每个报告章节只加载相关证据，章节可断点恢复，最终版本冻结材料版本、知识库版本和覆盖快照。

**Tech Stack:** Python 3.11、FastAPI、SQLModel/SQLite、Pydantic v2、OpenAI-compatible LLM、python-docx、pytest

**Spec:** `docs/superpowers/specs/2026-08-26-audit-material-evidence-pipeline-design.md`

## Global Constraints

- 每份当前材料必须提取成功并完成分块，文件覆盖率才可计为 `100%`。
- 每个登记块必须 `processing_status=succeeded`，块覆盖率才可计为 `100%`。
- 每个必需要素必须为 `evidence_found`、`knowledge_found`、`evidence_gap` 或经审核组长确认的 `not_applicable`。
- 模型输入按单块或小批量处理，已成功块不得因重试重复处理。
- 知识检索必须固定 `knowledge_base_version_ids_json`，生成过程中不得静默升级版本。
- 零命中必须写入要素状态，禁止虚构知识依据。
- 报告章节只读取关联台账；失败后只重试未完成章节。
- 最终报告的每条事实引用必须指向有效的材料块或知识块。
- 覆盖 Gate 未通过时只允许保存草稿，禁止标记为已发布。
- 审核组长未确认时最终文件必须标记为“待确认草稿”。

---

## File Structure

- `backend/app/audit_cases/evidence_schema.py`: 模型抽取和证据台账的严格 JSON 契约。
- `backend/app/audit_cases/elements.py`: 按管理体系加载稳定审核要素目录。
- `backend/app/audit_cases/evidence.py`: 块级抽取、幂等处理和材料证据映射。
- `backend/app/audit_cases/knowledge.py`: 按要素运行现有 KnowledgeService 并登记知识证据。
- `backend/app/audit_cases/coverage.py`: 三类覆盖率和发布 Gate。
- `backend/app/audit_cases/reporting.py`: 章节计划、生成、恢复、引用校验和文档合并。
- `backend/app/core/harness_audit_capabilities.py`: 审核清单、处理和报告状态的受控 Harness 能力。
- `backend/tests/fixtures/audit_elements/energy_management.json`: 固定审核要素目录。

### Task 1: 证据、要素状态和报告版本模型

**Files:**
- Modify: `backend/app/db/models.py`
- Create: `backend/tests/test_audit_evidence.py`
- Create: `backend/tests/test_audit_reporting.py`

**Interfaces:**
- Consumes: Phase 2 的 `AuditCase`、`AuditCaseMaterial`、`AuditCaseMaterialChunk`。
- Produces: `AuditEvidenceLedger`、`AuditElementCoverage`、`AuditReportVersion`、`AuditReportSection`。

- [ ] **Step 1: 编写模型唯一性测试**

```python
def test_evidence_and_report_rows_have_stable_idempotency_keys() -> None:
    with _test_session() as db:
        ledger = AuditEvidenceLedger(
            tenant_id="tenant_demo",
            audit_case_id="case-1",
            audit_element_id="4.4.3",
            source_kind="case_material",
            source_id="material-1",
            source_version_id="material-1:v1",
            chunk_id="chunk-1",
            source_ref="审核记录.pdf#chunk=0",
            evidence_type="conformity",
            evidence_text="组织已识别主要能源使用。",
            confidence=0.91,
            extractor_version="audit-evidence-v1",
        )
        db.add(ledger)
        db.commit()
        assert ledger.report_section_ids_json == []
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_audit_evidence.py::test_evidence_and_report_rows_have_stable_idempotency_keys -v`

Expected: FAIL，新模型尚不存在。

- [ ] **Step 3: 增加证据和要素状态模型**

```python
class AuditEvidenceLedger(SQLModel, table=True):
    __tablename__ = "audit_evidence_ledger"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "audit_case_id", "audit_element_id", "source_kind",
            "chunk_id", "extractor_version", name="uq_audit_evidence_source"
        ),
    )

    id: str = Field(default_factory=lambda: new_id("auditev"), primary_key=True)
    tenant_id: str = Field(index=True)
    audit_case_id: str = Field(index=True)
    audit_element_id: str = Field(index=True)
    source_kind: str = Field(index=True)
    source_id: str = Field(index=True)
    source_version_id: str = Field(index=True)
    chunk_id: str = Field(index=True)
    source_ref: str
    evidence_type: str = Field(index=True)
    evidence_text: str
    confidence: float
    extractor_version: str = Field(index=True)
    report_section_ids_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)


class AuditElementCoverage(SQLModel, table=True):
    __tablename__ = "audit_element_coverage"
    __table_args__ = (
        UniqueConstraint("tenant_id", "audit_case_id", "audit_element_id", name="uq_audit_element_coverage"),
    )

    id: str = Field(default_factory=lambda: new_id("auditcov"), primary_key=True)
    tenant_id: str = Field(index=True)
    audit_case_id: str = Field(index=True)
    audit_element_id: str = Field(index=True)
    status: str = Field(default="pending", index=True)
    material_evidence_count: int = 0
    knowledge_evidence_count: int = 0
    gap_reason: Optional[str] = None
    lead_auditor_confirmed_by: Optional[str] = None
    updated_at: datetime = Field(default_factory=utc_now)
```

- [ ] **Step 4: 增加报告版本和章节模型**

```python
class AuditReportVersion(SQLModel, table=True):
    __tablename__ = "audit_report_versions"

    id: str = Field(default_factory=lambda: new_id("auditreport"), primary_key=True)
    tenant_id: str = Field(index=True)
    audit_case_id: str = Field(index=True)
    version: int = Field(index=True)
    status: str = Field(default="draft", index=True)
    material_version_ids_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    knowledge_base_version_ids_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    coverage_snapshot_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    final_storage_key: Optional[str] = None
    lead_auditor_confirmed_by: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AuditReportSection(SQLModel, table=True):
    __tablename__ = "audit_report_sections"
    __table_args__ = (
        UniqueConstraint("report_version_id", "section_id", name="uq_audit_report_section"),
    )

    id: str = Field(default_factory=lambda: new_id("auditsection"), primary_key=True)
    tenant_id: str = Field(index=True)
    audit_case_id: str = Field(index=True)
    report_version_id: str = Field(index=True)
    section_id: str = Field(index=True)
    title: str
    sequence: int
    audit_element_ids_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    status: str = Field(default="pending", index=True)
    draft_markdown: str = ""
    citation_ids_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    model_config_id: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    retry_count: int = 0
    error_code: Optional[str] = None
    updated_at: datetime = Field(default_factory=utc_now)
```

同时扩展 Phase 2 的 `AuditCaseService.delete_case`：在删除材料和项目行前，按 `(tenant_id, audit_case_id)` 删除本阶段新增的报告章节、报告版本、要素覆盖和证据台账；删除后 `AuditCaseEvent` 继续保留。知识版本变更、证据处理批次、报告生成和人工确认分别写入 `AuditCaseEvent`，元数据只保存受控 ID、状态和计数。

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_audit_evidence.py tests/test_audit_reporting.py -k "model or idempotency" -v`

Expected: PASS。

- [ ] **Step 5: 提交台账模型**

```powershell
git add backend/app/db/models.py backend/tests/test_audit_evidence.py backend/tests/test_audit_reporting.py
git commit -m "feat: add audit evidence and report models"
```

### Task 2: 稳定审核要素目录

**Files:**
- Create: `backend/app/audit_cases/elements.py`
- Create: `backend/tests/fixtures/audit_elements/energy_management.json`
- Modify: `backend/tests/test_audit_evidence.py`

**Interfaces:**
- Consumes: `AuditCase.management_systems_json`。
- Produces: `AuditElement`、`load_required_elements(management_systems: list[str]) -> list[AuditElement]`。

- [ ] **Step 1: 编写要素加载与去重测试**

```python
def test_required_elements_are_stable_and_unique() -> None:
    elements = load_required_elements(["GB/T 23331-2020"])
    assert elements
    assert len({item.id for item in elements}) == len(elements)
    assert all(item.query_templates for item in elements)
    assert all(item.report_section_id for item in elements)
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_audit_evidence.py::test_required_elements_are_stable_and_unique -v`

Expected: FAIL，目录加载器尚不存在。

- [ ] **Step 3: 定义严格要素类型**

```python
class AuditElement(BaseModel):
    id: str
    management_system: str
    title: str
    requirement: str
    query_templates: list[str] = Field(min_length=1)
    report_section_id: str
    required: bool = True


def load_required_elements(management_systems: list[str]) -> list[AuditElement]:
    selected = set(management_systems)
    rows = json.loads(ELEMENT_FIXTURE.read_text(encoding="utf-8"))
    result = [AuditElement.model_validate(row) for row in rows if row["management_system"] in selected]
    by_id = {row.id: row for row in result}
    if len(by_id) != len(result):
        raise ValueError("duplicate audit element id")
    return [by_id[key] for key in sorted(by_id)]
```

fixture 中每个要素必须给出确定的 `query_templates` 和 `report_section_id`；测试不允许从模型动态生成必需要素目录。

- [ ] **Step 4: 运行目录测试并提交**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_audit_evidence.py -k "required_elements" -v`

Expected: PASS。

```powershell
git add backend/app/audit_cases/elements.py backend/tests/fixtures/audit_elements/energy_management.json backend/tests/test_audit_evidence.py
git commit -m "feat: add stable audit element catalog"
```

### Task 3: 块级证据抽取和幂等恢复

**Files:**
- Create: `backend/app/audit_cases/evidence_schema.py`
- Create: `backend/app/audit_cases/evidence.py`
- Modify: `backend/tests/test_audit_evidence.py`

**Interfaces:**
- Consumes: `AuditCaseMaterialChunk`、`AuditElement`、`LLMClient.generate_json`。
- Produces: `ExtractedEvidence`、`AuditEvidenceProcessor.process_pending_chunks(case, model_config, batch_size=4) -> ProcessingSummary`。

- [ ] **Step 1: 编写已成功块不重复调用测试**

```python
def test_processor_retries_only_pending_or_failed_chunks() -> None:
    processor, chunks, fake_client = _processor_with_chunks(["succeeded", "pending", "failed"])
    summary = processor.process_pending_chunks(case, model_config, batch_size=2)
    assert fake_client.chunk_ids == [chunks[1].id, chunks[2].id]
    assert summary.succeeded == 2
    assert summary.skipped == 1
```

- [ ] **Step 2: 编写严格引用测试**

```python
def test_extracted_evidence_must_reference_known_element() -> None:
    with pytest.raises(AuditEvidenceError, match="INVALID_AUDIT_ELEMENT_REFERENCE"):
        validate_extraction_result({
            "chunk_id": "chunk-1",
            "items": [{
                "audit_element_id": "unknown",
                "evidence_type": "conformity",
                "evidence_text": "无来源要素",
                "confidence": 0.8,
            }],
        }, allowed_element_ids={"4.4.3"})
```

- [ ] **Step 3: 运行测试并确认失败**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_audit_evidence.py -k "retries_only or known_element" -v`

Expected: FAIL，处理器和 schema 尚不存在。

- [ ] **Step 4: 定义模型输出契约**

```python
class ExtractedEvidence(BaseModel):
    audit_element_id: str
    evidence_type: Literal["conformity", "improvement", "nonconformity", "context"]
    evidence_text: str = Field(min_length=1, max_length=2_000)
    confidence: float = Field(ge=0.0, le=1.0)


class ChunkExtractionResult(BaseModel):
    chunk_id: str
    items: list[ExtractedEvidence] = Field(default_factory=list)


class ProcessingSummary(BaseModel):
    total: int
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0


class AuditEvidenceError(RuntimeError):
    pass


def validate_extraction_result(
    payload: dict[str, Any], *, allowed_element_ids: set[str]
) -> ChunkExtractionResult:
    result = ChunkExtractionResult.model_validate(payload)
    unknown = sorted({
        item.audit_element_id
        for item in result.items
        if item.audit_element_id not in allowed_element_ids
    })
    if unknown:
        raise AuditEvidenceError(
            "INVALID_AUDIT_ELEMENT_REFERENCE:" + ",".join(unknown)
        )
    return result
```

`validate_extraction_result` 在任何数据库写入前运行；未知要素会让整块标记 `failed` 和 `INVALID_AUDIT_ELEMENT_REFERENCE`，不写入部分结果。

- [ ] **Step 5: 实现幂等处理器**

```python
class AuditEvidenceProcessor:
    EXTRACTOR_VERSION = "audit-evidence-v1"

    def process_pending_chunks(
        self, case: AuditCase, model_config: ModelConfig, batch_size: int = 4
    ) -> ProcessingSummary:
        elements = load_required_elements(case.management_systems_json)
        allowed = {item.id for item in elements}
        chunks = self._chunks_for_case(case.id)
        pending = [chunk for chunk in chunks if chunk.processing_status != "succeeded"]
        summary = ProcessingSummary(total=len(chunks), skipped=len(chunks) - len(pending))
        for batch in _batches(pending, batch_size):
            for chunk in batch:
                try:
                    result = self._extract_chunk(chunk, elements, model_config)
                    self._replace_chunk_evidence(case, chunk, result, allowed)
                    chunk.processing_status = "succeeded"
                    summary.succeeded += 1
                except (LLMError, ValidationError, AuditEvidenceError) as exc:
                    chunk.processing_status = "failed"
                    chunk.extracted_facts_json = [{"error_code": _evidence_error_code(exc)}]
                    summary.failed += 1
                self.db.add(chunk)
                self.db.commit()
        return summary
```

同一文件中的辅助函数必须给出确定实现：

```python
def _batches(rows: list[AuditCaseMaterialChunk], size: int) -> Iterator[list[AuditCaseMaterialChunk]]:
    if size < 1:
        raise ValueError("batch_size must be positive")
    for start in range(0, len(rows), size):
        yield rows[start:start + size]


def _evidence_error_code(exc: Exception) -> str:
    text = str(exc)
    if text.startswith("INVALID_AUDIT_ELEMENT_REFERENCE"):
        return "INVALID_AUDIT_ELEMENT_REFERENCE"
    if isinstance(exc, ValidationError):
        return "INVALID_EXTRACTION_JSON"
    if isinstance(exc, LLMError):
        return "EVIDENCE_MODEL_FAILED"
    return "EVIDENCE_PROCESSING_FAILED"


def _extract_chunk(
    self,
    chunk: AuditCaseMaterialChunk,
    elements: list[AuditElement],
    model_config: ModelConfig,
) -> ChunkExtractionResult:
    payload = LLMClient(model_config).generate_json(
        AUDIT_EVIDENCE_PROMPT,
        {
            "chunk_id": chunk.id,
            "source_ref": f"{chunk.material_id}#chunk={chunk.chunk_index}",
            "content": chunk.content,
            "allowed_elements": [item.model_dump(mode="json") for item in elements],
        },
    )
    result = validate_extraction_result(
        payload, allowed_element_ids={item.id for item in elements}
    )
    if result.chunk_id != chunk.id:
        raise AuditEvidenceError("EXTRACTION_CHUNK_ID_MISMATCH")
    return result
```

`_replace_chunk_evidence` 在单个事务中删除同一 `(tenant_id, audit_case_id, chunk_id, extractor_version)` 的旧行，再逐项写入新的 `AuditEvidenceLedger`；任何一项失败都回滚该块，不能留下半块证据。

模型输入只包含单块正文、材料受控引用、要素 ID/标题/要求，不包含其他块全文。

- [ ] **Step 6: 运行证据处理测试并提交**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_audit_evidence.py -k "processor or extraction" -v`

Expected: PASS。

```powershell
git add backend/app/audit_cases/evidence_schema.py backend/app/audit_cases/evidence.py backend/tests/test_audit_evidence.py
git commit -m "feat: extract audit evidence by material chunk"
```

### Task 4: 按审核要素编排稳定知识检索

**Files:**
- Create: `backend/app/audit_cases/knowledge.py`
- Create: `backend/tests/test_audit_knowledge_orchestrator.py`

**Interfaces:**
- Consumes: `KnowledgeService.search(KnowledgeSearchRequest, model_config)`、固定要素查询组和项目固定知识库版本。
- Produces: `AuditKnowledgeOrchestrator.retrieve(case, model_config) -> KnowledgeRetrievalSummary`。

- [ ] **Step 1: 编写每个要素均执行查询的测试**

```python
def test_orchestrator_records_hits_and_zero_hits_for_every_element() -> None:
    fake_search = FakeKnowledgeSearch({"4.4.3": [knowledge_chunk], "4.6.1": []})
    summary = AuditKnowledgeOrchestrator(db, search=fake_search).retrieve(case, model_config)
    assert summary.queried_element_ids == ["4.4.3", "4.6.1"]
    assert _coverage("4.4.3").knowledge_evidence_count == 1
    assert _coverage("4.6.1").status == "evidence_gap"
    assert _coverage("4.6.1").gap_reason == "KNOWLEDGE_ZERO_HIT"
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_audit_knowledge_orchestrator.py -v`

Expected: FAIL，编排器尚不存在。

- [ ] **Step 3: 实现固定版本查询**

```python
def retrieve(
    self, case: AuditCase, model_config: ModelConfig | None
) -> KnowledgeRetrievalSummary:
    summary = KnowledgeRetrievalSummary()
    for element in load_required_elements(case.management_systems_json):
        hits: dict[str, KnowledgeChunkRead] = {}
        for template in element.query_templates:
            response = self.search(
                KnowledgeSearchRequest(
                    tenant_id=case.tenant_id,
                    query=template.format(
                        organization_name=case.organization_name,
                        requirement=element.requirement,
                    ),
                    query_type="policy_check",
                    knowledge_base_version_ids=case.knowledge_base_version_ids_json,
                    max_chunks=8,
                ),
                model_config,
            )
            for chunk in response.chunks:
                hits[chunk.id] = chunk
        self._replace_element_knowledge(case, element, list(hits.values()))
        summary.queried_element_ids.append(element.id)
    return summary
```

知识台账记录 `source_kind="knowledge"`、`source_id=document_id`、`source_version_id=knowledge_base_version_id`、`chunk_id` 和 `source_ref`。不得复用普通 Harness 的两次调用预算。

- [ ] **Step 4: 运行知识编排测试并提交**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_audit_knowledge_orchestrator.py -v`

Expected: PASS，零命中和低置信度均有明确状态。

```powershell
git add backend/app/audit_cases/knowledge.py backend/tests/test_audit_knowledge_orchestrator.py
git commit -m "feat: retrieve knowledge for every audit element"
```

### Task 5: 三类覆盖率和发布 Gate

**Files:**
- Create: `backend/app/audit_cases/coverage.py`
- Modify: `backend/app/audit_cases/schema.py`
- Modify: `backend/tests/test_audit_evidence.py`

**Interfaces:**
- Consumes: 当前材料、全部块、要素目录和 `AuditElementCoverage`。
- Produces: `AuditCoverageSnapshot`、`calculate_coverage(db, case) -> AuditCoverageSnapshot`、`require_publishable(snapshot)`。

- [ ] **Step 1: 编写三类覆盖率测试**

```python
def test_coverage_gate_requires_all_files_chunks_and_elements() -> None:
    snapshot = calculate_coverage(db, case)
    assert snapshot.file_coverage == 1.0
    assert snapshot.chunk_coverage == 0.5
    assert snapshot.element_coverage == 1.0
    assert snapshot.publish_allowed is False
    assert snapshot.blockers == ["CHUNK_COVERAGE_INCOMPLETE"]
```

- [ ] **Step 2: 编写字符区间空洞测试**

```python
def test_chunk_interval_gap_blocks_publish_even_when_status_succeeded() -> None:
    snapshot = calculate_coverage(db, case_with_chunks([(0, 900), (901, 1800)]))
    assert "CHUNK_INTERVAL_GAP" in snapshot.blockers
    assert snapshot.publish_allowed is False
```

- [ ] **Step 3: 运行测试并确认失败**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_audit_evidence.py -k "coverage_gate or interval_gap" -v`

Expected: FAIL，覆盖模块尚不存在。

- [ ] **Step 4: 实现覆盖快照**

```python
class AuditCoverageSnapshot(BaseModel):
    file_coverage: float
    chunk_coverage: float
    element_coverage: float
    publish_allowed: bool
    blockers: list[str]
    files_total: int
    files_succeeded: int
    chunks_total: int
    chunks_succeeded: int
    elements_total: int
    elements_resolved: int


RESOLVED_ELEMENT_STATUSES = {
    "evidence_found", "knowledge_found", "evidence_gap", "not_applicable"
}
```

`calculate_coverage` 还必须逐材料验证 `chunks[0].start_char == 0`、相邻区间相接、最后 `end_char == material.characters`。

- [ ] **Step 5: 运行覆盖测试并提交**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_audit_evidence.py -k "coverage" -v`

Expected: PASS。

```powershell
git add backend/app/audit_cases/coverage.py backend/app/audit_cases/schema.py backend/tests/test_audit_evidence.py
git commit -m "feat: enforce audit evidence coverage gate"
```

### Task 6: 分章节生成、断点恢复和确定性合并

**Files:**
- Create: `backend/app/audit_cases/reporting.py`
- Modify: `backend/tests/test_audit_reporting.py`

**Interfaces:**
- Consumes: `AuditCoverageSnapshot`、要素和证据台账、模板材料、`LLMClient`。
- Produces: `AuditReportService.create_version`、`generate_pending_sections`、`assemble_draft`、`publish`。

- [ ] **Step 1: 编写章节只加载相关证据测试**

```python
def test_section_generation_receives_only_mapped_evidence() -> None:
    service, fake_client, report = _report_service()
    service.generate_pending_sections(case, report, model_config)
    management_payload = fake_client.payload_for("management_summary")
    assert {item["audit_element_id"] for item in management_payload["evidence"]} == {"4.4.3", "4.4.4"}
    assert "unrelated-secret" not in json.dumps(management_payload, ensure_ascii=False)
```

- [ ] **Step 2: 编写中断恢复测试**

```python
def test_report_resume_skips_completed_sections() -> None:
    service, fake_client, report = _report_service(section_statuses=["succeeded", "failed", "pending"])
    service.generate_pending_sections(case, report, model_config)
    assert fake_client.generated_section_ids == ["section-2", "section-3"]
```

- [ ] **Step 3: 运行测试并确认失败**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_audit_reporting.py -k "mapped_evidence or resume" -v`

Expected: FAIL，报告服务尚不存在。

- [ ] **Step 4: 实现章节计划和生成**

```python
class ReportSectionSpec(BaseModel):
    id: str
    title: str
    sequence: int
    audit_element_ids: list[str]


def generate_pending_sections(
    self, case: AuditCase, report: AuditReportVersion, model_config: ModelConfig
) -> None:
    for section in self._pending_sections(report.id):
        evidence = self._section_evidence(case.id, section.audit_element_ids_json)
        try:
            draft = LLMClient(model_config).generate_text(
                REPORT_SECTION_PROMPT,
                {
                    "section_id": section.section_id,
                    "title": section.title,
                    "evidence": [ledger_public_payload(item) for item in evidence],
                    "citation_rule": "每项事实使用 [EVIDENCE:<id>] 引用",
                },
            )
            citation_ids = validate_section_citations(draft, evidence)
            section.draft_markdown = draft
            section.citation_ids_json = citation_ids
            section.status = "succeeded"
            section.error_code = None
        except (LLMError, AuditCitationError) as exc:
            section.status = "failed"
            section.retry_count += 1
            section.error_code = (
                "REPORT_CITATION_INVALID"
                if isinstance(exc, AuditCitationError)
                else "REPORT_MODEL_FAILED"
            )
        self.db.add(section)
        self.db.commit()
```

引用校验和 DOCX 输出采用明确接口：

```python
EVIDENCE_CITATION = re.compile(r"\[EVIDENCE:([A-Za-z0-9_-]+)\]")


def validate_section_citations(
    markdown: str, evidence: list[AuditEvidenceLedger]
) -> list[str]:
    allowed = {item.id for item in evidence}
    cited = EVIDENCE_CITATION.findall(markdown)
    if not cited or any(item not in allowed for item in cited):
        raise AuditCitationError("REPORT_CITATION_INVALID")
    return list(dict.fromkeys(cited))


def render_docx(markdown: str, *, title: str) -> bytes:
    document = Document()
    document.add_heading(title, level=0)
    for block in markdown.split("\n\n"):
        if block.startswith("## "):
            document.add_heading(block.removeprefix("## "), level=1)
        elif block.strip():
            document.add_paragraph(block)
    stream = BytesIO()
    document.save(stream)
    return stream.getvalue()
```

模板分支独立实现 `render_docx_from_template(template_bytes, sections)`；仅当模板材料存在时调用，并严格检查每个 `{{SECTION:<section_id>}}` 锚点恰好出现一次。

- [ ] **Step 5: 实现合并和发布限制**

```python
def publish(
    self, case: AuditCase, report: AuditReportVersion, confirmed_by: str | None
) -> AuditReportVersion:
    snapshot = calculate_coverage(self.db, case)
    require_publishable(snapshot)
    if any(row.status != "succeeded" for row in self._sections(report.id)):
        raise AuditReportBlocked("REPORT_SECTIONS_INCOMPLETE")
    markdown = "\n\n".join(
        f"## {row.title}\n\n{row.draft_markdown}"
        for row in sorted(self._sections(report.id), key=lambda item: item.sequence)
    )
    filename = "审核报告.docx" if confirmed_by else "审核报告-待确认草稿.docx"
    data = render_docx(markdown, title=filename.removesuffix(".docx"))
    report.final_storage_key = write_report_blob(case, report, filename, data)
    report.coverage_snapshot_json = snapshot.model_dump(mode="json")
    report.status = "published" if confirmed_by else "review"
    report.lead_auditor_confirmed_by = confirmed_by
    self.db.add(report)
    self.db.commit()
    return report
```

`render_docx` 使用 `python-docx` 按章节顺序写入；若项目有 `material_type=report_template` 的 DOCX，则读取模板并只替换明确命名的章节占位符，找不到占位符时阻止发布并返回 `REPORT_TEMPLATE_ANCHOR_MISSING`。

- [ ] **Step 6: 运行报告测试并提交**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_audit_reporting.py -v`

Expected: PASS，所有引用有效，失败章节可恢复，覆盖不完整不能发布。

```powershell
git add backend/app/audit_cases/reporting.py backend/tests/test_audit_reporting.py
git commit -m "feat: generate traceable audit report sections"
```

### Task 7: 处理、覆盖和报告 API 及 Harness 能力

**Files:**
- Modify: `backend/app/api/audit_cases.py`
- Create: `backend/app/core/harness_audit_capabilities.py`
- Modify: `backend/app/core/capability_manifest.py`
- Modify: `backend/app/core/harness_capability_invoker.py`
- Modify: `backend/app/core/harness_v2_engine.py`
- Modify: `backend/tests/test_audit_case_api.py`
- Modify: `backend/tests/test_harness_v2.py`

**Interfaces:**
- Consumes: Tasks 3-6 服务。
- Produces: `/process`、`/coverage`、`/reports` API；`audit_case_manifest`、`audit_evidence_process`、`audit_report_status` 三个受控能力。

- [ ] **Step 1: 编写 API 状态流测试**

```python
def test_process_coverage_and_report_api_flow(client, auth_headers, ready_case) -> None:
    processed = client.post(
        f"/api/audit-cases/{ready_case.id}/process?tenant_id=tenant_demo",
        headers=auth_headers,
        json={"model_config_id": "model-1"},
    )
    assert processed.status_code == 202
    coverage = client.get(
        f"/api/audit-cases/{ready_case.id}/coverage?tenant_id=tenant_demo",
        headers=auth_headers,
    )
    assert set(coverage.json()) >= {"file_coverage", "chunk_coverage", "element_coverage", "publish_allowed"}
```

- [ ] **Step 2: 编写普通聊天不出现审核能力测试**

```python
def test_audit_capabilities_exist_only_for_bound_audit_case() -> None:
    ordinary = builder.build("tenant_demo", "agent-1", None, None, audit_case_id=None)
    bound = builder.build("tenant_demo", "agent-1", None, None, audit_case_id="case-1")
    assert "audit_case_manifest" not in ordinary.allowed_names()
    assert "audit_case_manifest" in bound.allowed_names()
```

- [ ] **Step 3: 运行测试并确认失败**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_audit_case_api.py tests/test_harness_v2.py -k "process_coverage or audit_capabilities" -v`

Expected: FAIL，端点和能力尚不存在。

- [ ] **Step 4: 实现受控能力结果**

```python
def audit_capability_descriptors(audit_case_id: str | None) -> list[CapabilityDescriptor]:
    if not audit_case_id:
        return []
    return [
        CapabilityDescriptor(capability_id="audit.case.manifest", name="audit_case_manifest", kind="internal"),
        CapabilityDescriptor(capability_id="audit.evidence.process", name="audit_evidence_process", kind="internal"),
        CapabilityDescriptor(capability_id="audit.report.status", name="audit_report_status", kind="internal"),
    ]
```

把现有 `CapabilityManifestBuilder.build` 扩展为可选的关键字参数，并保持旧调用兼容：

```python
def build(
    self,
    tenant_id: str,
    agent_id: str | None,
    skill: Skill | None,
    step_id: str | None,
    *,
    audit_case_id: str | None = None,
) -> CapabilityManifest:
    # 保留现有 agent、Skill、SOP 和内置能力构建逻辑。
    available.extend(audit_capability_descriptors(audit_case_id))
```

`harness_v2_engine.py` 的唯一调用点传入 `audit_case_id=session.audit_case_id`；其他调用方省略关键字时行为不变。

Invoker 不接受模型提供 `tenant_id` 或 `audit_case_id`，只使用当前 session 已授权的项目 ID。返回结果只含状态、计数和受控引用，不含材料全文。

- [ ] **Step 5: 完成 API 和能力测试并提交**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_audit_case_api.py tests/test_harness_v2.py -k "audit" -v`

Expected: PASS。

```powershell
git add backend/app/api/audit_cases.py backend/app/core/harness_audit_capabilities.py backend/app/core/capability_manifest.py backend/app/core/harness_capability_invoker.py backend/app/core/harness_v2_engine.py backend/tests/test_audit_case_api.py backend/tests/test_harness_v2.py
git commit -m "feat: expose controlled audit pipeline capabilities"
```

### Task 8: 更新 SOP 强制能力和固定样本端到端测试

**Files:**
- Modify: `backend/app/db/seed_fixtures/audit_report_generation_sop_v2.json`
- Modify: `backend/tests/test_audit_report_sop_v2.py`
- Create: `backend/tests/fixtures/audit_case_40k/manifest.json`
- Create: `backend/tests/test_audit_report_e2e.py`

**Interfaces:**
- Consumes: `audit_case_manifest`、`audit_evidence_process`、`audit_report_status` 和项目固定知识库版本。
- Produces: 可验证的完整审核报告工作流。

- [ ] **Step 1: 固定 SOP 节点能力断言**

```python
def test_audit_report_sop_requires_structured_pipeline_capabilities() -> None:
    card = load_audit_report_sop_v2()
    by_id = {node.node_id: node for node in card.nodes}
    assert "audit_case_manifest" in by_id["collect_materials"].allowed_actions
    assert "audit_evidence_process" in by_id["build_material_evidence_ledger"].allowed_actions
    assert "audit_report_status" in by_id["consistency_and_coverage_check"].allowed_actions
    assert by_id["retrieve_reference_knowledge"].capability_refs.required_knowledge_base_ids
```

- [ ] **Step 2: 创建 40,000 字固定样本清单**

`manifest.json` 记录样本角色而不是提交敏感企业材料：

```json
{
  "organization_name": "StaffDeck 审核流水线测试企业",
  "report_type": "再认证",
  "management_systems": ["GB/T 23331-2020"],
  "generated_files": [
    {"filename": "审核计划.txt", "material_type": "audit_plan", "minimum_characters": 2000},
    {"filename": "审核记录.txt", "material_type": "audit_record", "minimum_characters": 40000},
    {"filename": "绩效确认表.txt", "material_type": "performance_record", "minimum_characters": 3000},
    {"filename": "报告模板.docx", "material_type": "report_template", "required_anchors": ["管理体系概况", "审核证据", "改进建议"]}
  ]
}
```

- [ ] **Step 3: 编写端到端断言**

```python
def test_40k_case_processes_all_text_and_resumes_report_generation() -> None:
    case = create_fixture_case(minimum_audit_record_chars=40_000)
    process_all_materials(case)
    first_run = generate_report(case, fail_once_on_section="improvements")
    assert first_run.status == "failed"
    second_run = generate_report(case)
    snapshot = calculate_coverage(db, case)
    assert snapshot.file_coverage == 1.0
    assert snapshot.chunk_coverage == 1.0
    assert snapshot.element_coverage == 1.0
    assert second_run.regenerated_section_ids == ["improvements"]
    assert every_report_citation_resolves(second_run)
```

- [ ] **Step 4: 运行端到端和完整后端测试**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_audit_report_sop_v2.py tests/test_audit_report_e2e.py -v`

Expected: PASS。

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest -q`

Expected: PASS。

- [ ] **Step 5: 运行 Ruff 并提交**

Run: `cd backend; .\.venv\Scripts\python.exe -m ruff check app tests`

Expected: PASS。

```powershell
git add backend/app/db/seed_fixtures/audit_report_generation_sop_v2.json backend/tests/test_audit_report_sop_v2.py backend/tests/fixtures/audit_case_40k/manifest.json backend/tests/test_audit_report_e2e.py
git commit -m "test: verify full audit evidence report pipeline"
```
