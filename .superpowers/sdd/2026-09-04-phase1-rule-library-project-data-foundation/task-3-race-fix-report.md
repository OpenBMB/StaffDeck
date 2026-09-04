# Task 3 race fix round 2 report

Date: 2026-09-04

Status: COMPLETE

## Scope and inputs

- Implemented only the remaining Critical Finding 3 from `task-3-fix-review.md`.
- Read `task-3-brief.md`, `task-3-fix-report.md`, and `task-3-fix-review.md` before changing code.
- Reused the existing project-role authorization helpers and did not change authorization behavior.
- Did not touch rules, frontend, APIs, OCR, ONLYOFFICE, retrieval, Task 4 files, database models, or database startup migrations.
- Preserved the pre-existing unrelated tracked and untracked worktree changes.

## Root cause confirmed

`approve_candidate` and `resolve_conflict` performed ordinary reads and checks, then called `_apply_candidate`. `_apply_candidate` re-read the current row, derived the next revision from whatever was then visible, and unconditionally mutated the current value, candidate, and conflict before commit. An intervening transaction could therefore cause either of two failures:

1. the stale operation observed the intervening revision and wrote a further revision, overwriting the winner; or
2. both operations targeted the same revision and the loser leaked the revision-history `IntegrityError`.

There was no conditional pending-to-approved transition for the candidate, no conditional open-to-resolved transition for a conflict, and no expected-revision predicate on the current-value update. The no-current-row path also relied only on an uncaught unique insert race.

## TDD evidence

Every pytest invocation set `TEMP` and `TMP` to the worktree-local `.pytest-temp-task3-race` directory and set `PYTHONPATH=backend`. The worktree-local repository uses the shared interpreter at `../../backend/.venv/Scripts/python.exe`.

### Baseline

Command:

```powershell
../../backend/.venv/Scripts/python.exe -m pytest backend/tests/test_project_data_service.py -q
```

Result before adding tests: `14 passed in 72.55s`.

### RED

Tests were added before any production change:

- `test_interleaved_approval_loser_is_a_conflict_without_a_second_revision`
- `test_interleaved_approval_loses_to_conflict_resolution_with_domain_error`

Command:

```powershell
../../backend/.venv/Scripts/python.exe -m pytest backend/tests/test_project_data_service.py -q -k 'interleaved_approval'
```

Result: `2 failed, 14 deselected in 18.39s`.

The first failure was `Failed: DID NOT RAISE ProjectDataConflictError`, proving that two approvals could both complete. The second failure leaked `sqlalchemy.exc.IntegrityError` from the unique revision-history constraint instead of returning `PROJECT_DATA_CONFLICT`. An earlier run exposed and corrected a test-local closure-name shadowing error before this valid RED run; that setup error was not treated as regression evidence.

After the initial GREEN, the approval/approval test was strengthened from a same-candidate race to two same-valued candidates at revision zero. Equal values intentionally bypass the existing different-pending-value conflict rule, so the loser reaches the no-current-row unique insert race. The final assertions require one current row, one revision, the winner approved, and the losing candidate rolled back to pending.

### GREEN

Initial focused regression run after the minimal production fix:

```text
2 passed, 14 deselected in 14.45s
```

Final focused run after strengthening the no-current-row case:

```text
2 passed, 14 deselected in 11.58s
```

Full project-data service run:

```text
16 passed in 61.23s
```

Integrated Task 1–3 backend command:

```powershell
../../backend/.venv/Scripts/python.exe -m pytest backend/tests/test_project_data_service.py backend/tests/test_project_data_models.py backend/tests/test_project_data_migration.py backend/tests/test_project_permissions.py backend/tests/test_audit_cases.py -q
```

Result: `45 passed in 125.45s`.

## Implementation

`ProjectDataService._apply_candidate` now performs a fenced state transition in one database transaction:

1. derives the immutable target revision from the candidate baseline for approval, or the conflict-captured revision for resolution;
2. conditionally updates exactly one candidate from `pending` to `approved`;
3. for conflict resolution, conditionally updates exactly one conflict from `open` to `resolved` at the captured revision;
4. for an existing current value, conditionally updates the row only when its revision still equals the expected revision;
5. for revision zero, inserts the unique current row and flushes inside the guarded transaction, so a concurrent creator becomes a caught lost race;
6. appends revision history and the audit event in the same transaction; and
7. rolls back and raises stable `PROJECT_DATA_CONFLICT` for any failed CAS or `IntegrityError` from a lost uniqueness race.

This ordering ensures that candidate approval and conflict resolution cannot both win. If a later CAS fails, the earlier candidate/conflict state changes are rolled back with the entire transaction.

## Files changed

- `backend/app/project_data/service.py`
- `backend/tests/test_project_data_service.py`
- `.superpowers/sdd/2026-09-04-phase1-rule-library-project-data-foundation/task-3-race-fix-report.md`

No model or database schema change was required.

## Self-review

- Candidate baseline remains authoritative; the caller cannot override it.
- Different pending values still create a conflict before mutation.
- Existing conflict creation and retry-idempotent uniqueness behavior is unchanged.
- `source_optional` persistence/loading and project-data serialization code are untouched and remain covered by the integrated tests.
- The candidate CAS prevents approval/approval and approval/conflict-resolution double wins.
- The conflict CAS prevents two conflict resolutions from both finalizing the same conflict.
- The current-value revision CAS prevents stale candidates from overwriting an intervening nonzero revision.
- The unique current-row insert plus guarded flush handles the no-current-row creation race.
- Any failed transition rolls back candidate, conflict, current value, history, and audit-event changes together.
- No unrelated refactor was included.

## Concerns

- The regression tests use deterministic two-session interleaving at the `_apply_candidate` boundary rather than probabilistic OS-thread timing. This is intentional: they exercise real SQLite commits and the exact CAS/unique-loss paths without weakening assertions or relying on scheduler luck.
- No known functional blocker remains.

## Commit

Commit message: `fix: make project data approval race-safe`

The commit hash is recorded after commit creation.
