# Audit Report Runtime Traceability Phase 5A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the audited evidence-to-report runtime path with explicit project entry points, immutable rule/material/knowledge snapshots, and a shared HTTP/Harness execution surface.

**Architecture:** Keep material extraction, evidence processing, report drafting, and final confirmation as separate states. Extend the existing report service with a non-inventive snapshot of the project’s current published rule bindings, expose additive API contracts plus a scoped download route, then let the project UI and Harness invoke those same services. Drafts remain compatible for projects without rules, while confirmed publication is blocked until a published binding exists.

**Tech Stack:** Python 3.11, FastAPI, SQLModel/SQLite, Pydantic v2, pytest, React 18, TypeScript, Vitest, Testing Library, existing authenticated blob client.

**Spec:** `docs/superpowers/specs/2026-09-05-audit-report-runtime-traceability-design.md`

## Global Constraints

- Do not create, seed, infer, or modify certification business rules; an empty rule library is an explicit administrator-configuration state.
- Preserve existing audit-case, material, evidence, knowledge, and report API behavior; new response fields are additive.
- Draft generation may report `rule_traceability_status=not_configured`; confirmed publication must fail with `RULE_BINDING_REQUIRED_FOR_PUBLISH` when no current published binding exists.
- Every report and download query must enforce the authenticated tenant, project authorization, and archive read-only behavior.
- Only current published bindings are snapshotted; later rule migration must not mutate an older report.
- A section receives only its mapped evidence plus the controlled rule metadata selected for `generate_report_sections`/`audit_report`; no material全文 or host path enters API/Harness results.
- Every behavior-changing edit starts with a failing test and ends with focused tests, relevant frontend build, and the required Windows runtime checks.
- Work directly in the existing detached worktree; retain user data and do not run destructive Git or database commands.

## File Map

### Backend

- `backend/app/db/models.py`: add report-level rule snapshot fields and section-level applied rule IDs.
- `backend/app/db/database.py`: add an idempotent additive migration for the new report columns.
- `backend/app/audit_cases/schema.py`: expose process/report/publish/download DTOs and stable errors.
- `backend/app/audit_cases/reporting.py`: resolve current published bindings, build rule snapshots, pass rule context to sections, and enforce the confirmed-publication rule gate.
- `backend/app/api/audit_cases.py`: return structured process results, publish an existing version, and stream a scoped report blob.
- `backend/app/core/harness_audit_capabilities.py`, `backend/app/core/harness_capability_invoker.py`: expose the scoped report-generation capability and its status payload.
- `backend/app/db/seed_fixtures/audit_report_generation_sop_v2.json`: authorize report generation at the report-section node.
- `backend/tests/test_audit_report_runtime_traceability.py`: model, migration, service, API, download, and Harness regression coverage.
- Existing `backend/tests/test_audit_reporting.py`, `test_audit_case_api.py`, `test_harness_v2.py`, and `test_audit_report_sop_v2.py`: extend compatibility assertions.

### Frontend

- `frontend-enterprise/src/types/index.ts`: add process/report DTOs and optional coverage fields.
- `frontend-enterprise/src/pages/audit-cases/auditCaseApi.ts`: add the project evidence-process client.
- `frontend-enterprise/src/pages/audit-cases/auditReportApi.ts`: add typed list/create/publish/download calls.
- `frontend-enterprise/src/pages/audit-cases/auditReportApi.test.ts`: assert exact authenticated URLs and request bodies.
- `frontend-enterprise/src/pages/audit-cases/components/EvidenceReportPanel.tsx`: provide evidence processing, draft generation, report status, publication, and download actions.
- `frontend-enterprise/src/pages/audit-cases/components/EvidenceReportPanel.test.tsx`: cover gates, warnings, and state preservation.
- `frontend-enterprise/src/pages/audit-cases/AuditCaseDetailPage.tsx` and its test: add the new project tab without changing existing tabs.

---

### Task 1: Add immutable report rule-snapshot schema and migration

