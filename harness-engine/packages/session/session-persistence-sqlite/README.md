---
description: "SQLite session persistence for deployments and maintainers choosing, configuring, or debugging the opt-in packed-row backend."
kind: "package-reference"
---

# @deepseek-ai/dsh-session-persistence-sqlite

English | [中文](README.zh.md)

## Summary

`dsh-session-persistence-sqlite` keeps every session's durable history in a single SQLite database: sessions survive restarts, and the deployment's whole history becomes one queryable file you can back up, inspect with SQL, and analyze — instead of one artifact per session. Choosing it changes nothing for the agent loop, the model, or replay, because it serves the same logical `SessionEvent` stream as the JSONL backend; packing, compression, and recovery are storage-internal details. Choose it when a single queryable database fits the deployment; no shipped composition enables it by default. It is a pre-release provider: it rejects database files it does not own instead of migrating them, and its synchronous Node SQLite driver blocks the JavaScript thread during reads and writes. Setup, sizing, and migration guidance come first; the implementation internals live in a collapsible developer section below.

## Table of Contents

- [Use this package](#use-this-package)
- [Understand the implementation](#understand-the-implementation)
- [Further Exploration](#further-exploration)
- [Model Experience](#model-experience)
- [Known Limitations and Deferred Work](#known-limitations-and-deferred-work)
- [Dev Note](#dev-note)

-----

<a id="use-this-package"></a>
## Use this package

Mount this provider when a composition needs durable sessions backed by SQLite and accepts a process-local, synchronous database driver. The common path is explicit: load the session service, mount the provider, and give it a database path.

### When to choose it

Choose this backend when a local deployment benefits from one queryable database instead of many per-session files. Choose the JSONL backend when consumers need a per-session artifact: this provider returns `undefined` from `locate(meta)`, supports no raw artifacts, and exposes no per-session file. Account for synchronous SQLite and compression work before adopting it for a high-concurrency service.

### Disk footprint and performance

The packed layout exchanges some SQLite-local latency for a smaller queryable database. The available 501-session comparison measures schema 19 rather than schema 20; that layout used 233.18 MB against the SQLite comparison baseline's 438.31 MB and compressed JSONL's 148.15 MB. Full writes were about 2.3× faster than JSONL and suffix reads remained much faster; complete reads and forks were slightly slower than JSONL. The [persistence latency and page-size decision](../../../.agents/notes/implemented/architecture/2026-08-25-persistence-latency-and-page-size.md) owns the method, complete metrics, and accepted trade-offs.

The disk cost buys a structured, queryable view of session history: external tooling can analyze `sessions` and `events` with SQL, decoding physical rows the way this provider does — the groundwork for features such as built-in full-text search.

### Minimal configuration

Load the session service first, then mount the provider with a database path. Use an absolute path when the location must not depend on the process working directory; relative paths resolve from that directory. `:memory:` is valid for an in-process database whose contents disappear with the process.

```yaml
- name: '@deepseek-ai/dsh-session'
- name: '@deepseek-ai/dsh-session-persistence-sqlite'
  config:
    path: /absolute/path/to/sessions.db
```

| Field | Default | Meaning |
|---|---|---|
| `path` | required | SQLite database path, or `:memory:` |
| `journalMode` | `wal` | Durable journal mode: `wal`, `delete`, `truncate`, or `persist` |
| `busyTimeoutMs` | `5,000` | Maximum synchronous wait for another connection's lock |
| `preparedSessionCacheSize` | `5` | Cold session preparations retained for resume reuse |
| `writeBatchMaxDelayMs` | `200` | Fixed live-event coalescing window, in milliseconds |

The generated [configuration catalog](../../../docs/config-catalog.md#deepseek-aidsh-session-persistence-sqlite) is the exhaustive source for every accepted field and its JSDoc.

### Migrating existing JSONL sessions

There is no built-in migration tool: the JSONL and SQLite stores are separate, and nothing copies sessions between them. Because both backends implement the same logical contract, you can carry a session over with the persistence API — read on the JSONL side, write on the SQLite side. One backend serves `ctx.sessionPersistence` per composition, so run the two halves as separate runs or processes:

```text
// Export — run against the JSONL composition, per session id:
const { meta, events } = await ctx.sessionPersistence.load(id)

// Import — run against the SQLite composition, per exported session:
await ctx.sessionPersistence.create(meta)
await ctx.sessionPersistence.append(id, events)
```

`list()` enumerates the materialized sessions to export. The exported events keep contiguous `seq` values starting at 0, so `append` accepts them as one ordered batch into a fresh session; `load` also commits any needed cold repair on the source first, so the exported log is balanced. Treat the migration as a one-time cutover: verify that the imported sessions load, then switch the composition to the SQLite provider. Continuing to write through the old JSONL root afterwards would let the two stores diverge.

### Startup and safe operation

A fresh database initializes directly at schema version 20 with 64 KiB pages. Existing files are never retuned: databases with any other version, a foreign application identity, an unversioned non-pristine schema, or unexpected schema objects are rejected before any data is exposed or changed. This pre-release provider ships no migration. Every statement and fixed pragma comes from packaged `.sql` resources in `resources/sql/`, and runtime values are bound as SQLite parameters, so package code never assembles query text.

Each connection disables SQLite trusted schemas and memory-mapped I/O, verifies the requested journal mode, and pins `synchronous=FULL` so a resolved append remains durable across an OS crash or power loss. On POSIX, the database parent directory and file must belong to the current user, the parent must not be group/world-writable, and the file must grant no group or world permissions; Windows additionally rejects symbolic links and non-regular files, while ACL restriction stays the deployment's job. Path and ownership failures reject plugin initialization; Node's SQLite driver loads lazily on the first persistence operation. Ordinary `create` stays lazy until the first append, while `ensureMaterialized` writes a session metadata row with no event rows.

-----

<a id="understand-the-implementation"></a>
## Understand the implementation

<details>
<summary>Implementation internals — click to expand</summary>

This section explains the design decisions behind the provider and points at the code that realizes them; the observable behavior is fully covered in [Use this package](#use-this-package).

### Design philosophy

The provider is built on one separation and three commitments:

- **Logical contract, physical format.** Callers always read and write ordinary `SessionEvent[]`; how rows are packed, stored, and compressed is private to this package.
- **The schema owns the format.** Schema 20 is a frozen physical contract: a database at another version, with a foreign identity, or with unexpected schema objects is rejected, never migrated. Changing the schema, row codec, page size, or dictionary bytes requires a new schema version.
- **Durability is the default.** Appends run in immediate transactions with `synchronous=FULL`, and a resolved `append()` means the batch is durable. Normal appends are insert-only: earlier event rows are never rewritten.
- **Efficiency within strict bounds.** Packing and compression keep the database small, but every limit is a hard format bound — at most 1,024 events and 1 MiB of payload per packed row.

The packed-row foundation lives in the [SQLite physical chunk-row decision](../../../.agents/notes/implemented/architecture/2026-08-18-sqlite-physical-chunk-row-compression.md); the current compression, key, and page-size choices live in the [persistence latency and page-size decision](../../../.agents/notes/implemented/architecture/2026-08-25-persistence-latency-and-page-size.md).

### Source map

| File | Role |
|---|---|
| [`src/index.ts`](src/index.ts) | Plugin entry: `Config` schema, service registration, coordinator wiring |
| [`src/store.ts`](src/store.ts) | Storage primitives: transactional append, reads, repair, path and ownership validation |
| [`src/schema.ts`](src/schema.ts) | Schema ownership: version gate, connection hardening, row decoding |
| [`src/codec.ts`](src/codec.ts) | Packing: which `assistant/chunk` runs become packed rows, size bounds |
| [`src/compression.ts`](src/compression.ts) | Physical encoding: dictionary compression, sequence lists, row scan and decode |
| [`src/sql.ts`](src/sql.ts) + [`resources/sql/`](resources/sql/) | Every SQL statement as a packaged, closed-name resource |
| [`src/invariant.ts`](src/invariant.ts) | Invariant companion (no runtime invariant; packing is observable only by database round-trip) |

### Database schema

A fresh database contains three strict tables, defined in [`resources/sql/schema.sql`](resources/sql/schema.sql):

| Table | Purpose |
|---|---|
| `persistence_state` | One-row store identity |
| `sessions` | One row per session: header fields plus a monotonic revision |
| `events` | Physical event rows: one logical event, or one packed run |

The exact columns live in [`resources/sql/schema.sql`](resources/sql/schema.sql). `sessions.id` is an internal integer key while `sessions.session_key` retains the public session id. `events.data` holds text or an independently decodable Zstandard blob; compression uses the schema-owned shared dictionary only when the result is smaller. `events.source_event_seqs` uses tagged delta or run encoding. `events.ignorable` is `0` for a packed chunk run, `1` for a scalar logical event carrying `ignorable: true`, and `NULL` for every other scalar event, so a scalar event whose type matches a physical chunk tag remains unambiguous. Packed rows reuse the `seq` of their first logical event, so under the composite `(session_id, seq)` primary key physical order is logical order.

### Write path

Each append takes an immediate transaction, re-validates schema ownership, checks the stored tail so a stale writer cannot extend the log, packs only the new batch, inserts its rows, bumps the session revision once, and commits. The coordinator coalesces live events for the configured window, so high-frequency streams produce larger packed runs while physical writes stay proportional to newly durable batches.

### Read and recovery

A full read locates the last valid `turn/end` in a reverse pass, then decodes each physical row into its logical events in forward order, rejecting gaps or malformed rows in the committed prefix. A malformed final row is treated as a torn tail: a mutating load may delete it under the write lock and close the log with synthetic closers. Suffix reads (`readFrom`) examine only the physical span that may contain the requested sequence, so they never parse unrelated earlier rows.

</details>

-----

<a id="further-exploration"></a>
## Further Exploration

Read these pages when the package-level contract is not enough. They move from the shared persistence model to exhaustive configuration and the decision evidence behind the physical layout.

- [Session persistence subsystem](../../../docs/subsystems/persistence.md) — backend-neutral service semantics and provider relationships.
- [Session package map](../README.md) — adjacent persistence, projection, title, and telemetry packages.
- [Generated configuration catalog](../../../docs/config-catalog.md#deepseek-aidsh-session-persistence-sqlite) — every accepted config field and its source declaration.
- [SQLite physical chunk-row decision](../../../.agents/notes/implemented/architecture/2026-08-18-sqlite-physical-chunk-row-compression.md) — rationale, alternatives, and measurements behind the packed layout.
- [Persistence latency and page-size decision](../../../.agents/notes/implemented/architecture/2026-08-25-persistence-latency-and-page-size.md) — the 501-session benchmark and current storage trade-offs.

-----

<a id="model-experience"></a>
## Model Experience

### Resumed conversation history

#### What the model sees

Nothing specific to SQLite. Resume restores the same logical events and derived messages as the JSONL backend; physical packed tags never reach prompts, tools, replay, or live `session/event` delivery.

#### Token effect

Zero live-request tokens. Resume pays only for the retained logical history and the current request envelope.

#### KV Cache effect

Physical packing does not mutate request prefixes. Provider cache reuse depends on the reconstructed history, current envelope, and model route exactly as with other persistence backends.

## Known Limitations and Deferred Work

<a id="known-limitations-and-deferred-work"></a>


These limits define when the provider is a poor fit or needs special operational care. They are current package constraints, not a general SQLite comparison or a task backlog.

- **Pre-release design with no migration** — schema 20 is an interim SQLite-only design; neither schema stability nor migration support is guaranteed.
- **Packing depends on batch boundaries** — a compatible run split by the write-behind window or an explicit flush stays split across physical rows; this avoids rewriting prior rows at the cost of a timing-dependent packing ratio.
- **Synchronous SQLite and compression** — Node's SQLite driver and Zstandard calls block the JavaScript thread.
- **Busy waits block the event loop** — SQLite waits inside synchronous calls; a competing writer can stall the thread for up to the configured `busyTimeoutMs`.
- **External SQL readers must decode physical rows** — a packed `events.type` (`text-chunks`, `reasoning-chunks`, `tool-call-chunks`) is not a logical event type; supported consumers read through this provider.
- **No deletion or historical compaction** — normal appends are insert-only and nothing removes old rows.

<a id="dev-note"></a>
### Dev Note

<details>
<summary>Working context for maintainers — click to expand</summary>

The 501-session corpus contains private session data and is not committed. Its aggregate method, complete results, and rejected candidates are recorded in the [persistence latency and page-size decision](../../../.agents/notes/implemented/architecture/2026-08-25-persistence-latency-and-page-size.md); the packaged dictionary's hash-pinned resource is part of the schema-20 source of truth.

</details>
