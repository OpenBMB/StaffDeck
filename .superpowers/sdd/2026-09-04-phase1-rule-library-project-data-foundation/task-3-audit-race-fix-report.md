# Task 3 audit-state race fix round 3 report

Date: 2026-09-04

Status: COMPLETE

## Scope and inputs

- Read `task-3-brief.md`, `task-3-race-fix-report.md`, and `task-3-race-fix-review.md` before changing code.
- Addressed only the two remaining Important audit-state races from Critical Finding 3.
- Changed only the project-data service, its service tests, and this requested report.
- Did not modify models, migrations, Task 4/rules files, frontend, APIs, OCR, ONLYOFFICE, or retrieval.
- Preserved pre-existing untracked worktree artifacts and the independently committed Task 4 work.

## Root cause

1. `approve_candidate` ignored a relevant existing open conflict when its candidate baseline matched and the competing candidate had already been rejected. It therefore called `_apply_candidate` as an ordinary approval, allowing its candidate/current-value CAS to win while leaving the conflict open at the old revision.
2. `reject_candidate` loaded a pending ORM candidate and later mutated that stale instance unconditionally. An approval committed between the read and flush could therefore be overwritten by the stale rejection status and decision metadata.

## TDD evidence

Every pytest invocation set `TEMP` and `TMP` to worktree-local `.pytest-temp-task3-race3` and set `PYTHONPATH=backend`. The worktree used the shared interpreter at `../../backend/.venv/Scripts/python.exe`.

### Baseline

```powershell
../../backend/.venv/Scripts/python.exe -m pytest backend/tests/test_project_data_service.py -q
```

Result before new tests: `16 passed in 74.14s`.

### RED

Added before production changes:

- `test_interleaved_conflict_resolution_loses_to_approval_with_coherent_audit_state`
- `test_interleaved_rejection_loses_to_approval_without_overwriting_audit_state`

Focused command:

```powershell
../../backend/.venv/Scripts/python.exe -m pytest backend/tests/test_project_data_service.py -q -k 'interleaved_conflict_resolution_loses_to_approval or interleaved_rejection_loses_to_approval'
```

Result: `2 failed, 16 deselected in 19.17s`.

- The inverse approval/conflict-resolution test failed because the approval winner left `final_conflict.status == "open"` instead of `"resolved"`.
- The approval/rejection test failed because stale rejection did not raise `ProjectDataConflictError` and overwrote the approval winner.

### GREEN

The same focused command after the minimal service changes: `2 passed, 16 deselected in 15.36s`.

All deterministic interleaving tests:

```powershell
../../backend/.venv/Scripts/python.exe -m pytest backend/tests/test_project_data_service.py -q -k 'interleaved'
```

Result: `4 passed, 14 deselected in 19.84s`.

Full service suite:

```powershell
../../backend/.venv/Scripts/python.exe -m pytest backend/tests/test_project_data_service.py -q
```

Result: `18 passed in 60.65s`.

Integrated Task 1-3 suite:

```powershell
../../backend/.venv/Scripts/python.exe -m pytest backend/tests/test_project_data_service.py backend/tests/test_project_data_models.py backend/tests/test_project_data_migration.py backend/tests/test_project_permissions.py backend/tests/test_audit_cases.py -q
```

Result: `47 passed in 135.46s`.

## Implementation

- `_open_conflict` now treats candidate membership as relevant regardless of which candidate originally triggered the conflict.
- When an ordinary approval has a matching baseline and a relevant open conflict, it passes that conflict into the existing fenced `_apply_candidate` transaction. The candidate, conflict, current value, revision history, and audit event then finalize together as one `resolve_conflict` winner. A concurrent explicit resolution loses a CAS and returns `PROJECT_DATA_CONFLICT` without partial state.
- `reject_candidate` now performs a conditional SQL update constrained by candidate identity, tenant, case, field, and `status == "pending"`. A zero-row update rolls back and raises `PROJECT_DATA_CONFLICT`; the rejection audit event is emitted only after the conditional transition succeeds and is committed in the same transaction.

## Files changed

- `backend/app/project_data/service.py`
- `backend/tests/test_project_data_service.py`
- `.superpowers/sdd/2026-09-04-phase1-rule-library-project-data-foundation/task-3-audit-race-fix-report.md`

No model or migration change was required.

## Self-review

- Both approval/conflict-resolution orderings now assert candidate decision metadata, conflict resolution metadata, current value/revision/actor, revision-history operation/actor, and the single finalization audit event.
- The approval/rejection interleaving asserts the approval winner remains authoritative, no rejection event is recorded, and no conflict or extra revision is created.
- Existing candidate/current/conflict CAS behavior and lost-uniqueness mapping remain unchanged.
- Candidate baseline checks and caller expected-revision checks still run before open-conflict finalization.
- Different competing pending values still create/reuse a conflict instead of approving.
- Existing idempotency, `source_optional`, serialization, model, and migration behavior remained covered by the integrated suite.
- `git diff --check` passed; only line-ending normalization warnings were reported.

## Concerns

- The interleaving tests deliberately use two real SQLModel sessions and deterministic service-boundary hooks rather than scheduler-dependent threads. They exercise real SQLite commits and CAS row counts without weakening SQLite behavior.
- The worktree contains many pre-existing untracked test/report artifacts; they are intentionally excluded from this commit except for this requested report.
- No known functional blocker remains.

## Commit

Commit message: `fix: preserve project data finalization audit state`

The commit hash is recorded in the final handoff after commit creation.