**Files:**

- Modify: `backend/app/db/models.py` near `AuditReportVersion` and `AuditReportSection`.
- Modify: `backend/app/db/database.py` with `_migrate_audit_report_traceability_schema` and its startup call.
- Modify: `backend/app/audit_cases/schema.py` with additive read/request models.
- Test: `backend/tests/test_audit_report_runtime_traceability.py`.

**Interfaces:**

- `AuditReportVersion.rule_set_version_ids_json: list[str]` defaults to `[]`.
- `AuditReportVersion.rule_traceability_status: str` defaults to `"not_configured"`.
- `AuditReportSection.rule_definition_ids_json: list[str]` defaults to `[]`.
- `AuditReportRead` exposes `rule_set_version_ids`, `rule_traceability_status`; `AuditReportSectionRead` exposes `rule_definition_ids`.
- `AuditCaseProcessRead` exposes `status`, `materials`, `coverage`, and optional `evidence`/`knowledge` summaries.
- `AuditReportPublishRequest` contains no user identity field; the route derives confirmer identity from the authenticated user.

- [ ] **Step 1: Write failing model and migration tests**

```python
def test_report_rows_default_to_unconfigured_rule_traceability() -> None:
    with _test_session() as db:
        row = AuditReportVersion(tenant_id="tenant_demo", audit_case_id="case-1", version=1)
        section = AuditReportSection(
            tenant_id="tenant_demo", audit_case_id="case-1", report_version_id=row.id,
            section_id="summary", title="摘要", sequence=1,
        )
        db.add(row)
        db.add(section)
        db.commit()
        assert row.rule_set_version_ids_json == []
        assert row.rule_traceability_status == "not_configured"
        assert section.rule_definition_ids_json == []


def test_report_traceability_migration_is_additive_and_idempotent(tmp_path) -> None:
    engine = create_legacy_audit_report_engine(tmp_path)
    run_startup_migrations(engine)
    run_startup_migrations(engine)
    columns = {column["name"] for column in inspect(engine).get_columns("audit_report_versions")}
    assert {"rule_set_version_ids_json", "rule_traceability_status"} <= columns
    section_columns = {column["name"] for column in inspect(engine).get_columns("audit_report_sections")}
    assert "rule_definition_ids_json" in section_columns
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `backend\.venv\Scripts\python.exe -m pytest backend\tests\test_audit_report_runtime_traceability.py -k "default_to_unconfigured or traceability_migration" -v`

Expected: FAIL because the model fields and migration do not exist.

- [ ] **Step 3: Implement the additive fields and migration**

Add the SQLModel fields with JSON columns and defaults:

```python
rule_set_version_ids_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
rule_traceability_status: str = Field(default="not_configured", index=True)
rule_definition_ids_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
```

In the startup migration, inspect live columns before each `ALTER TABLE`; add JSON columns without rewriting existing rows, add the text status column with default `not_configured`, and record one migration marker in `app_data_migrations`. Existing reports remain unchanged except for the explicit default state.

- [ ] **Step 4: Run the focused model and migration tests**

Run: `backend\.venv\Scripts\python.exe -m pytest backend\tests\test_audit_report_runtime_traceability.py -k "default_to_unconfigured or traceability_migration" -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/db/models.py backend/app/db/database.py backend/app/audit_cases/schema.py backend/tests/test_audit_report_runtime_traceability.py
git commit -m "feat: add audit report rule snapshot fields"
```

### Task 2: Snapshot current rules and pass controlled rule context to report sections

**Files:**

- Modify: `backend/app/audit_cases/reporting.py`.
- Modify: `backend/tests/test_audit_reporting.py` and `backend/tests/test_audit_report_runtime_traceability.py`.

**Interfaces:**

- Add `ReportRuleSnapshot` with `version_ids`, `definition_ids_by_section`, `definitions`, and `status`.
- Add `AuditReportService.rule_snapshot(case) -> ReportRuleSnapshot`.
- `create_version(case)` stores the snapshot version IDs/status and initializes each section’s `rule_definition_ids_json`.
- `generate_pending_sections` sends a `rules` list containing only controlled rule metadata and persists section rule IDs.
- `publish(case, report, confirmed_by)` raises `AuditReportBlocked("RULE_BINDING_REQUIRED_FOR_PUBLISH")` when `confirmed_by` is non-empty and the report snapshot is not complete.

- [ ] **Step 1: Write failing snapshot and section-input tests**

```python
def test_report_version_freezes_current_published_bindings_and_section_rule_ids(binding_context) -> None:
    db, _engine, admin, _reviewer, case, _rule_set, _draft, published = binding_context
    RuleBindingService(db).bind_published_version(case, published.id, admin, "manual")
    report = AuditReportService(db).create_version(case)
    assert report.rule_set_version_ids_json == [published.id]
    assert report.rule_traceability_status == "complete"
    sections = db.exec(select(AuditReportSection).where(AuditReportSection.report_version_id == report.id)).all()
    assert all(section.rule_definition_ids_json for section in sections)


