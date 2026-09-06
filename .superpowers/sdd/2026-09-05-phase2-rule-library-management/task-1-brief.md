# Task 1: Complete rule-library management API contracts

Work in the Phase 2 isolated worktree. Read this brief first; it is the exact task requirement.

## Files

- Modify `backend/app/api/rules.py`.
- Modify `backend/app/rules/service.py` only if a safe read/update seam is required.
- Modify `backend/tests/test_rule_api.py`.

## Required public interfaces

- `GET /api/rule-sets?tenant_id=...` returns `list[RuleSetRead]` for the current tenant.
- `GET /api/rule-sets/{rule_set_id}/versions/{version_id}/rules?tenant_id=...` returns `list[RuleDefinitionRead]`.
- `PUT /api/rule-sets/{rule_set_id}/versions/{version_id}/rules?tenant_id=...` accepts `{ "rules": [...] }`, replaces only a draft version, and returns `RuleSetVersionRead`.
- `RuleDefinitionRead` exposes stable key, name, description, workflow nodes, information domains, document types, field keys, execution level/method, condition, input requirements, evidence requirements, source references, sequence, and enabled state.

## Required behavior

- Only tenant administrators can use the rule-library management endpoints.
- Tenant isolation and the existing stable error conventions must be preserved.
- Missing rule set/version returns the existing not-found errors.
- Replacing a published version returns the existing immutable-version conflict and does not mutate any rule or version.
- Read endpoints must not expose another tenant's rows.
- Reuse `RuleLibraryService.replace_rules` where possible; do not introduce a second persistence path.
- Existing Phase 1 endpoints and tests must remain compatible.

## TDD and verification

1. Add focused tests for list, draft rule read, draft replacement, published replacement rejection, and cross-tenant read rejection.
2. Run the focused tests and observe the expected RED failures caused by missing routes/models.
3. Implement the minimal routes/read models/error mapping.
4. Run `backend/.venv/Scripts/python.exe -m pytest backend/tests/test_rule_api.py backend/tests/test_rule_service.py backend/tests/test_rule_binding_service.py -q`.
5. Run a changed-file Ruff check if available.

## Constraints

- Do not modify frontend files.
- Do not modify published versions or published rule definitions.
- Do not add unrelated refactors.
- Do not dispatch subagents.

Write a full report to `.superpowers/sdd/2026-09-05-phase2-rule-library-management/task-1-report.md` with changed files, RED/GREEN evidence, test results, and concerns. Commit the implementation with a focused commit message and return only the commit hash, one-line test summary, and concerns.
