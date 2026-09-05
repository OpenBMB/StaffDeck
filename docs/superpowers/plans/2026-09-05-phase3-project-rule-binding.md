# Project Rule Binding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an administrator-facing rule-binding tab to certification project details so a project can explicitly select, lock, inspect, and deliberately migrate to published rule versions.

**Architecture:** Reuse the existing Phase 1 rule-binding API and Phase 2 rule-library read APIs. Add a small typed client for project binding actions, a focused `RuleBindingPanel` that treats “not initialized” as an empty project state, and a fifth tab in `AuditCaseDetailPage`. Initial binding is a direct explicit save; changing an existing binding requires a migration preview and a non-empty reason before the backend migration endpoint is called.

**Tech Stack:** React 18, TypeScript, React Router, Vitest, Testing Library, existing FastAPI endpoints, existing `api` client and Enterprise UI primitives.

**Spec:** `docs/superpowers/specs/2026-09-04-rule-library-project-data-onlyoffice-design.md`

## Global Constraints

- A project binds published rule versions only; draft versions must never be selectable or submitted.
- A project does not automatically follow a newer rule-library version after it is bound.
- Changing an existing binding requires an explicit migration preview, a non-empty reason, and the existing `/migrate` endpoint.
- Preserve the last visible project binding state when any request fails; show an actionable notification instead of clearing the panel.
- Reuse tenant-scoped API query conventions, existing authentication, `notify`, `AppHeader`, `UIButton`, `Card`, `Dialog`, `Select`, and Tailwind conventions.
- Do not modify knowledge retrieval, material processing, OCR, ONLYOFFICE, report generation, or the rule-library editor in this slice.
- Every behavior-changing edit starts with a failing test and ends with focused tests plus the relevant frontend build.

---

### Task 1: Add the project rule-binding API client

**Files:**

- Create: `frontend-enterprise/src/pages/audit-cases/ruleBindingApi.ts`
- Create: `frontend-enterprise/src/pages/audit-cases/ruleBindingApi.test.ts`

**Interfaces:**

- `RuleBindingRead` mirrors the backend response fields: `id`, `tenant_id`, `audit_case_id`, `rule_set_id`, `rule_set_version_id`, `selection_source`, `status`, `priority`, `bound_by_user_id`, and optional `supersedes_binding_id`.
- `RuleMigrationPreviewRead` contains `added_rule_keys`, `removed_rule_keys`, `changed_rule_keys`, `unchanged_rule_keys`, `impacted_information_domains`, and `impacted_workflow_nodes`.
- Export `loadCurrentRuleBindings(caseId)`, `loadPublishedRuleVersionOptions()`, `replaceCurrentRuleBindings(caseId, versionIds, selectionSource)`, `previewRuleBindingMigration(caseId, versionIds)`, and `migrateRuleBindings(caseId, versionIds, reason)`.
- `loadPublishedRuleVersionOptions()` calls `listRuleSets()` and `listRuleSetVersions()` from the Phase 2 rule-library client, filters `status === "published"`, and returns options carrying the rule-set name/key and version record.

- [x] **Step 1: Write the failing API-client tests**

  Assert exact URL/query/body mappings for the five exported functions. Assert that published-version options discard draft versions and retain the owning rule-set name/key.

- [x] **Step 2: Run the focused tests and verify RED**

  Run `npm --prefix frontend-enterprise run test -- src/pages/audit-cases/ruleBindingApi.test.ts`. The expected failure is the missing `ruleBindingApi` module or missing exports, not a fixture or environment error.

- [x] **Step 3: Implement the typed client**

  Use `api.get`, `api.put`, and `api.post` with `TENANT_ID`; encode `caseId`; submit `{ version_ids, selection_source }` to `/api/audit-cases/{caseId}/rule-bindings`, `{ version_ids }` to `/migration-preview`, and `{ version_ids, reason }` to `/migrate`.

- [x] **Step 4: Run the focused API-client tests**

  Run the same Vitest command and confirm all URL, query, body, and published-filter assertions pass.

- [x] **Step 5: Commit**

  `git add frontend-enterprise/src/pages/audit-cases/ruleBindingApi.ts frontend-enterprise/src/pages/audit-cases/ruleBindingApi.test.ts && git commit -m "feat: add project rule binding api client"`

---

### Task 2: Build the rule-binding panel

**Files:**

- Create: `frontend-enterprise/src/pages/audit-cases/components/RuleBindingPanel.tsx`
- Create: `frontend-enterprise/src/pages/audit-cases/components/RuleBindingPanel.test.tsx`

**Interfaces:**

