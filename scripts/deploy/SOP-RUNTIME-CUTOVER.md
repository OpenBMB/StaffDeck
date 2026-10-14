# SOP v3 /dsh Cutover

This is an operator procedure for the paired delivery branch
`standalone_alpha1_decouple_sop_1013`. No server has been changed by the local
acceptance. Verify the existing layout in README.md on the actual server before
using any path below.

## Inspect And Prepare

1. Record the actual /dsh StaffDeck and PilotDeck checkouts, commit IDs, service
   users, systemd units, environment files, worker processes, database paths,
   frontend base path and gateway routing. Preserve the root instance.
2. Confirm the paired commits from the local private `release-pair.json`.
   Install `portable_sop` in the StaffDeck host environment. Build a separate
   Runtime image with `portable_sop/Dockerfile`; it copies no backend code.
3. Run Runtime with an independent persistent volume mounted at `/data`,
   `SOP_RUNTIME_DB=/data/sop-runtime.sqlite`, and the existing private publisher
   credential as `SOP_PUBLISH_TOKEN`. Bind port 8091 on loopback. Verify
   `/healthz` reports `sop.lifecycle/v3`.
4. Back up the actual StaffDeck SQLite with its online backup mechanism and
   retain the current Runtime volume if present. Record backup locations and
   the worker/service configuration to restore.

## Migrate And Switch

1. Pause management writes, drain existing legacy SOP sessions and stop all
   hosts/workers that could change legacy definitions. Runtime remains running.
2. From the StaffDeck backend environment, set the actual `DATABASE_URL`,
   `SOP_RUNTIME_ENDPOINT`, private `SOP_PUBLISH_TOKEN` and temporary
   `SOP_RUNTIME_LEGACY_WRITES=compat`. Execute:

   ```bash
   python -m app.skills.migrate_sop_runtime --all-tenants \
     --report /private/deploy/sop-migration.json
   ```

3. Require exit code zero and report status `passed`. Review every tenant,
   gallery, overall-agent and employee row, publication totals, definitions,
   versions and drafts. Archived employees are excluded by existing visibility;
   explicitly inventory them before deciding the migration is complete. Compare
   actual source records and preserved draft IDs/history against the new tables.
   A partial failure requires repair/retry with writes still paused.
4. Configure every StaffDeck worker with the same Runtime endpoint and
   `SOP_RUNTIME_LEGACY_WRITES=disabled`; configure PilotDeck's SOP module with
   `sop.lifecycle/v3`, `sop-http-v3`, the same endpoint and published definition
   scope. Keep each host's model, authorization, capabilities and channels.
5. Restart the actual /dsh services. Verify their effective configuration and
   worker list, then reopen management writes. Do not infer uptake from the
   edited environment files alone.

## Online Acceptance And Main Instance

Record management edit/publication/version/child checks, new-table writes,
the paired Tool/Skill/Knowledge business workflow, human approval, real Feishu
input/reply IDs, Runtime revisions, duplicate resume and process-restart
continuation. Keep failures and missing evidence visible in the report.

Only after /dsh passes, schedule the main-instance window with its owner and
record the service/routing changes, backup locations and rollback decision.
Main-instance cutover has not been performed by this procedure's preparation.

For rollback, pause new execution and management writes first. Preserve the new
Runtime SQLite and host delivery/context stores, including their latest progress.
Do not overwrite them with pre-cutover databases or run an old SOP engine over
sessions advanced by v3. Restore service/routing configuration only after
classifying sessions that must remain on the new Runtime; keep /dsh evidence.
