# Phase 2 Rule Library Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an administrator-facing rule-library page that manages rule sets, draft versions, rule definitions, validation, and publishing through the existing Phase 1 contracts.

**Architecture:** Extend the existing FastAPI rule domain with read/update endpoints that preserve immutable published versions. Add a React page under the existing Enterprise shell; the page edits only draft versions, displays published versions as read-only, and uses the existing `api` client, authentication, notifications, and UI primitives.

**Tech Stack:** FastAPI, Pydantic v2, SQLModel, React 18, TypeScript, React Router, Vitest, Testing Library.

**Spec:** `docs/superpowers/specs/2026-09-04-rule-library-project-data-onlyoffice-design.md`

## Global Constraints

- Only tenant administrators may create or change rule-library data.
- Published RuleSetVersion rows and their RuleDefinition rows are immutable; editing always creates or changes a draft.
- Do not place rule definitions into knowledge retrieval or change existing knowledge-library behavior.
- Reuse the existing tenant query convention, `get_current_user`, `api` client, `notify`, Enterprise sidebar, and design tokens.
- Every behavior-changing edit starts with a failing test and ends with focused tests plus the relevant frontend build.
- Do not implement project rule binding UI, ONLYOFFICE, OCR, or report generation in this slice.

### Task 1: Complete rule-library management API contracts

**Files:**

- Modify: `backend/app/api/rules.py`
- Modify: `backend/app/rules/service.py` only if a safe read/update seam is required
- Modify: `backend/tests/test_rule_api.py`

**Interfaces:**

- `GET /api/rule-sets?tenant_id=...` returns `list[RuleSetRead]` for the current tenant.
- `GET /api/rule-sets/{rule_set_id}/versions/{version_id}/rules?tenant_id=...` returns `list[RuleDefinitionRead]`.
- `PUT /api/rule-sets/{rule_set_id}/versions/{version_id}/rules?tenant_id=...` accepts `{ "rules": [...] }`, replaces only a draft version, and returns `RuleSetVersionRead`.
- `RuleDefinitionRead` exposes the stable key, labels, scope, execution level/method, condition, requirements, source references, sequence, and enabled state.
- Requests for another tenant, a missing rule set/version, or a published version return the existing stable error conventions and never mutate data.

- [ ] **Step 1: Write failing API tests**

  Add tests for listing a created rule set, reading its draft rules, replacing a draft rule list, rejecting replacement of a published version, and rejecting a cross-tenant version read.

- [ ] **Step 2: Run the focused tests and verify the expected RED failures**

  Run `backend/.venv/Scripts/python.exe -m pytest backend/tests/test_rule_api.py -q` and confirm failures are missing routes/response models, not fixture errors.

- [ ] **Step 3: Implement the minimal routes and read model**

  Reuse `RuleLibraryService.replace_rules`; add tenant-scoped lookup helpers and map `RuleVersionImmutableError`, `RuleAccessDenied`, and not-found cases to the established HTTP errors.

- [ ] **Step 4: Run focused API and Phase 1 regression tests**

  Run `backend/.venv/Scripts/python.exe -m pytest backend/tests/test_rule_api.py backend/tests/test_rule_service.py backend/tests/test_rule_binding_service.py -q`.

- [ ] **Step 5: Commit**

  `git add backend/app/api/rules.py backend/app/rules/service.py backend/tests/test_rule_api.py && git commit -m "feat: expose rule library management api"`

### Task 2: Build the administrator rule-library page

**Files:**

- Create: `frontend-enterprise/src/pages/rules/RuleLibraryPage.tsx`
- Create: `frontend-enterprise/src/pages/rules/ruleLibraryApi.ts`
- Create: `frontend-enterprise/src/pages/rules/ruleLibraryApi.test.ts`
- Create: `frontend-enterprise/src/pages/rules/RuleLibraryPage.test.tsx`
- Modify: `frontend-enterprise/src/enums/routes.ts`
- Modify: `frontend-enterprise/src/components/AppSidebar.tsx`
- Modify: `frontend-enterprise/src/App.tsx`

**Interfaces:**

- The page is available at `/enterprise/rules` and is admin-only.
- The API client exports typed `listRuleSets`, `createRuleSet`, `listRuleSetVersions`, `listRuleDefinitions`, `createDraftVersion`, `replaceDraftRules`, `validateRuleVersion`, and `publishRuleVersion` functions.
- The UI has a rule-set list, new rule-set dialog, version selector, draft rule editor, add/remove rule controls, validate button, publish button, and read-only published-version view.
- A failed request leaves the previous server state visible and shows an actionable notification; it never clears a list to pretend that a request succeeded.

- [ ] **Step 1: Write failing API-client and page tests**

  Test URL/query/body mapping for every client function; test that the page loads rule sets, opens a selected draft, adds a rule, saves it, validates, and publishes; test that published versions show read-only controls and non-admins are redirected by the route guard.

- [ ] **Step 2: Run the focused Vitest tests and verify RED**

  Run `npm --prefix frontend-enterprise run test -- src/pages/rules/ruleLibraryApi.test.ts src/pages/rules/RuleLibraryPage.test.tsx` and confirm failures are missing modules/route behavior.

- [ ] **Step 3: Implement the typed client and page**

  Follow existing page patterns (`AuditCasesPage`, `KnowledgePage`, `AppHeader`, `UIButton`, `Dialog`, `Select`, `Textarea`, `notify`) and keep rule JSON editing structured through explicit fields rather than an unvalidated raw JSON textarea.

- [ ] **Step 4: Add the admin sidebar item and route**

  Add `EnterpriseRoute.Rules`, show “规则库” in `SYSTEM_NAV`, mark `/enterprise/rules` selected, and render the page only when `isAdmin` is true.

- [ ] **Step 5: Run focused tests, full frontend tests, and build**

  Run `npm --prefix frontend-enterprise run test -- src/pages/rules/ruleLibraryApi.test.ts src/pages/rules/RuleLibraryPage.test.tsx`, then `npm --prefix frontend-enterprise run test`, then `npm --prefix frontend-enterprise run build` using the prepared Node.js 20.19.5 runtime.

- [ ] **Step 6: Commit**

  `git add frontend-enterprise/src/pages/rules frontend-enterprise/src/enums/routes.ts frontend-enterprise/src/components/AppSidebar.tsx frontend-enterprise/src/App.tsx && git commit -m "feat: add rule library management page"`

## Acceptance Checklist

- [ ] An administrator can create a rule set from the browser.
- [ ] An administrator can create and edit a draft version with structured rule fields.
- [ ] Validation errors are shown without publishing.
- [ ] Publishing a version makes it read-only in the page and backend.
- [ ] Cross-tenant reads and non-admin writes are rejected.
- [ ] Existing Phase 1 rule and project-data tests remain green.
- [ ] The frontend build succeeds with the documented Node.js 20.19.5 runtime.
