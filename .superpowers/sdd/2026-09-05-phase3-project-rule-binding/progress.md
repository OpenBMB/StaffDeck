# SDD ledger — plan: docs/superpowers/plans/2026-09-05-phase3-project-rule-binding.md

## Preflight scan

| Item | Shared files/interface | Check | Result/ruling |
|---|---|---|---|
| Task 1 → Task 2 | `ruleBindingApi.ts` exports client methods and read types consumed by `RuleBindingPanel.tsx` | The panel depends only on the exact functions/types defined in Task 1 | Consistent; Task 1 must commit the client before Task 2 starts |
| Task 2 → Task 3 | `RuleBindingPanel` props `{ caseId, disabled }` consumed by `AuditCaseDetailPage.tsx` | The integration adds one tab branch and does not change the detail hook | Consistent; Task 2 owns panel internals, Task 3 owns page integration |
| Task 1 own text | `ruleBindingApi.ts`, its test, existing Phase 2 rule-library client | Every exported function has a URL/query/body test and published filtering has an owning rule-set mapping assertion | Consistent |
| Task 2 own text | `RuleBindingPanel.tsx`, its test | Unbound, bound, preview, migration, archive disable, and request-failure states are all expressible without raw JSON editing | Consistent |
| Task 3 own text | `AuditCaseDetailPage.tsx`, its existing test | `'rules'` is added to `DetailTab`; the new branch renders the panel while existing tabs remain unchanged | Consistent |
| Global constraints | Existing backend endpoints and Phase 2 route/page | Only published versions are candidates; migration is explicit; no knowledge/OCR/ONLYOFFICE/report changes | Consistent |

## Rulings

- Ruling: treat backend `RULE_BINDING_NOT_INITIALIZED` as the normal empty state in the panel — why: the existing backend deliberately raises this stable code when a project has no current bindings, while the UI must offer initial binding — cost if wrong: a backend error-code mapping may need adjustment later, but no data is lost.
- Ruling: reuse the Phase 2 admin-only rule-set/version reads for candidates — why: the current audit-case management route is already administrator-only and this avoids introducing a new backend listing contract in this slice — cost if wrong: a future non-admin project-manager route will need a role-scoped published-version endpoint.
- Ruling: perform migration only after preview and non-empty reason — why: this matches the spec's explicit migration and auditability requirement — cost if wrong: users take one extra confirmation step, which is acceptable for a destructive binding change.
- Ruling: compare selected rule-version IDs in their submitted order — why: the backend persists and compares binding priority by list order, so reordering versions is a meaningful migration — cost if wrong: a user may need to preview a migration when only priority changed, which is intentional and auditable.
- Ruling: add direct tests for bind/preview/load failures and archived write protection — why: every request boundary must preserve the last visible state and archive protection must be observable, not inferred from disabled styling — cost if wrong: a small increase in focused test runtime.
- Ruling: treat any ordinary candidate/current-binding load error as a non-editable data-unavailable state — why: an initial empty binding list cannot be distinguished from a failed current-binding read, and allowing writes could create an unverified binding — cost if wrong: the administrator must reload the panel after a transient read failure.

## Task 1: complete

- Implemented in commit `f6a7aedef76f64d2ec2e9a0a984960ce18678358`.
- Added typed project-binding API functions and published-version option loading by reusing the Phase 2 rule-library client.
- Focused verification with Node.js `20.19.5`: 5 tests passed.
- Independent review was attempted twice but both review agents were interrupted before producing a valid conclusion. Local review checked the exact endpoint contracts against `backend/app/api/rules.py` and found no mismatch; this is recorded as local review, not independent approval.

## Task 2: complete after independent review and remediation

- Implemented in commit `1012d2e548b8ef66c297e0e021e8e6d88d6d764c`.
- Added the rule-binding panel with published-only candidates, explicit initial binding, pinned current-version display, migration preview, non-empty migration reason gate, archive write protection, and failure-state preservation.
- Independent review found and verified two defects: order-insensitive selection comparison, then editable state after ordinary load errors. The order fix is `c07729d8f29baffc1706e9ad54efd8e4cb7849fa`; the load-failure lock and direct load-failure tests are `9db1299`.
- TDD evidence: the order regression was RED at 1 failed/6 passed before `c07729d`, then GREEN at 9/9; load-failure tests were RED at 4 failed/9 passed before `9db1299`, then GREEN at 11/11.
- Final independent re-review: PASS. Node.js `20.19.5` focused verification: 11 tests passed; the reviewer found no new state regression. Details are in `task-2-review.md`.

## Task 3: complete after independent review

- Implemented in commit `9ba3b744d18a737a23e3e3fb4ebb84092536aaa3`.
- Added the `规则绑定` tab between `审核材料` and `操作记录`; the panel is rendered only after that tab is selected and receives the archive read-only flag.
- Existing project settings, member, material, and event branches were preserved. Integration focused verification passed 14/14 in the implementation run; the final related suite passed 19/19.
- Independent integration review: PASS. Details are in `task-3-review.md`.

## Cross-task final verification

- Full frontend regression under Node.js `20.19.5`: 60 test files and 271 tests passed.
- Frontend production build under Node.js `20.19.5`: `tsc -b && vite build` passed. Vite reported only the pre-existing large-chunk advisory.
- This phase changes only the administrator project rule-binding UI/client and one test typing issue; it does not change backend endpoints, knowledge retrieval, OCR, ONLYOFFICE, materials processing, or report generation.
