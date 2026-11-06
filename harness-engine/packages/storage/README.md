---
description: "The storage group map: durable non-session data through named backends and the typed domain data form, for users and maintainers navigating the group."
kind: "package-group"
---

# packages/storage

English | [中文](README.zh.md)

## Summary

The storage group gives a composition durable storage for everything that is not a session event log: workspace records, session sidecars, and other host-side application data. With it, host packages can persist typed records through a schema-validated domain form, choose between a human-readable JSON backend and a point-update SQLite backend, and receive a change event after every durable write. The family is optional and host-side only: it registers no tools, injects no prompts, and writes no session events, so the model and the agent loop never see it. Use it when the product keeps application state that must survive restarts; a composition with no such data can omit the whole group.

## Table of Contents

- [Packages](#packages)
- [Related documentation](#related-documentation)
- [Dev Note](#dev-note)

-----

<a id="packages"></a>
## Packages

| Package | Role | ctx key |
|---|---|---|
| [`storage`](storage/README.md) | Connects registered backends with mounted data-form facilities | `ctx.storage` |
| [`storage-json`](storage-json/README.md) | Stores each unit as one human-readable JSON file | registers backend `json` |
| [`storage-sqlite`](storage-sqlite/README.md) | Stores units as JSON documents in one SQLite database | registers backend `sqlite` |
| [`storage-domain`](storage-domain/README.md) | Provides schema-validated, change-emitting KV domains over routed backends | `ctx.storageDomain` |

-----

<a id="related-documentation"></a>
## Related documentation

- [Storage subsystem](../../docs/subsystems/storage.md) — the authoritative contract: the backend contract, domain declaration, change events, and generated API.
- [domain KV storage Agent Note](../../.agents/notes/proposed/architecture/2026-07-24-domain-kv-storage-and-workspace.md) — the design behind the family, the workspace consumer, and the deferred session-backend migration.
- [Workspace subsystem](../../docs/subsystems/workspace.md) — the first consumer of the domain data form.

-----

<a id="dev-note"></a>
## Dev Note

<details>
<summary>Working context for maintainers — click to expand</summary>

The design Agent Note is still marked proposed while the family ships; its out-of-scope table is the deferred-work list for the migration phase (the `log` facet, session-backend reuse, cross-process change push). Promote decisions into implemented notes as they land.

</details>
