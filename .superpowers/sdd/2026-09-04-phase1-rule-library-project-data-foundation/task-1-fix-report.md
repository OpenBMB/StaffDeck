# Phase 1 Task 1/2 Review Fix Report

Status: DONE

Commit: this review-fix commit, based on `dc5dea7` (Task 1) and `136aee1` (Task 2).

## Changes

- Added transactional legacy/explicit role dual writes to `AuditCaseService.create_case()` and
  `replace_members()`.
  - New owners receive `project_admin` and new members receive `editor`.
  - Existing explicit roles, including `reviewer` and delegated `project_admin`, are preserved.
  - Removed members lose their explicit project role; the case owner role is retained.
  - The existing `owner_user_id` and `member_user_ids_json` fields remain populated for legacy
    compatibility, and existing same-tenant member validation remains in force.
- Made project-role startup backfill concurrency-safe.
  - SQLite now runs the role backfill and migration marker write in one `BEGIN IMMEDIATE`
    transaction against the supplied engine.
  - Role and marker inserts use SQLite `INSERT OR IGNORE`, PostgreSQL `ON CONFLICT DO NOTHING`,
    and a savepoint/`IntegrityError` fallback for other dialects.
  - Existing malformed, missing-user, and cross-tenant diagnostic warnings are unchanged.
- Added a real unique partial index for system `ProjectDataFieldDefinition` rows where
  `tenant_id IS NULL`.
  - Model metadata creates the index for new SQLite/PostgreSQL databases.
  - Startup migration creates the missing index before the role marker early-return, covering an
    already-created Phase 1 SQLite table.
  - A real second conflicting commit is tested and rejected.
- Replaced the misleading object-only
  `test_published_rule_version_has_immutable_identity_fields` test with a persistence test named
  `test_published_rule_version_publication_fields_are_persisted`.
  - This task now claims only storage of publication identity fields.
  - Published-version mutation protection remains assigned to the Task 4 rule service.
- No frontend, ONLYOFFICE, OCR, material retrieval, or report-runtime code was changed.

## TDD evidence

- Baseline before review-fix edits: Task 1/2 focused set, `18 passed in 55.52s`.
- RED for the four principal regressions: `4 failed in 16.32s`.
  - create did not write roles;
  - replace did not add/remove roles;
  - duplicate system definitions committed successfully;
  - concurrent backfills raised the role unique constraint.
- Additional RED for an existing database with the model index removed: `1 failed in 4.29s`
  because startup did not recreate the index.
- Focused GREEN after implementation:
  - role dual-write tests: `2 passed in 9.80s`;
  - system unique model test: `1 passed in 4.07s`;
  - concurrent migration plus existing-table index migration: `2 passed in 8.27s`.
- Final Task 1/2 focused regression, including service, migration, permissions, and API paths:
  `41 passed, 4 warnings in 82.04s`.
  - The four warnings are pre-existing FastAPI `on_event` deprecations in `backend/app/main.py`.
- Ruff:
  - all changed non-model files passed;
  - `backend/app/db/models.py --ignore UP045` passed;
  - historical `UP045` count at `HEAD` and this commit is `251 -> 251`, so this commit introduced
    zero new Ruff findings;
  - `git diff --check` passed.

All pytest commands set `TEMP` and `TMP` to the worktree `.pytest-temp`, set `PYTHONPATH` to the
worktree `backend`, disabled bytecode writes, and reused the main checkout's existing backend
virtual-environment interpreter because this isolated worktree has no local `.venv`.

## Deferred concerns

1. Published rule-version immutability is deferred to Task 4. Task 4 explicitly owns
   `RuleLibraryService.replace_rules()` and the negative test that a published version raises
   `RULE_VERSION_IMMUTABLE`. The current Task 1 model is intentionally not presented as a
   database-level immutability guarantee; this report's 41-test result proves persistence only.
2. Cross-tenant associations among the new project-data and rule models remain service/API
   responsibilities because the current models store string IDs and the repository has not chosen
   composite foreign keys as a universal pattern. Task 3 must validate project-data case/user
   associations, Task 4 rule-set/version/definition associations, Task 5 project binding and
   evaluation associations, and Task 6 API tenant/project isolation and non-disclosure. Those
   services/endpoints do not yet exist in this Task 1/2 checkout, so adding isolated model checks
   here would either be bypassable or prematurely implement later services. The review's
   cross-tenant persistence probe therefore remains valid deferred evidence, not a closed claim.
3. Overall-design fields omitted from the Task 1 brief remain deferred to their owning service
   tasks: rule ownership/tags/current publication, publication notes/effective windows/source
   snapshots, and rule failure/exception policy belong to Task 4; binding migration metadata and
   evaluation execution/request identifiers belong to Task 5; externally writable representations
   and isolation checks belong to Task 6. No migration was added without an owning behavior or
   test plan.
4. The repository-wide 251 `UP045` findings in `backend/app/db/models.py` predate this review-fix
   commit. They were measured against `HEAD` and did not increase; a whole-file annotation rewrite
   would be unrelated churn and is not included.
