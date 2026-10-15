# Independent Knowledge Core

This package installs and serves knowledge management and its shared UI without
StaffDeck or PilotDeck sources. See `ACCEPTANCE.md` for the current modular
deployment evidence and the separate records from earlier phases.

## Build and run

Copy this directory alone and run `node build.mjs` (Node 22+, npm and uv). This
builds the locked UI sources and includes them in the wheel. Then install
the wheel with `pip install dist/knowledge_core-0.1.0-py3-none-any.whl`.
Run `knowledge-core --database-url sqlite:///knowledge.sqlite --port 8787`.
Open `http://127.0.0.1:8787/` for the built knowledge UI.
Fresh databases default to `minimal`: documents, buckets, chunks, tasks,
retrieval and citations. No space, catalog, branch or version is created.
The default principal is instance-local, with no actor parameter or user tables.
Use `--config minimal.json` or a JSON module configuration:

```json
{"preset":"minimal","modules":{"catalog":true,"versions":true}}
```

Modules are `authentication`, `spaces`, `catalog`, `versions`, `concepts`,
`discovery`, `publication`. Versions requires catalog; publication requires
discovery. Missing module dependencies fail startup. Publication routes are
available only with a configured publisher adapter. Configuration
is fixed at startup; `/api/km/config` advertises available operations.
Full compatibility uses the same kernel and enabled module factories.
Existing complete deployments retain their configured capabilities. Historical
databases require an explicit offline schema conversion before starting this version.

Enable `authentication` in the configuration and supply
`--identity-file /path/to/tokens.json`. Authentication can run without spaces.
The file maps bearer tokens to objects containing `id`, `tenant_id`,
`display_name`, `spaces` (IDs or `*`) and `permissions` (`read`, `write`).
Missing tokens return 401; provider failures return 503 and never grant local access.
The standalone UI prompts for a bearer only when identity is enabled. It keeps
the credential in browser session storage and hides write controls for readers.

For SD's default `tenant_demo` namespace, configure a host bearer whose principal
has that namespace; a local-mode KM instance uses namespace `local`.
SD uses `STAFFDECK_KM_URL` and `STAFFDECK_KM_BEARER`. Runtime entrypoints default
to HTTP ownership even if the URL environment variable is empty.

Optional model and parsing delegation uses `KM_CAPABILITIES_URL` pointing to
`/api/internal/km/capabilities` and `KM_CAPABILITIES_BEARER`, matched by
`STAFFDECK_KM_CAPABILITIES_BEARER`. Credentials and model protocols stay in SD;
KM sends only the principal namespace, space and opaque model ID. Without this
adapter, the package uses local parsing and an optional `--model-config` model.

## Host plugins and capabilities

SD requires the installed `knowledge-core` wheel (the repository also declares
the local package as a uv source). Configure `STAFFDECK_KM_URL` and
`STAFFDECK_KM_BEARER`; an empty URL returns 503. The authenticated host
`/api/km/health/ready` checks both the database probe and the service credential.
SD has no native knowledge storage or ingestion fallback.

PD uses `knowledge-core/v1`, `rest-http-v1`, and frontend module `knowledge-core`.
`staffdeck.knowledge` is a deprecated frontend alias. A minimal binding is:

```yaml
modules:
  knowledge:
    enabled: true
    implementationId: knowledge-core
    frontendModule: knowledge-core
    contract: knowledge-core/v1
    transport: rest-http-v1
    endpoint: http://127.0.0.1:8787
    credentialEnv: PD_KM_SERVICE_TOKEN
    capabilitiesCredentialEnv: PD_KM_CAPABILITY_TOKEN
    namespace: local
    pilotDeckUserId: "1"
    spaceId: pd-local
    spaces:
      - id: pd-local
        name: Local knowledge
    methods: [query, resolve_citation]
```

Management methods must be explicitly declared and require a writable binding,
the bound PD user and KM write authorization. The shared operation registry is
`src/composition/knowledgeCoreOperations.mjs` in PD. Editing operations require
the caller's revision; they never fetch a revision to overwrite a concurrent edit.
`confirm_discovery` reviews a generic suggestion to `reviewed` without external
creation. `publish_discovery` is separate; SD legacy confirmation retains its
publication behavior. No management tools are added to the Agent by default.

