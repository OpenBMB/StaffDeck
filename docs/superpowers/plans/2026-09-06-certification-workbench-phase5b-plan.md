# Certification Workbench Phase 5B Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Open certification processes 17, 21, 22, and 23 in the unified workbench with version-pinned predecessor gates and document-check visibility while preserving existing customizations.

**Architecture:** Add a single backend process-definition registry that owns names, enablement, predecessor relations, reference requirements, and the process-21 check requirement. Expose the registry plus a document-scoped gate evaluation endpoint, enforce the same gate in work-item creation and submit/approve transitions, and render the gate result in the existing review panel. Reuse `AuditWorkItem`, immutable document versions, `AuditDocumentCheck`, issues, and events; add no database tables or formal certification-decision behavior.

**Tech Stack:** FastAPI, SQLModel/SQLite, pytest, React 18, TypeScript, Vitest, Testing Library.

**Spec:** `docs/superpowers/specs/2026-09-06-certification-workbench-phase5b-design.md`

## Global Constraints

- Only processes 17, 21, 22, and 23 become enabled; processes 28–36 and every other non-pilot process remain directory-only.
- `AuditCaseDocument` is the primary scope; every document and predecessor reference is tenant- and audit-case-scoped and pinned to an active version at submission.
- Process 21 approval requires a current completed document check with no finding whose severity is `error`; warnings remain human-review work.
- Process 19 is optional for process 23 and must not be made a hard predecessor.
- Do not add formal certification decisions, certificate signing, regulator reporting, Feishu integration, or new persistence tables.
- Existing role, tenant isolation, idempotency, revision, stale-version, issue, and audit-event behavior must remain intact.

### Task 1: Add the process-definition registry and gate evaluator

**Files:**
- Create: `backend/app/audit_cases/workbench_processes.py`
- Modify: `backend/app/audit_cases/workbench.py`
- Create: `backend/tests/test_audit_workbench_processes.py`
- Modify: `backend/tests/test_audit_workbench.py`

**Interfaces:**
- Produces `ProcessDefinition` (frozen dataclass) with `number`, `name`, `stage`, `enabled`, `predecessor_numbers`, `required_reference_process_numbers`, `requires_fresh_check`, and `guidance`.
- Produces `ProcessGate` (typed mapping) with `process_number`, `ready`, `blockers`, `predecessors`, `required_reference_document_ids`, and `check`.
- Produces `get_process_definition(number: int) -> ProcessDefinition`, `all_process_definitions() -> tuple[ProcessDefinition, ...]`, and `evaluate_process_gate(db: Session, case: AuditCase, process_number: int, document_id: str | None = None) -> ProcessGate`.
- `AuditWorkbenchService.create_item` and `transition_item` consume `evaluate_process_gate`; `snapshot` consumes the registry for its 36 process directory.

- [x] **Step 1: Write failing unit tests for registry metadata and gate semantics**

  Add tests that assert:

  ```python
  assert get_process_definition(17).predecessor_numbers == (16,)
  assert get_process_definition(21).requires_fresh_check is True
  assert get_process_definition(23).predecessor_numbers == (18,)
  assert get_process_definition(23).required_reference_process_numbers == (18,)
  assert 17 in {row.number for row in all_process_definitions() if row.enabled}
  assert 28 not in {row.number for row in all_process_definitions() if row.enabled}
  ```

  Build a small SQLite case with approved process-16 and process-18 work items and assert process 17/23 are ready, process 21 is blocked until process 17 is approved, and process 23 has no process-19 blocker. Assert a dependent gate becomes blocked after its predecessor is reopened.

- [x] **Step 2: Run the focused tests and verify they fail**

  Run from `backend`:

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_audit_workbench_processes.py -q
  ```

  Expected: FAIL because the process registry and gate evaluator do not exist.

- [x] **Step 3: Implement the registry and evaluator**

  Move the existing 36 names into `workbench_processes.py`, preserve the spreadsheet order, and define the enabled set as `{15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26}`. Define process 17/21/22/23 metadata exactly as the spec table. The evaluator must query only the current tenant/case, find approved predecessor work items, derive required reference document IDs excluding the selected primary document, and report blockers without mutating the database. For process 21, inspect the newest `AuditDocumentCheck` for the selected document and report `CHECK_REQUIRED`, `CHECK_NOT_COMPLETED`, `CHECK_STALE`, or `CHECK_ERRORS` as applicable.

  Replace `PILOT_PROCESSES`/`PROCESS_NAMES` use in `workbench.py` with registry helpers while keeping compatibility aliases if existing imports require them. Enforce `PROCESS_NOT_ENABLED` for directory-only entries.

- [x] **Step 4: Run unit tests and existing backend workbench tests**

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_audit_workbench_processes.py tests/test_audit_workbench.py -q
  ```

  Expected: PASS with no regression in the existing 15/16/18/19/20/24/25/26 lifecycle tests.