- `RuleBindingPanel` accepts `{ caseId: string; disabled?: boolean }` and owns candidate loading, current-binding loading, selection, preview, migration reason, save, and notification state.
- Initial `RULE_BINDING_NOT_INITIALIZED` is represented as an empty binding list and an explicit “尚未绑定规则版本” state; other errors preserve the current state and notify the user.
- Candidate cards show only published versions, the owning rule-set name/key, version number, and a selected checkbox. Current bindings show their pinned version and source; a newer published version is informational until the user selects it.
- When no current binding exists, “绑定选中版本” calls `replaceCurrentRuleBindings` and then displays the returned current bindings.
- When current bindings exist and the selection differs, “预览迁移影响” calls `previewRuleBindingMigration`; a preview with added/removed/changed rules and impacted domains/nodes is shown. “确认迁移” is disabled until a non-empty reason is entered and calls `migrateRuleBindings`.
- Failed loads/saves/previews/migrations leave the visible candidates, current bindings, selection, and preview intact.

- [x] **Step 1: Write the failing component tests**

  Cover initial unbound state, published-only candidate display, explicit initial binding, migration preview and reason-gated confirmation, read-only current-version display, and failed migration state preservation with notification.

- [x] **Step 2: Run the focused component tests and verify RED**

  Run `npm --prefix frontend-enterprise run test -- src/pages/audit-cases/components/RuleBindingPanel.test.tsx`. Confirm the failure is the missing panel module.

- [x] **Step 3: Implement the minimal panel**

  Follow existing `MaterialManager` and `KnowledgeVersionSelector` patterns. Keep state updates transactional: update current bindings only after a successful API response; do not clear data while a request is in flight or after an error. Use stable labels for Testing Library and accessible checkbox/button controls.

- [x] **Step 4: Run the focused panel tests**

  Run the same Vitest command and confirm all binding, preview, migration, read-only, and failure-preservation assertions pass.

- [x] **Step 5: Commit**

  `git add frontend-enterprise/src/pages/audit-cases/components/RuleBindingPanel.tsx frontend-enterprise/src/pages/audit-cases/components/RuleBindingPanel.test.tsx && git commit -m "feat: add project rule binding panel"`

---

### Task 3: Integrate the panel into project details

**Files:**

- Modify: `frontend-enterprise/src/pages/audit-cases/AuditCaseDetailPage.tsx`
- Modify: `frontend-enterprise/src/pages/audit-cases/AuditCaseDetailPage.test.tsx`

**Interfaces:**

- Extend `DetailTab` with `'rules'` and add the visible tab label `规则绑定` after `审核材料` and before `操作记录`.
- Render `<RuleBindingPanel caseId={detail.project.id} disabled={Boolean(archived)} />` only when the new tab is active.
- Preserve the existing project/member/material/event behavior, loading states, archive protection, route guard, and refresh behavior.

- [x] **Step 1: Write the failing integration tests**

  Assert the new tab is present, clicking it renders the panel, and archived projects disable binding actions while existing member/material behavior remains covered.

- [x] **Step 2: Run the focused integration tests and verify RED**

  Run `npm --prefix frontend-enterprise run test -- src/pages/audit-cases/AuditCaseDetailPage.test.tsx`. Confirm the failure is the missing tab/panel integration.

- [x] **Step 3: Implement the tab integration**

  Add the tab value and panel branch without changing the existing detail hook contract or loading all rule data on every page load.

- [x] **Step 4: Run focused and related frontend tests**

  Run `npm --prefix frontend-enterprise run test -- src/pages/audit-cases/ruleBindingApi.test.ts src/pages/audit-cases/components/RuleBindingPanel.test.tsx src/pages/audit-cases/AuditCaseDetailPage.test.tsx`, then run `npm --prefix frontend-enterprise run build` with Node.js 20.19.5.

- [x] **Step 5: Commit**

  `git add frontend-enterprise/src/pages/audit-cases/AuditCaseDetailPage.tsx frontend-enterprise/src/pages/audit-cases/AuditCaseDetailPage.test.tsx && git commit -m "feat: integrate project rule binding tab"`

## Acceptance Checklist

- [x] An administrator can open `认证项目 → 规则绑定` and see only published rule versions.
- [x] An unbound project can explicitly bind one or more selected published versions.
- [x] The page displays the exact pinned current version and does not silently replace it with the latest version.
- [x] A changed binding requires a migration preview and a non-empty reason.
- [x] Migration preview displays changed rule keys and impacted domains/workflow nodes before confirmation.
- [x] Failed requests preserve the visible panel state and show an actionable notification; ordinary load failures lock editing.
- [x] Archived projects cannot bind or migrate rule versions.
- [x] Existing audit-case and full frontend tests pass in this worktree.
- [x] Frontend build succeeds with Node.js 20.19.5.

## Verification record

- Task 1 focused API tests: 5/5 passed; independent review was unavailable after two interrupted attempts, so the contract was additionally checked locally against the backend routes.
- Task 2 initial order regression: RED at 1 failed/6 passed; order fix GREEN at 9/9. Load-failure regression: RED at 4 failed/9 passed; load-lock fix GREEN at 11/11. Final independent review: PASS.
- Task 3 related tests: 3 files/19 tests passed under Node.js 20.19.5. Independent integration review: PASS.
- Full frontend regression: 60 files/271 tests passed. Production build (`tsc -b && vite build`) passed under Node.js 20.19.5. Vite emitted only the existing large-chunk warning.
