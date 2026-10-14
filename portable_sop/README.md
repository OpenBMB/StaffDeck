# Independent SOP HTTP Runtime

This directory contains the standalone SOP lifecycle core and HTTP runtime.
It does not import StaffDeck's ORM, Harness, AgentLoop, channels, identity
providers, or model/tool implementations. Hosts publish definition snapshots
and submit authorized capability receipts.

Build from the StaffDeck repository root:

```bash
docker build -f portable_sop/Dockerfile -t portable-sop-runtime .
```

For local execution, install this package into an isolated environment:

```bash
pip install -e portable_sop
python -m staffdeck_sop_runtime
```

The v3 tests use only Core, SQLite, and HTTP dependencies. Legacy parity tests
live in the host environment and use `staffdeck_harness.sop.legacy_api`.

Runtime configuration:

- `SOP_RUNTIME_DB`: persistent SQLite file (default `./data/sop-runtime.sqlite`).
- `SOP_DEFINITIONS_DIR`: directory containing JSON definitions or `{sops: [...]}` bundles.
- `SOP_PUBLISH_TOKEN`: optional bearer credential for the definition publication port;
  set this when publishing outside a trusted local network.

StaffDeck cutover requires installing this package in the host environment and
setting `SOP_RUNTIME_ENDPOINT`. Publish existing definitions first:

```bash
python -m app.skills.migrate_sop_runtime --tenant-id TENANT --agent-id AGENT
```

For the deployment cutover, migrate every tenant's gallery and non-archived
employee/overall-agent scope and write per-scope publication/management counts:

```bash
SOP_RUNTIME_LEGACY_WRITES=compat python -m app.skills.migrate_sop_runtime \
  --all-tenants --report /private/deploy/sop-migration.json
```

Inspect archived employees separately before cutover: they are excluded by the
host's existing resource visibility policy. A failed run records completed
scopes and fails the command; repeat with management writes still paused.

This command exports definitions only. Existing live host-owned sessions must
finish before cutover; it does not import mutable session rows. Internal and
public publication use `SopManagementService`. The command also copies visible
definitions and drafts into the new management tables. After migration, set
`SOP_RUNTIME_LEGACY_WRITES=disabled` on all StaffDeck workers. Existing management
APIs then write `sop_management_definitions`, `sop_management_versions`, and
`sop_management_drafts`; they do not create Skill or API SOP draft rows. Direct
legacy normalizers/version writers reject with `SOP_LEGACY_WRITES_DISABLED`.
Management tables and UI remain host-owned; graph state belongs to Runtime.

The write switch is process-wide. Migrate every tenant/employee scope (including
the gallery scope), pause management writes while exporting, and drain legacy
sessions before enabling it on all workers. The migrator preserves definition
history, draft IDs, and binding metadata; it does not remove old rows. Direct
ORM mutations of old SOP tables are rejected after cutover. Employee SOP import,
sync, promotion, rollback, and employee copying use the same management service
and new definition records. Existing resource summaries use the new bindings.

PilotDeck selects `sop.lifecycle/v3` / `sop-http-v3`. Its local files hold reply
delivery, tool receipts, and recovery projections; reads refresh Runtime state.
Node model contexts remain host-owned. SOP-only exports copy `portable_sop`
without StaffDeck backend and mount a separate Runtime SQLite volume.

## Independent management for PilotDeck

The same Runtime process now mounts `POST /v1/management/call` and
`GET /v1/management/catalog`. Management owns its own SQLite tables for SOPs,
revisioned drafts, publication pointers, generation jobs and replayable events.
Immutable versions use the existing independent definition snapshot store.
It imports neither `app.*` nor StaffDeck ORM. StaffDeck's existing management
adapter remains available for existing deployments.

Calls have `{operation, input}` and return `{ok, result}`. Operations include
`list`, `create`, `import`, `get_draft`, `replace_draft`, `validate`, `publish`,
`list_versions`, `get_version`, `rollback`, `archive`, and `delete`. Draft
updates/publication require `sopId`, `draftId`, and `expectedRevision` or `etag`.
Publication validates and pins child SOP versions, saves a snapshot, and
updates its pointer in one transaction. Rollback moves only the pointer.
Archived/deleted SOPs reject new sessions; existing sessions keep their snapshot.
Set `SOP_PUBLISH_TOKEN` to require the server-side publisher bearer.

PD's generation worker owns model credentials and calls the `job_*` operations
to save tasks, chunks, cancellation, completion and worker restart interruption.
Completed generation saves a validated draft, never a publication. PD uses
`SOP_RUNTIME_PUBLISH_TOKEN` for the matching server-side publisher credential.
In native mode, `assignee_ref: pilotdeck:owner` is normalized at validation;
PD authenticates the active owner and owns notification/continuation delivery.

## Optional SOP features

Configure the independent Runtime/Management process and restart it:

```bash
SOP_KNOWLEDGE_ENABLED=false \
SOP_VERSION_MANAGEMENT_ENABLED=false \
python -m staffdeck_sop_runtime
```

Both settings default to `true`. `features` on `/v1/management/call` returns
`{knowledge, versionManagement}`; PilotDeck native SOP reads this policy for
its editor and generation capability list.

- Disabling knowledge removes knowledge choices from the native editor and
  generation bindings. Management validation, direct publication and execution
  reject knowledge-dependent definitions with `SOP_FEATURE_DISABLED`, including
  expanded child definitions. Existing knowledge-dependent sessions cannot
  advance while disabled; ordinary SOPs continue to work.
- Disabling version management removes history, rollback and manual version
  editing from the native page. The management API rejects `list_versions`,
  `get_version` and `rollback`. Editing and publication remain available:
  publication allocates an internal immutable snapshot ID automatically.
  Existing sessions retain their pinned snapshots. No historical data is deleted.

These are SOP-service feature switches, not global host knowledge switches.
StaffDeck's legacy management UI is not controlled by the native PilotDeck page.

The installed three-service, four-combination and real-model webpage acceptance
is recorded in [ACCEPTANCE-FEATURES-1014.md](ACCEPTANCE-FEATURES-1014.md).
