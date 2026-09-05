# Task 2: rule-binding panel

Work only in the Phase 3 worktree. Read the Phase 3 plan, ledger, and Task 1 client after Task 1 is complete.

## Files

- Create `frontend-enterprise/src/pages/audit-cases/components/RuleBindingPanel.tsx`.
- Create `frontend-enterprise/src/pages/audit-cases/components/RuleBindingPanel.test.tsx`.

## Required behavior

- Props are `{ caseId: string; disabled?: boolean }`.
- Load published rule-version candidates and current bindings. A `ApiError` with code `RULE_BINDING_NOT_INITIALIZED` means an empty unbound state; any other failure keeps the current visible state and shows an actionable notification.
- Candidate cards show only published versions with rule-set name/key and version number. Do not make a draft selectable.
- Current bindings show the exact pinned rule-set version and selection source. Do not silently select or replace a newer published version.
- When unbound, selecting published versions and pressing `绑定选中版本` calls `replaceCurrentRuleBindings` and applies the response only after success.
- When bound, selecting a different set calls `previewRuleBindingMigration`; show added/removed/changed rule keys and impacted information domains/workflow nodes. `确认迁移` requires a non-empty reason and calls `migrateRuleBindings`.
- Disable binding/migration controls when `disabled` is true (archived project). Preserve candidates, current bindings, selected IDs, preview, and reason on all failures.
- Use existing `Card`, `Input`, `Textarea`, `Switch` only when appropriate, `UIButton`, `notify`, and Tailwind conventions. No raw JSON editor and no dependency additions.

## TDD and verification

1. Write tests first for unbound state, published-only candidates, initial binding, preview/reason-gated migration, current-version display, archived disable, and failed migration state preservation.
2. Run `npm --prefix frontend-enterprise run test -- src/pages/audit-cases/components/RuleBindingPanel.test.tsx` and confirm the missing-panel RED failure.
3. Implement the minimal component.
4. Run the focused test command with Node.js 20.19.5 and confirm it passes.
5. Commit only the two Task 2 files as `feat: add project rule binding panel` and report the hash and test result.