def test_section_generation_receives_only_report_applicable_rule_metadata(binding_context) -> None:
    service, fake_client, case, report = _report_service_with_bound_rules(binding_context)
    service.generate_pending_sections(case, report, None)
    payload = fake_client.payload_for(report.sections[0].section_id)
    assert {item["id"] for item in payload["rules"]} == set(report.sections[0].rule_definition_ids_json)
    assert all("evidence_text" not in item for item in payload["rules"])


def test_confirmed_publish_is_blocked_without_rule_binding(report_context, monkeypatch) -> None:
    service, case, report = report_context(with_rules=False)
    with pytest.raises(AuditReportBlocked, match="RULE_BINDING_REQUIRED_FOR_PUBLISH"):
        service.publish(case, report, confirmed_by="lead-auditor")
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `backend\.venv\Scripts\python.exe -m pytest backend\tests\test_audit_report_runtime_traceability.py backend\tests\test_audit_reporting.py -k "freezes_current or applicable_rule or blocked_without_rule" -v`

Expected: FAIL because reports do not yet store rule snapshots or send rule metadata.

- [ ] **Step 3: Implement deterministic rule snapshot resolution**

Read only `ProjectRuleBinding` rows with the case tenant, case ID, and `status == "current"`; resolve their `RuleSetVersion` and `RuleDefinition` rows with the same tenant and `status == "published"`. Sort version IDs and definition IDs deterministically. Include an enabled rule when `workflow_nodes_json` is empty or contains `generate_report_sections`, and when `document_types_json` is empty or contains `audit_report`; retain information-domain metadata in the payload instead of guessing a section mapping. Set status to `not_configured` for zero bindings and `incomplete` if a bound version or definition cannot be resolved.

Persist `rule_set_version_ids_json`, `rule_traceability_status`, and each section’s `rule_definition_ids_json` in `create_version`. Extend the section payload with a `rules` array containing IDs, stable rule keys, names, execution level/method, declared workflow nodes, information domains, document types, and source references. Never include rule source full text or material contents in this array.

In `publish`, keep review-draft behavior when `confirmed_by is None`; require status `complete` when a confirmer is present, and raise the stable blocked code otherwise. Do not run deterministic evaluations in this slice.

- [ ] **Step 4: Run reporting and compatibility tests**

Run: `backend\.venv\Scripts\python.exe -m pytest backend\tests\test_audit_report_runtime_traceability.py backend\tests\test_audit_reporting.py backend\tests\test_audit_pipeline_acceptance.py -q`

