# Task 3: integrate rule binding into project details

Work only in the Phase 3 worktree. Read the Phase 3 plan, ledger, and completed Task 2 report first.

## Files

- Modify `frontend-enterprise/src/pages/audit-cases/AuditCaseDetailPage.tsx`.
- Modify `frontend-enterprise/src/pages/audit-cases/AuditCaseDetailPage.test.tsx`.

## Required behavior

- Extend `DetailTab` with `'rules'` and add the `规则绑定` tab after `审核材料` and before `操作记录`.
- Render `<RuleBindingPanel caseId={detail.project.id} disabled={Boolean(archived)} />` only when the rules tab is active.
- Keep existing detail loading, admin route guard, archive protection, basic settings, member, materials, and events behavior unchanged.
- Do not load binding data on every detail-page mount; the panel may load when rendered after tab selection.

## TDD and verification

1. Add failing integration assertions for the new tab, panel rendering, and archived disabled binding controls.
2. Run `npm --prefix frontend-enterprise run test -- src/pages/audit-cases/AuditCaseDetailPage.test.tsx` and verify the expected missing-tab/panel RED failure.
3. Implement the tab branch.
4. Run the related focused suite with Node.js 20.19.5: `ruleBindingApi.test.ts`, `RuleBindingPanel.test.tsx`, and `AuditCaseDetailPage.test.tsx`; then run `npm --prefix frontend-enterprise run build`.
5. Commit only the two Task 3 files as `feat: integrate project rule binding tab` and report the hash and results.
