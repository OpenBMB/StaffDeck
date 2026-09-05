# Task 3 fix report — project-data candidate/conflict hardening

Date: 2026-09-04

Status: COMPLETE

## Scope and review inputs

- Read `task-3-brief.md` and `task-3-report.md` before changing code.
- The independent review file was not present at the supplied path; the six findings in the fix brief were used as the authoritative review input.
- Preserved all 37 explicitly listed system field keys. The original brief's stated count of 36 is contradictory and was not used to remove a key.
- Reused the existing project-role authorization seam in `project_data.permissions`; no authorization paths were added or replaced.
- Did not touch rules, frontend, APIs, OCR, ONLYOFFICE, retrieval, or unrelated migrations.

## Root causes confirmed

1. Candidate rows retained a nullable submission revision, and approval preferred the caller's `expected_current_revision` over the candidate's stored baseline. A caller could therefore match the newer current revision and overwrite it with a stale candidate.
2. Approval did not inspect other pending candidates for the same tenant/case/field before mutating the approved value.
3. Conflict creation always inserted and committed a new row and audit event. It had neither a retry identity nor a database uniqueness constraint.
4. `FieldDefinition.source_optional` existed only in the Pydantic model. The database model, startup migration, and tenant-definition loader omitted it.
5. Project-data schema serialization was not covered in `test_project_data_models.py`.
6. Unknown tenant `value_type` values fell through validation, and the combined empty-value/source test never exercised a genuinely missing source.

## TDD evidence

All pytest runs used the worktree-local `.pytest-temp-task3-fix` directory through `TEMP` and `TMP`, with `PYTHONPATH=backend`. The worktree does not contain `backend/.venv`, so the repository's shared interpreter at `../../backend/.venv/Scripts/python.exe` was used.

### Baseline

Command:

```powershell
../../backend/.venv/Scripts/python.exe -m pytest backend/tests/test_project_data_service.py backend/tests/test_project_data_models.py backend/tests/test_project_data_migration.py -q
```

Result: `15 passed in 56.51s`.

### RED

After adding the focused behavior tests and before production changes:

- Main regression run: `9 failed, 17 passed in 87.88s`.
- Failures matched the intended missing behavior: unknown value type accepted, tenant `source_optional` lost, null candidate baseline retained, approval override accepted, sibling pending candidate ignored, duplicate conflict/event created, model fields/index absent, and legacy field-definition migration absent.
- A separate legacy conflict migration test failed because `trigger_candidate_id` and `uq_project_data_open_conflict_retry` did not exist: `1 failed in 1.77s`.

### GREEN

Focused Task 3 model/service/migration run:

```text
27 passed in 106.10s
```

Integrated Task 1–3 backend run:

```powershell
../../backend/.venv/Scripts/python.exe -m pytest backend/tests/test_project_data_service.py backend/tests/test_project_data_models.py backend/tests/test_project_data_migration.py backend/tests/test_project_permissions.py backend/tests/test_audit_cases.py -q
```

Final result after all edits: `43 passed in 125.39s`.

Static verification:

- Full Ruff on `project_data/fields.py`, `project_data/service.py`, and the three changed project-data test files: passed.
- Ruff `F,I` checks on the changed legacy-sized `db/models.py` and `db/database.py`: passed.
- Python compile check for all changed Python files: passed.
- `git diff --check`: passed before commit.

## Implementation changes

### Candidate baseline and approval safety

- Candidate submission now stores the current field revision when `expected_revision` is omitted, making every new candidate revision-bound.
- Approval always compares the stored candidate baseline with the current revision.
- A supplied approval revision is only accepted when it agrees with the candidate baseline; it can no longer replace that baseline.
- Legacy candidates with a null baseline are treated as conflicts rather than being allowed to overwrite current data.

### Pending-candidate conflicts

- Approval checks all pending candidates in the same tenant/case/field.
- Any other pending candidate with a different JSON value creates/returns `PROJECT_DATA_CONFLICT` before current-value mutation.
- Equal-valued pending candidates do not create a false conflict.

### Atomic, retry-idempotent conflict creation

- Added nullable `ProjectDataConflict.trigger_candidate_id`.
- Added partial unique index `uq_project_data_open_conflict_retry` over tenant, case, field, trigger candidate, and current revision for open conflicts.
- Conflict and audit event are committed in one transaction.
- Sequential retries reuse the existing open conflict and do not append another audit event.
- Concurrent unique-key losers roll back their conflict and event together, reload the winning open conflict, and return `PROJECT_DATA_CONFLICT`.
- Migrated legacy open conflicts with null trigger keys remain readable; service fallback reuses them when their candidate list contains the retried candidate.

### Field definitions and validation

- Added persisted `ProjectDataFieldDefinition.source_optional` with default `False`.
- Startup migration adds the non-null Boolean column with a false default for existing databases.
- Tenant field-definition loading now propagates `source_optional` into the validation model.
- Unknown tenant value types now raise stable `UNKNOWN_VALUE_TYPE`.
- Empty-value and missing-source tests are independent, so the missing-source branch is exercised directly.

### Serialization coverage

- Added nested `SourceRef` serialization coverage for candidate requests.
- Added revision and candidate-ID serialization checks for project-data value, candidate, and conflict read schemas.

## Files changed

- `backend/app/db/models.py`
- `backend/app/db/database.py`
- `backend/app/project_data/fields.py`
- `backend/app/project_data/service.py`
- `backend/tests/test_project_data_models.py`
- `backend/tests/test_project_data_service.py`
- `backend/tests/test_project_data_migration.py`
- `.superpowers/sdd/2026-09-04-phase1-rule-library-project-data-foundation/task-3-fix-report.md`

## Self-review

- Confirmed approved values and candidate statuses remain unchanged on all conflict paths.
- Confirmed conflict resolution still advances from the conflict's captured current revision.
- Confirmed the retry key includes the target current revision, so a later revision can legitimately produce a new conflict while an exact retry cannot duplicate one.
- Confirmed conflict uniqueness is limited to open rows, allowing resolved history to remain immutable.
- Confirmed old field-definition rows backfill `source_optional=False` and repeated startup migration remains safe through `IF NOT EXISTS` plus column inspection.
- Confirmed all 37 explicit stable system keys remain unchanged.
- No known functional blocker remains. The large legacy database modules contain pre-existing style findings outside Task 3; this round did not expand into unrelated cleanup.

## Commit

Commit message: `fix: harden project data conflict workflow`

The final commit hash is reported by the controller after commit creation.