Expected: PASS, including the existing 40k pipeline acceptance; its no-rule fixture must remain a draft-compatible flow.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/audit_cases/reporting.py backend/tests/test_audit_reporting.py backend/tests/test_audit_report_runtime_traceability.py
git commit -m "feat: snapshot bound rules in audit reports"
```

### Task 3: Close the HTTP processing, report publication, and download contracts

**Files:**

- Modify: `backend/app/audit_cases/schema.py`.
- Modify: `backend/app/api/audit_cases.py`.
- Modify: `backend/tests/test_audit_case_api.py` and `backend/tests/test_audit_report_runtime_traceability.py`.

**Interfaces:**

- `POST /api/audit-cases/{case_id}/process` returns an additive `AuditCaseProcessRead`; a pending material response includes `code="MATERIAL_PROCESSING_PENDING"` and current coverage.
- `POST /api/audit-cases/{case_id}/reports` keeps current draft creation and returns rule snapshot fields.
- `POST /api/audit-cases/{case_id}/reports/{version_id}/publish` publishes an existing report using `current_user.id` as confirmer.
- `GET /api/audit-cases/{case_id}/reports/{version_id}/download` returns the stored DOCX bytes with a safe attachment filename.
- Stable errors map through `_case_error`: report not found, download not ready, report blocked, and archive write protection.

- [ ] **Step 1: Write failing API tests**

```python
def test_process_endpoint_exposes_evidence_and_coverage_status(api_context, ready_case) -> None:
    response = api_context.client.post(
        f"/api/audit-cases/{ready_case.id}/process?tenant_id=tenant_demo",
        headers=_headers(api_context.users["admin"]), json={},
    )
    assert response.status_code == 202
    assert set(response.json()) >= {"status", "coverage", "evidence", "knowledge"}


def test_report_publish_requires_binding_and_download_is_scoped(api_context, draft_report) -> None:
    publish = api_context.client.post(
        f"/api/audit-cases/{draft_report.audit_case_id}/reports/{draft_report.id}/publish?tenant_id=tenant_demo",
        headers=_headers(api_context.users["admin"]), json={},
    )
    assert publish.status_code == 409
    assert publish.json()["detail"] == "RULE_BINDING_REQUIRED_FOR_PUBLISH"


def test_report_download_returns_blob_only_after_final_storage_exists(api_context, published_report, monkeypatch) -> None:
    monkeypatch.setattr("app.api.audit_cases.read_case_blob", lambda key: b"docx-bytes")
    response = api_context.client.get(
        f"/api/audit-cases/{published_report.audit_case_id}/reports/{published_report.id}/download?tenant_id=tenant_demo",
        headers=_headers(api_context.users["admin"]),
    )
    assert response.status_code == 200
    assert response.content == b"docx-bytes"
    assert "attachment" in response.headers["content-disposition"]
```

- [ ] **Step 2: Run the API tests and verify RED**

Run: `backend\.venv\Scripts\python.exe -m pytest backend\tests\test_audit_report_runtime_traceability.py backend\tests\test_audit_case_api.py -k "process_endpoint or report_publish or report_download" -v`

Expected: FAIL because the structured response, publish route, and download route do not exist.

- [ ] **Step 3: Implement the additive endpoints**

Keep the existing body-sensitive process behavior. On pending extraction/chunking, return the existing material list and current coverage plus `code`; after evidence/knowledge processing, recalculate coverage and return summaries. For publication, load the requested version with tenant and case filters, reject archived cases, require the existing project admin/reviewer permission, and call `AuditReportService.publish(case, report, current_user.id)`. For download, load the same scoped version, require `final_storage_key`, read through `read_case_blob`, and return a DOCX `Response`; never return the storage key as a filesystem path or accept a caller-supplied path.

- [ ] **Step 4: Run focused API tests and existing audit API tests**

Run: `backend\.venv\Scripts\python.exe -m pytest backend\tests\test_audit_report_runtime_traceability.py backend\tests\test_audit_case_api.py backend\tests\test_audit_pipeline_acceptance.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/audit_cases/schema.py backend/app/api/audit_cases.py backend/tests/test_audit_case_api.py backend/tests/test_audit_report_runtime_traceability.py
git commit -m "feat: expose audit evidence and report runtime endpoints"
```

### Task 4: Expose report generation through the scoped Harness capability and SOP

**Files:**

- Modify: `backend/app/core/harness_audit_capabilities.py`.
- Modify: `backend/app/core/harness_capability_invoker.py`.
- Modify: `backend/app/db/seed_fixtures/audit_report_generation_sop_v2.json`.
- Modify: `backend/tests/test_harness_v2.py` and `backend/tests/test_audit_report_sop_v2.py`.

**Interfaces:**

- Add descriptor `audit.report.generate` / name `audit_report_generate`, visible only when `audit_case_id` is bound in the manifest.
- Invoker method `_audit_report_generate()` calls the same `AuditReportService.create_version` and `generate_pending_sections` used by HTTP, always creates a draft, and returns only `audit_case_id`, report ID/version/status, generation summary, rule traceability status, and section statuses.
- SOP node `generate_report_sections.allowed_actions` contains `audit_report_generate`; ordinary chats remain unchanged.

- [ ] **Step 1: Write failing Harness/SOP tests**

```python
def test_report_generate_capability_is_hidden_without_bound_case(manifest_builder) -> None:
    assert "audit_report_generate" not in manifest_builder.build(
        "tenant_demo", "agent-1", None, None, audit_case_id=None
    ).allowed_names()


