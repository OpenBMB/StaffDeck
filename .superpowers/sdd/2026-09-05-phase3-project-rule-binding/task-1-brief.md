# Task 1: project rule-binding API client

Work only in the Phase 3 worktree. Read the Phase 3 plan and ledger first.

## Files

- Create `frontend-enterprise/src/pages/audit-cases/ruleBindingApi.ts`.
- Create `frontend-enterprise/src/pages/audit-cases/ruleBindingApi.test.ts`.

## Required interfaces

Export these types:

- `RuleBindingRead` with `id`, `tenant_id`, `audit_case_id`, `rule_set_id`, `rule_set_version_id`, `selection_source`, `status`, `priority`, `bound_by_user_id`, and nullable `supersedes_binding_id`.
- `RuleMigrationPreviewRead` with arrays `added_rule_keys`, `removed_rule_keys`, `changed_rule_keys`, `unchanged_rule_keys`, `impacted_information_domains`, and `impacted_workflow_nodes`.
- `PublishedRuleVersionOption` containing the Phase 2 `RuleSetRead` identity fields needed for display plus a `RuleSetVersionRead` version record.

Export these functions using the existing `api` client and `TENANT_ID`:

- `loadCurrentRuleBindings(caseId)` → `Promise<RuleBindingRead[]>`, GET `/api/audit-cases/{encodedCaseId}/rule-bindings?tenant_id=tenant_demo`.
- `loadPublishedRuleVersionOptions()` → `Promise<PublishedRuleVersionOption[]>`; call Phase 2 `listRuleSets()` and `listRuleSetVersions()` and filter to `status === 'published'`.
- `replaceCurrentRuleBindings(caseId, versionIds, selectionSource = 'manual')` → `Promise<RuleBindingRead[]>`, PUT body `{ version_ids, selection_source }`.
- `previewRuleBindingMigration(caseId, versionIds)` → `Promise<RuleMigrationPreviewRead>`, POST body `{ version_ids }`.
- `migrateRuleBindings(caseId, versionIds, reason)` → `Promise<RuleBindingRead[]>`, POST body `{ version_ids, reason }`.

## TDD and verification

1. Write tests first for exact URL/query/body mappings for all five functions and published-only option filtering with rule-set display identity.
2. Run `npm --prefix frontend-enterprise run test -- src/pages/audit-cases/ruleBindingApi.test.ts` from the Phase 3 worktree and confirm a missing-module/export RED failure.
3. Implement the minimal typed client; do not modify backend or unrelated pages.
4. Run the same focused test command with Node.js 20.19.5 and confirm it passes.
5. Commit only the two Task 1 files as `feat: add project rule binding api client` and report the hash and test result.
