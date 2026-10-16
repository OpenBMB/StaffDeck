# PR 285 Review And Fix Acceptance

Reviewed PR head: `2f4cd28bbbac6390af78236e269e850cea587f8c`.
All checks below run locally against the review fixes on 2026-10-16.

## Confirmed Findings And Fixes

| Priority | Trigger and failure | Fix and regression |
| --- | --- | --- |
| P1 | SD proxy accepts an authorized query space and a different body space, or forwards an unscoped request with the host credential. | Reject conflicting/invalid selectors, inject the authorized body scope, and always scope forwarded calls. Real host permission integration in `backend/tests/test_knowledge_proxy.py`. |
| P1 | A queued ingestion completes after the branch head changes and writes into the new branch. | Claim the captured head inside the content transaction; fail without partial content on mismatch. `test_queued_ingest_does_not_write_into_a_changed_branch`. |
| P1 | Wiki writes bypass published/shared document protection, including detachment and documentless Wiki edits. | Check original/target documents and the editable head; new Wiki creation forks a writable branch. Published document and detached Wiki regressions in `test_review_regressions.py`. |
| P2 | Full-mode space creation and lint persist writes during maintenance/read-only mode; lint only requires read permission. | Apply runtime write authorization before writes. `test_full_maintenance_rejects_space_creation_and_lint_writes`. |
| P2 | Same-named documentless Wiki pages collide across bases or copied versions. | Match normalized IDs within tenant/space/base/version scope and save citation recovery context. `test_detached_concepts_are_independent_across_bases_and_copied_versions`. |
| P2 | Binding a base into another space loses documentless Wiki visibility; copying keeps the source space. | Resolve detached pages through the visible bound branch version and assign copied rows to the target context. `test_detached_wiki_survives_binding_and_copy_into_another_space`. |
| P2 | Permanent base deletion leaves detached Wiki, lint suggestions, contexts and citations. | Delete both document-owned and context-owned rows. `test_permanent_delete_removes_detached_wiki_and_lint_records`. |
| P2 | Exporting a documentless lint suggestion returns 422; typed SDK rejection raises validation errors after the server has rejected it. | Restore stored base context for export and return the operation result for rejection. `test_typed_client_exports_and_rejects_documentless_lint_suggestion`. |

Each finding was reproduced by a failing regression before its fix. Subsequent
review of these paths and the final regressions found no further blocking issue
within this review scope.

## Final Validation

- `regression/report.json`: 125 core tests and 81 SD compatibility tests pass;
  collected test IDs, JUnit XML and logs are included.
- `isolated-final/isolated-artifact.json`: copy only `knowledge_core`, build a new
  wheel, install into an independent environment, run minimal desktop/mobile UI,
  inspect runtime imports and storage, and delete test content successfully.
- `isolated-final/browser-model.json`: real `deepseek-v4-flash-0731` calls for
  ingestion and document/bucket routing, with call IDs; no fallback route.
- `final-wheel-regressions.xml`: six client/review regressions pass against the
  installed final wheel with no source `PYTHONPATH` (Python 3.14).
- `full-artifact-final/report.json`: 20 full UI workflows pass against the final
  wheel, including document/Bucket/Chunk/Wiki edits, retrieval/citation, version
  copy/publish/rollback/sync, hide/restore, graph, OKF import/export and mobile.
  No page errors; no host imports or host database tables. Screenshots inspected.
- `task-final/report.json`: installed final wheel passes running cancellation
  and hard-restart interruption; zero partial documents and cleared payloads.
- `authorization/report.json`: actual HTTP permission and namespace acceptance.
- `host-browser-final/report.json`: SD/PD desktop/mobile browser workflows,
  persisted cancellation, discovery review/rejection, SD Skill/Tool publication,
  and independent KM UI upgrade pass. This run uses a deterministic local model
  fixture, separately from the real provider runs.
- `sd-agent-final/report.json`: actual StaffDeck `app.main` lifespan and Harness
  with real model calls, seven Agent turns and six independent HTTP readbacks
  pass. Both services terminate on SIGTERM without forced killing.

Commands are provided by `scripts/regression_acceptance.py`,
`scripts/isolated_minimal.py`, `scripts/artifact_acceptance.py`,
`scripts/task_acceptance.py`, `scripts/authorization_acceptance.py`,
`scripts/sd_agent_acceptance.py`, and the companion PilotDeck
`scripts/g4-knowledge-cancel-discovery-browser.mjs`. Final reports retain exact
environment paths and model call evidence. Private model configuration,
databases, virtual environments and disposable source/build copies are excluded.

## Boundaries

This is development and packaged-artifact acceptance, not production migration
or deployment. The SD/PD browser runs use existing built host frontend assets
with the current KM/backend code. The PilotDeck browser runner reports an exit
code of 1 during teardown, with no forced termination; its shutdown log and the
passing workflow report are retained. This is not claimed as a clean PilotDeck
process-exit acceptance. The separate full-wheel service closes on SIGTERM.
StaffDeck's real Agent run still logs the previously documented
`ASGI callable returned without completing response` warnings while closing
background streams; application shutdown completes. The raw log is retained.

The broader PR's previously documented legacy Harness fixture failures and
production migration constraints are unchanged; this review's passing counts
refer only to the explicit collected regression groups above.