def test_report_generate_capability_returns_scoped_draft_status(bound_session, fake_report_service) -> None:
    result = invoke_internal("audit_report_generate")
    assert result["success"] is True
    assert set(result["data"]) >= {"audit_case_id", "report", "rule_traceability_status"}
    assert "evidence_text" not in json.dumps(result, ensure_ascii=False)


def test_sop_requires_report_generation_capability() -> None:
    card = load_audit_report_sop_v2()
    node = next(item for item in card.nodes if item.node_id == "generate_report_sections")
    assert "audit_report_generate" in node.allowed_actions
```

- [ ] **Step 2: Run tests and verify RED**

Run: `backend\.venv\Scripts\python.exe -m pytest backend\tests\test_harness_v2.py backend\tests\test_audit_report_sop_v2.py -k "report_generate or report_generation_capability" -v`

Expected: FAIL because the descriptor, invoker branch, and SOP action are absent.

- [ ] **Step 3: Implement the scoped capability**

Add the descriptor to the existing audit capability list only for a bound case. In the invoker, resolve the bound case from the session and tenant exactly as `_audit_report_status` does, call the shared report service with the session model config, and do not honor model-provided tenant/case arguments. Return no section markdown, evidence text, storage paths, or credentials.

- [ ] **Step 4: Run focused Harness/SOP tests and full audit tests**

Run: `backend\.venv\Scripts\python.exe -m pytest backend\tests\test_harness_v2.py backend\tests\test_audit_report_sop_v2.py backend\tests\test_audit_report_runtime_traceability.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/core/harness_audit_capabilities.py backend/app/core/harness_capability_invoker.py backend/app/db/seed_fixtures/audit_report_generation_sop_v2.json backend/tests/test_harness_v2.py backend/tests/test_audit_report_sop_v2.py
git commit -m "feat: expose scoped audit report generation capability"
```

### Task 5: Add typed frontend process/report clients

**Files:**

- Modify: `frontend-enterprise/src/types/index.ts`.
- Modify: `frontend-enterprise/src/pages/audit-cases/auditCaseApi.ts`.
- Create: `frontend-enterprise/src/pages/audit-cases/auditReportApi.ts`.
- Create: `frontend-enterprise/src/pages/audit-cases/auditReportApi.test.ts`.

**Interfaces:**

- `processAuditCaseEvidence(caseId, modelConfigId?) -> Promise<AuditCaseProcessRead>` posts `{ model_config_id }` to `/process` with `TENANT_ID`.
- `listAuditCaseReports(caseId) -> Promise<AuditReportRead[]>` gets `/reports`.
- `createAuditCaseReport(caseId, modelConfigId?) -> Promise<AuditReportRead>` posts `{ model_config_id, publish: false }`.
- `publishAuditCaseReport(caseId, reportId) -> Promise<AuditReportRead>` posts `{}` to the version publish route.
- `downloadAuditCaseReport(caseId, reportId) -> Promise<Blob>` uses the authenticated `api.blob` client.

- [ ] **Step 1: Write failing client tests**

```typescript
it('posts evidence processing with the tenant and optional model config', async () => {
  await processAuditCaseEvidence('case/one', 'model-1');
  expect(fetchMock).toHaveBeenCalledWith(
    '/api/audit-cases/case%2Fone/process?tenant_id=tenant_demo',
    expect.objectContaining({ method: 'POST', body: JSON.stringify({ model_config_id: 'model-1' }) }),
  );
});

