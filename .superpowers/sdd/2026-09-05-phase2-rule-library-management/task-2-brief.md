# Task 2: Build the administrator rule-library page

Work in the Phase 2 isolated worktree. Read this brief first; it is the exact task requirement.

## Files

- Create `frontend-enterprise/src/pages/rules/RuleLibraryPage.tsx`.
- Create `frontend-enterprise/src/pages/rules/ruleLibraryApi.ts`.
- Create `frontend-enterprise/src/pages/rules/ruleLibraryApi.test.ts`.
- Create `frontend-enterprise/src/pages/rules/RuleLibraryPage.test.tsx`.
- Modify `frontend-enterprise/src/enums/routes.ts`.
- Modify `frontend-enterprise/src/components/AppSidebar.tsx`.
- Modify `frontend-enterprise/src/App.tsx`.

## Existing backend contracts

Use the Phase 1/Task 1 endpoints and the existing `api` client with `TENANT_ID`:

- `GET /api/rule-sets?tenant_id=...`
- `POST /api/rule-sets`
- `GET /api/rule-sets/{rule_set_id}/versions?tenant_id=...`
- `POST /api/rule-sets/{rule_set_id}/versions?tenant_id=...` with `{rules: [...]}`
- `GET /api/rule-sets/{rule_set_id}/versions/{version_id}/rules?tenant_id=...`
- `PUT /api/rule-sets/{rule_set_id}/versions/{version_id}/rules?tenant_id=...` with `{rules: [...]}`
- `POST /api/rule-sets/{rule_set_id}/versions/{version_id}/validate?tenant_id=...`
- `POST /api/rule-sets/{rule_set_id}/versions/{version_id}/publish?tenant_id=...`

## Required behavior

- The page is available at `/enterprise/rules` and is admin-only.
- The page contains a rule-set list, a new rule-set dialog, a version selector, a structured draft rule editor, add/remove rule controls, validate, publish, and a read-only published-version view.
- Structured fields must include at least rule key, name, description, workflow nodes, information domains, document types, field keys, execution level, execution method, condition operator/field/value, evidence requirements, and enabled state.
- A draft can be saved through the API, validated, and published. Published versions cannot show editable controls.
- Failed requests preserve the last visible state and show an actionable notification; do not clear lists on failure.
- Follow existing `AppHeader`, `UIButton`, `Dialog`, `Select`, `Textarea`, `notify`, and Tailwind conventions. Do not add dependencies.
- Non-admin navigation must redirect to the existing gallery route, consistent with the existing admin-only pages.

## TDD and verification

1. Write API-client tests for URL/query/body mapping for every exported client function.
2. Write page tests for loading, creating/selecting a rule set, editing a draft, save, validate, publish, read-only published state, and non-admin route guard.
3. Run the focused tests and observe RED because the modules/routes do not yet exist.
4. Implement the minimal client/page/route/sidebar changes.
5. Run focused tests, then `npm --prefix frontend-enterprise run test`, then `npm --prefix frontend-enterprise run build` with Node.js 20.19.5.

## Constraints

- Do not change backend files.
- Do not implement project rule binding UI, ONLYOFFICE, OCR, or report generation.
- Do not use raw unvalidated JSON as the primary editor.
- Do not silently invent rule-set data when the API fails.

Write a full report to `.superpowers/sdd/2026-09-05-phase2-rule-library-management/task-2-report.md` with changed files, RED/GREEN evidence, test results, and concerns. Commit only Task 2 changes and return the commit hash, one-line test summary, and concerns.