- [x] **Step 5: Commit the registry and evaluator**

  ```powershell
  git add backend/app/audit_cases/workbench_processes.py backend/app/audit_cases/workbench.py backend/tests/test_audit_workbench_processes.py backend/tests/test_audit_workbench.py
  git commit -m "feat: define certification workbench process gates"
  ```

### Task 2: Enforce gates in the API and expose document-scoped status

**Files:**
- Modify: `backend/app/audit_cases/workbench.py`
- Modify: `backend/app/api/audit_workbench.py`
- Modify: `backend/tests/test_audit_workbench.py`

**Interfaces:**
- Adds `GET /api/audit-workbench/cases/{case_id}/process-gates?tenant_id=<tenant>&document_id=<document>` returning `list[ProcessGate]` for all 36 processes.
- `AuditWorkbenchService.create_item` raises `HTTPException(409, "PROCESS_PRECONDITION_REQUIRED")` or `HTTPException(409, "PROCESS_REFERENCE_REQUIRED")` when the selected enabled process is not ready or required predecessor documents are not referenced.
- `AuditWorkbenchService.transition_item` re-evaluates the item’s process gate before `submit` and `approve`; process-21 approval additionally blocks until its fresh check is complete and error-free.

- [x] **Step 1: Write failing API tests for creation, transition, and gate endpoint**

  Extend the existing `ctx` fixture helpers so an approved predecessor can be created without bypassing role and revision checks. Assert creating process 17 before process 16 approval returns 409 with `PROCESS_PRECONDITION_REQUIRED`; after process 16 approval, omit its required reference and assert `PROCESS_REFERENCE_REQUIRED` when the predecessor document differs; include the reference and assert success. Assert a process-21 item cannot approve without a completed fresh check and can approve after inserting a no-error completed check. Assert process 23 creation succeeds with approved 18 and no 19 item. Assert the new endpoint returns enabled flags and blocker codes for the selected document.