it('lists, creates, publishes, and downloads report versions with scoped paths', async () => {
  await listAuditCaseReports('case/one');
  await createAuditCaseReport('case/one');
  await publishAuditCaseReport('case/one', 'report/1');
  await downloadAuditCaseReport('case/one', 'report/1');
  expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
    '/api/audit-cases/case%2Fone/reports?tenant_id=tenant_demo',
    '/api/audit-cases/case%2Fone/reports?tenant_id=tenant_demo',
    '/api/audit-cases/case%2Fone/reports/report%2F1/publish?tenant_id=tenant_demo',
    '/api/audit-cases/case%2Fone/reports/report%2F1/download?tenant_id=tenant_demo',
  ]);
});
```

- [ ] **Step 2: Run focused client tests and verify RED**

Run: `npm --prefix frontend-enterprise run test -- src/pages/audit-cases/auditReportApi.test.ts`

Expected: FAIL because the new module and exports do not exist.

- [ ] **Step 3: Implement typed clients and additive DTOs**

Use `encodeURIComponent` for both case and report IDs, reuse `TENANT_ID`, and call `api.blob` for downloads. Keep report fields optional only where the backend is backward-compatible; do not coerce a missing rule snapshot into `complete`.

- [ ] **Step 4: Run client tests and the existing audit-case API tests**

Run: `npm --prefix frontend-enterprise run test -- src/pages/audit-cases/auditReportApi.test.ts src/pages/audit-cases/auditCaseApi.test.ts`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add frontend-enterprise/src/types/index.ts frontend-enterprise/src/pages/audit-cases/auditCaseApi.ts frontend-enterprise/src/pages/audit-cases/auditReportApi.ts frontend-enterprise/src/pages/audit-cases/auditReportApi.test.ts
git commit -m "feat: add audit evidence and report clients"
```

### Task 6: Build the evidence/report project panel

**Files:**

- Create: `frontend-enterprise/src/pages/audit-cases/components/EvidenceReportPanel.tsx`.
- Create: `frontend-enterprise/src/pages/audit-cases/components/EvidenceReportPanel.test.tsx`.
- Modify: `frontend-enterprise/src/pages/audit-cases/auditCaseErrors.ts` only for stable error labels.

**Interfaces:**

- `EvidenceReportPanel({ caseId, coverage, disabled?, onChanged? })` owns report loading, process/generate/publish/download busy state, and visible error state.
- Process is enabled only when not archived and `pending_chunk_ids.length > 0` with `file_coverage === 1` and `chunk_coverage === 1`.
- Draft generation is enabled only when not archived and `coverage.publish_allowed === true`.
- Confirmed publication is enabled only for a report with `rule_traceability_status === "complete"` and a successful draft status.
- Empty current bindings are rendered as “规则尚未配置”; failed calls retain loaded reports and coverage.

- [ ] **Step 1: Write failing component tests**