PD supplies `/api/internal/km/capabilities/{catalog,model,parse}` with a server
bearer, using its existing model and file ports. The public catalog endpoint is
`/api/modules/knowledge/capabilities/catalog`. Actor identity comes from the
binding. Query uses the default PD model or explicit `model_id`; `use_model:false`
selects lexical retrieval. Real model acceptance excludes fallback results.

For a shared KM instance, use `--capabilities-file bindings.json` (or
`KM_CAPABILITIES_FILE`). Each exact tenant/space pair has its own provider:

```json
[
  {"tenant_id":"local","space_id":"pd-local","endpoint":"http://127.0.0.1:3001/api/internal/km/capabilities","credential_env":"PD_KM_CAPABILITY_TOKEN"},
  {"tenant_id":"tenant_demo","space_id":"sd:tenant_demo:gallery","endpoint":"http://127.0.0.1:8000/api/internal/km/capabilities","credential_env":"SD_KM_CAPABILITY_TOKEN"}
]
```

Duplicate pairs or missing credentials fail startup. Foreground requests and
background tasks resolve the same space provider.

Both host navigation paths embed `/ui/v1/index.html` from KM through their asset
proxy. `knowledge-frame/v1` messages carry directories, permissions, navigation
and notifications. The parent authenticates API requests, including upload
progress, binary downloads and cancellation; bearer values stay in the parent.
Origin, source, channel, protocol and allowed routes are checked. Unmount aborts
pending requests and protocol mismatch displays an error.

To upgrade UI without rebuilding hosts, pass `--ui-dir /path/to/ui` or
`KM_UI_DIRECTORY`. The directory must contain `index.html`, relative assets and
`version.json` with `{"protocol":"knowledge-frame/v1","version":"release-id"}`.
Replace this artifact and restart KM, then reload the host knowledge page.

API docs: `http://127.0.0.1:8787/docs`. Process liveness:
`/health/live`; database readiness: `/health/ready`.

## Migration

Module downgrade is an explicit offline operation on a development copy:

```sh
knowledge-core --database-url sqlite:///knowledge.sqlite --conversion preflight --config minimal.json
knowledge-core --database-url sqlite:///knowledge.sqlite --conversion convert --config minimal.json --source-config deployment.json --backup-root backups
knowledge-core --database-url sqlite:///knowledge.sqlite --conversion restore --backup backups/selected-backup
knowledge-core --database-url sqlite:///knowledge.sqlite --conversion recover
knowledge-core --database-url sqlite:///knowledge.sqlite --conversion export --backup backups/selected-backup --export-file recovery.json
```

Stop the service, finish/cancel tasks and checkpoint SQLite WAL first. The
converter stages a new database and keeps the old database, configuration,
report and document mapping under a persistent backup. Restore also restores
the recorded configuration. Any new write or module initialization blocks direct
restore. Interrupted database/configuration replacements block startup until
explicit recovery. Exporting new content does not permit overwriting it with an
older backup. Legacy records can be mapped into retained module storage, including
full mode. Production switching remains a separate maintenance operation.

The source is opened read-only and must be a consistent SQLite snapshot made
during the maintenance window. Cancel or finish running ingestion before making
the snapshot. Never pass the source file as the destination.

```sh
knowledge-core --migration preflight --source source-copy.sqlite
knowledge-core --migration migrate --source source-copy.sqlite --database-url sqlite:///new-km.sqlite
knowledge-core --migration compare --source source-copy.sqlite --database-url sqlite:///new-km.sqlite
```

Commands return nonzero on unresolved links or conflicting target resources.
Migration stages the historical host mappings in an isolated temporary database,
then converts them to the current module-owned schema before installing the target.
The target can start with the current Runtime; repeated migration compares its
contents and never overwrites later target edits.
The migration report includes an employee-to-space map for the host adapter;
the core database contains only generic spaces. Historical confirmed suggestions
are copied without publication. Unknown external targets remain unknown.
Known host publication receipts are migrated into generic publication records;
missing target references or inconsistent receipt identities fail preflight.
Confirmed suggestions without a receipt cannot be republished.

Before restoring writes, the host can reconnect to the old database. After
restoring writes, pause and reconcile changes before any rollback.

## Acceptance

See `ACCEPTANCE.md` for actual evidence and outstanding gates. A passed health
probe or unit test is not full independence acceptance.

`scripts/task_acceptance.py` exercises in-flight cancellation and a hard process
restart using an installed wheel. `scripts/cutover_acceptance.py` exercises a copied
KM database freeze, repeat migration and SD/PD evidence comparison. Both require
dedicated test ports and development data.
