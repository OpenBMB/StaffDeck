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