```tsx
it('offers evidence processing when chunks are pending and preserves state on failure', async () => {
  render(<EvidenceReportPanel caseId="case-1" coverage={coverageWithPendingEvidence} />);
  expect(screen.getByRole('button', { name: '处理证据' })).toBeEnabled();
  await user.click(screen.getByRole('button', { name: '处理证据' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('证据处理失败');
  expect(screen.getByText('规则尚未配置')).toBeTruthy();
});

it('allows a draft after coverage completes but blocks confirmation without a rule snapshot', async () => {
  render(<EvidenceReportPanel caseId="case-1" coverage={completeCoverage} />);
  await user.click(screen.getByRole('button', { name: '生成待确认草稿' }));
  expect(await screen.findByText(/规则尚未配置/)).toBeTruthy();
  expect(screen.getByRole('button', { name: '确认发布' })).toBeDisabled();
});

it('publishes and downloads a complete report', async () => {
  render(<EvidenceReportPanel caseId="case-1" coverage={completeCoverage} />);
  const publish = await screen.findByRole('button', { name: '确认发布' });
  expect(publish).toBeEnabled();
  await user.click(publish);
  await user.click(await screen.findByRole('button', { name: '下载报告' }));
  expect(window.URL.createObjectURL).toHaveBeenCalled();
});
```

- [ ] **Step 2: Run focused component tests and verify RED**

Run: `npm --prefix frontend-enterprise run test -- src/pages/audit-cases/components/EvidenceReportPanel.test.tsx`

Expected: FAIL because the panel module does not exist.

- [ ] **Step 3: Implement the minimal panel**

On mount, load report versions once; update the list only after successful process/create/publish calls. Render coverage counts and blocker labels through the existing `CoveragePanel` conventions. Convert a downloaded Blob to an object URL, click a hidden anchor with a fixed safe filename, and revoke the URL in `finally`. Use `auditCaseErrorMessage` for API errors and never clear prior reports after a failed request.

- [ ] **Step 4: Run focused and related frontend tests**

Run: `npm --prefix frontend-enterprise run test -- src/pages/audit-cases/components/EvidenceReportPanel.test.tsx src/pages/audit-cases/components/CoveragePanel.test.tsx src/pages/audit-cases/components/RuleBindingPanel.test.tsx`

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add frontend-enterprise/src/pages/audit-cases/components/EvidenceReportPanel.tsx frontend-enterprise/src/pages/audit-cases/components/EvidenceReportPanel.test.tsx frontend-enterprise/src/pages/audit-cases/auditCaseErrors.ts
git commit -m "feat: add audit evidence and report controls"
```

### Task 7: Integrate the panel into project details

**Files:**

- Modify: `frontend-enterprise/src/pages/audit-cases/AuditCaseDetailPage.tsx`.
- Modify: `frontend-enterprise/src/pages/audit-cases/AuditCaseDetailPage.test.tsx`.

**Interfaces:**

- Extend `DetailTab` with `'evidence-report'` and place the visible tab `证据与报告` after `审核材料` and before `项目文件库`.
- Render `<EvidenceReportPanel caseId={detail.project.id} coverage={detail.coverage} disabled={Boolean(archived)} onChanged={detail.reloadMaterials} />` only for the active tab.
- Preserve existing project/member/material/document/rule/event behavior, archive protection, and route guards.

- [ ] **Step 1: Write failing integration tests**

```tsx
it('renders the evidence and report panel from the project detail tab', async () => {
  render(<AuditCaseDetailPage currentUser={admin} />);
  await user.click(screen.getByRole('tab', { name: '证据与报告' }));
  expect(await screen.findByText('证据处理')).toBeTruthy();
});