- [x] **Step 2: Run the focused API tests and verify they fail**

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/test_audit_workbench.py -k "process_gate or process_17 or process_21 or process_23" -q
  ```

  Expected: FAIL because the endpoint and enforcement are not implemented.

- [x] **Step 3: Implement route and enforcement**

  Add a `document_id` query parameter and call `evaluate_process_gate` for every process definition after `service.case(...)` authorization. In `create_item`, evaluate the selected process before creating the row, and require every returned `required_reference_document_id` in `request.reference_document_ids`. In `transition_item`, evaluate before submit/approve; return stable 409 codes and keep the existing stale-version and issue checks. Do not alter idempotency replay behavior or create new database columns.

- [x] **Step 4: Run backend tests and formatting**

  ```powershell
  .\.venv\Scripts\python.exe -m pytest -q
  .\.venv\Scripts\python.exe -m ruff check app tests
  ```

  Expected: PASS; ruff reports no new errors.

- [x] **Step 5: Commit the API gate enforcement**

  ```powershell
  git add backend/app/audit_cases/workbench.py backend/app/api/audit_workbench.py backend/tests/test_audit_workbench.py
  git commit -m "feat: enforce certification process prerequisites"
  ```

### Task 3: Render process-specific gate cards in the workbench

**Files:**
- Modify: `frontend-enterprise/src/pages/audit-cases/workbenchApi.ts`
- Modify: `frontend-enterprise/src/pages/audit-cases/AuditWorkbenchPage.tsx`
- Modify: `frontend-enterprise/src/pages/audit-cases/components/WorkbenchReview.tsx`
- Modify: `frontend-enterprise/src/pages/audit-cases/workbench.css`
- Modify: `frontend-enterprise/src/pages/audit-cases/AuditWorkbenchPage.test.tsx`
- Create: `frontend-enterprise/src/pages/audit-cases/components/WorkbenchReview.test.tsx`

**Interfaces:**
- Adds `ProcessGate`, `ProcessGateBlocker`, and `ProcessGatePredecessor` TypeScript types matching the API response.
- Adds `loadProcessGates(caseId: string, documentId: string): Promise<ProcessGate[]>`.
- `AuditWorkbenchPage` loads gates after documents/snapshot and whenever the selected document changes; `WorkbenchReview` receives `gate?: ProcessGate`.

- [x] **Step 1: Write failing component tests**

  Add a review-panel test that renders process 17 with a blocked gate and asserts the card shows “流程 16” plus the blocker and disables “建立工作项”. Add a ready process 23 case and assert the card states process 18 is complete and does not mention process 19 as required. Extend the page mock to return `/process-gates` and assert changing the process refreshes the gate request.

- [x] **Step 2: Run focused frontend tests and verify they fail**

  ```powershell
  npm --prefix frontend-enterprise test -- --run src/pages/audit-cases/components/WorkbenchReview.test.tsx src/pages/audit-cases/AuditWorkbenchPage.test.tsx
  ```

  Expected: FAIL because the new gate types, fetch, prop, and card do not exist.

- [x] **Step 3: Implement API types, loading, and gate card**

  Add the API types and loader. Keep gate loading failures visible through the existing workbench error region. Render the card only for enabled process definitions, listing the process objective, predecessor status, required document references, and check result. Keep directory-only copy unchanged. Disable creation and submit/approve actions when `gate.ready` is false, while retaining server-side error handling for races. Do not change the existing dirty-navigation guard.

- [x] **Step 4: Run focused tests, TypeScript build, and full frontend tests**

  ```powershell
  npm --prefix frontend-enterprise test -- --run src/pages/audit-cases/components/WorkbenchReview.test.tsx src/pages/audit-cases/AuditWorkbenchPage.test.tsx
  npm --prefix frontend-enterprise run build
  npm --prefix frontend-enterprise test -- --run
  ```

  Expected: PASS; build may retain the existing chunk-size warning only.

- [x] **Step 5: Commit the workbench UI**

  ```powershell
  git add frontend-enterprise/src/pages/audit-cases/workbenchApi.ts frontend-enterprise/src/pages/audit-cases/AuditWorkbenchPage.tsx frontend-enterprise/src/pages/audit-cases/components/WorkbenchReview.tsx frontend-enterprise/src/pages/audit-cases/workbench.css frontend-enterprise/src/pages/audit-cases/AuditWorkbenchPage.test.tsx frontend-enterprise/src/pages/audit-cases/components/WorkbenchReview.test.tsx
  git commit -m "feat: show process gate status in certification workbench"
  ```

### Task 4: Verify integration and update the Phase 5B handoff

**Files:**
- Modify: `C:/Users/Administrator/Documents/Codex/2026-09-05/staffdeck-continuation/work/phase5b-contract.md` (outside repository; append/update only)

- [x] **Step 1: Run the complete verification set**

  ```powershell
  .\.venv\Scripts\python.exe -m pytest -q
  .\.venv\Scripts\python.exe -m ruff check app tests
  npm --prefix frontend-enterprise run i18n:check
  npm --prefix frontend-enterprise test -- --run
  npm --prefix frontend-enterprise run build
  git diff --check
  ```

- [x] **Step 2: Smoke-test the running workbench**

  Refresh the existing local workbench tab, select processes 17, 21, 22, and 23, confirm the gate card and disabled create action reflect the API response, and confirm processes 28–36 still say “后续阶段”. Check browser console for new errors or warnings.

  本地样例项目当前没有工作文档，因此文档级流程门槛卡片无法在真实页面中展开；流程门槛状态、按钮禁用和流程切换由 `WorkbenchReview.test.tsx` 覆盖。本次浏览器冒烟已验证 36 个流程目录、流程 17 选择、阶段 3 的 28–36 “后续阶段”标记，以及无控制台错误/警告。

- [x] **Step 3: Update the handoff artifact**

  Append the Phase 5B design, commits, test outputs, and any known external PR/CodeQL state to `phase5b-contract.md`; do not rewrite unrelated handoff entries.

- [x] **Step 4: Commit only repository changes and report evidence**

  ```powershell
  git status --short
  git log --oneline -4
  ```

  Report the exact commit SHAs, test/build results, and any remaining GitHub review/permission blocker without claiming the PR is mergeable unless the live status confirms it.

