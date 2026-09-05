# SDD ledger — plan: docs/superpowers/plans/2026-09-05-phase4-project-document-workspace.md

## Scope

- Workspace: `codex/phase4-project-document-workspace`
- Base: `64207fa`
- Goal: project document workspace foundation; external ONLYOFFICE is deferred.

## Preflight scan

| Item | Checked relationship | Result | Ruling |
|---|---|---|---|
| Task 1 ↔ Task 2 | Task 1 service/schema outputs are Task 2 route inputs | Names and return shapes are compatible | None |
| Task 2 ↔ Task 3 | Task 2 JSON response supplies typed client/component data | `active_version` and `versions` are explicitly required | None |
| Task 3 ↔ Task 4 | Component props and hook state are defined before integration | `documents`, `disabled`, `onChanged` match | None |
| Task 4 ↔ Task 5 | Integration leaves verification commands and deferred scope explicit | No production editor claim | None |
| Task 1 self-consistency | Tests cover immutable version, role, archive, material isolation | Model/service contract is testable without external services | None |
| Task 2 self-consistency | Tests and route/error mapping describe the same API | `tenant_id` and status mapping are explicit | None |
| Task 3 self-consistency | UI tests cover local editor retention and disabled writes | Component can be tested with mocked typed client | None |
| Task 4 self-consistency | Tab/load/refresh behavior is independently testable | No changes to existing materials/rules semantics | None |
| Task 5 self-consistency | Verification and review are bounded to this branch | No merge/push or destructive action | None |

## Rulings

- Ruling: use Markdown/Text snapshots for this phase instead of writing `.docx` or wiring ONLYOFFICE — this keeps the evidence chain and current runtime stable; the cost if wrong is a later adapter migration for binary editor payloads.
- Ruling: use a logical document table plus immutable version table instead of reusing `AuditReportVersion` — report versions have report-specific provenance fields and are not a general project workspace; the cost if wrong is an additional migration path.
- Ruling: cross-tenant access keeps the existing authentication contract and returns 403 for tenant mismatch; only same-tenant missing cases/documents return 404. This avoids introducing a new tenant-hiding convention in this phase; the cost if wrong is a caller expectation mismatch.

## Task status

- Environment note: the repository `task-brief` helper was found but could not run because this Windows host has no `/bin/bash` or WSL Bash; task briefs are therefore supplied directly in the subagent prompt and the full plan remains the source of truth.

- Task 1: complete (commit 7139b903, local verification clean; independent review service made 4 attempts without returning a verdict)
- Task 1 review note: controller independently inspected `git show 7139b903`, ran `backend/tests/test_audit_case_documents.py`, and observed `8 passed in 34.37s`; no Critical/Important issue was found in the local contract check. This is recorded as local verification, not a proxy claim of subagent review approval.
- Task 2: complete (commit 9e1a196, local verification clean)
- Task 2 review note: controller added API tests for create/list/detail, optimistic version conflict, archive/viewer permission, unsupported format, cross-tenant mismatch and missing case; focused suite observed `11 passed in 36.33s`.
- Task 3: complete (commit 069dbcb, local verification clean)
- Task 3 review note: independent read-only review returned no Critical/Important issues; component suite observed `4 passed`; the reviewer noted only a Minor type-widening issue for `content_format`.
- Task 4: complete (commit 2647264, local verification clean)
- Task 4 review note: project detail tab/load/refresh integration test suite observed `4 passed`; related frontend regression suite observed `11 passed`; production build completed successfully.
- Task 5: complete for the scoped Phase 4 foundation (commits 7139b903, 9e1a196, 069dbcb, 2647264, 03d86fb)
- Task 5 verification: frontend full suite observed `61 files passed, 278 tests passed`; frontend production build passed. Backend Phase 4 plus related audit-case suite observed `21 passed`; backend full suite observed `2322 passed, 2 skipped, 16 failed`.
- Task 5 failure attribution: the 16 full-suite failures are outside the Phase 4 diff. Individual reruns confirmed the first WeChat recovery case and the Feishu close-race case pass on isolation; the remaining failures reproduce in WeCom concurrency, Feishu watchdog/thread cleanup, general-skill/Bash behavior, artifact metadata, harness sandbox behavior, and Markdown `SIGALRM` assumptions. Four `test_dev_scripts` failures stop at absent `packaging/sandbox_runtime/bin/node.exe` or sibling `tools/npm.cmd`; the Bash guard fails because this host is Windows without Bash. No Phase 4 file was changed to mask these unrelated failures.
- Task 5 scope boundary: this phase provides Markdown/Text project document snapshots, immutable versions, optimistic conflict handling, project-role authorization, archive/read-only behavior, and the project detail tab. ONLYOFFICE, `.docx` binary round-trip, callback/JWT integration, and document-to-material domain synchronization remain deferred to the next phase.