it('keeps evidence/report actions disabled for archived projects', async () => {
  render(<AuditCaseDetailPage currentUser={admin} />);
  await user.click(screen.getByRole('tab', { name: '证据与报告' }));
  expect(await screen.findByRole('button', { name: '处理证据' })).toBeDisabled();
});
```

- [ ] **Step 2: Run the integration test and verify RED**

Run: `npm --prefix frontend-enterprise run test -- src/pages/audit-cases/AuditCaseDetailPage.test.tsx`

Expected: FAIL because the tab and panel branch do not exist.

- [ ] **Step 3: Add the tab branch without broad detail-page refactoring**

Add the tab value/label and panel import; pass the existing coverage and reload callback. Do not load reports in `useAuditCaseDetail`, so opening other tabs keeps its current network behavior.

- [ ] **Step 4: Run related frontend tests and the production build**

Run: `npm --prefix frontend-enterprise run test -- src/pages/audit-cases/AuditCaseDetailPage.test.tsx src/pages/audit-cases/components/EvidenceReportPanel.test.tsx src/pages/audit-cases/auditReportApi.test.ts`

Run: `npm --prefix frontend-enterprise run build` with the repository’s Node 20.19.5 runtime.

Expected: all focused tests pass and `tsc -b && vite build` succeeds.

- [ ] **Step 5: Commit**

```powershell
git add frontend-enterprise/src/pages/audit-cases/AuditCaseDetailPage.tsx frontend-enterprise/src/pages/audit-cases/AuditCaseDetailPage.test.tsx
git commit -m "feat: integrate evidence and report project tab"
```

### Task 8: Run end-to-end acceptance and repository verification

**Files:**

- Modify: `backend/tests/test_audit_pipeline_acceptance.py` only for additive rule-snapshot assertions.
- Modify: `docs/superpowers/plans/2026-09-05-audit-report-runtime-traceability-phase5a.md` to append the verification record.

- [ ] **Step 1: Add the fixed-sample runtime assertions**

Extend the existing 40k acceptance flow to assert that no-rule projects produce a draft with `rule_traceability_status == "not_configured"`, while a fixture with one published binding freezes its version ID and section rule IDs. Assert that a later binding migration leaves the first report snapshot unchanged.

- [ ] **Step 2: Run focused backend acceptance**

Run: `backend\.venv\Scripts\python.exe -m pytest backend\tests\test_audit_pipeline_acceptance.py backend\tests\test_audit_report_runtime_traceability.py backend\tests\test_audit_report_sop_v2.py -q`

Expected: PASS.

- [ ] **Step 3: Run backend style and full regression**

Run: `backend\.venv\Scripts\python.exe -m ruff check backend\app\audit_cases backend\app\api\audit_cases.py backend\app\core\harness_audit_capabilities.py backend\app\core\harness_capability_invoker.py backend\tests\test_audit_report_runtime_traceability.py`

Run: `backend\.venv\Scripts\python.exe -m pytest backend\tests -q`

Expected: changed paths pass Ruff; full backend results are recorded exactly, including any known Windows-only baseline failures.

- [ ] **Step 4: Run frontend checks**

Run: `npm --prefix frontend-enterprise run test`

Run: `npm --prefix frontend-enterprise run i18n:check`

Run: `npm --prefix frontend-enterprise run config:check`

Run: `npm --prefix frontend-enterprise run build`

Expected: full Vitest, i18n/config checks, and production build pass.

- [ ] **Step 5: Run Windows runtime verification**

Use only the documented PowerShell lifecycle:

```powershell
.\scripts\dev_up.ps1 --detach
.\scripts\dev_status.ps1
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:5173/api/health
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:5173/workspace/gallery
.\scripts\dev_down.ps1
```

Confirm the report tab loads for the authorized admin project, the empty rule state is visible for the current database, evidence processing remains blocked/available according to real coverage, and the service is stopped afterward.

- [ ] **Step 6: Append verification evidence and commit**

Append exact test/build/runtime counts to this plan’s `## Verification record` section, run `git diff --check`, confirm `git status --short --branch`, and commit:

```powershell
git add docs/superpowers/plans/2026-09-05-audit-report-runtime-traceability-phase5a.md backend/tests/test_audit_pipeline_acceptance.py
git commit -m "test: verify audit report runtime traceability"
```

## Verification record

- Plan created after root-cause investigation and approval of the Phase 5A design.
- Existing baseline before implementation: backend audit/rule focused tests 45 passed; frontend audit-case tests 47 passed; worktree clean at detached `3948add`.
