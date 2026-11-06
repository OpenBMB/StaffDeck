---
description: "fd-3 wire protocol between a Node host and a CPython subprocess for users and maintainers building or debugging the Python code-execution backend."
kind: "package-library"
---

# @deepseek-ai/dsh-code-runtime-python

English | [中文](README.zh.md)

## Summary

`dsh-code-runtime-python` owns the versionless wire protocol between a Node host and a CPython subprocess for the [`dsh-code-runtime`](../code-runtime/README.md) seam: one JSON object per line on the child's fd 3, leaving stdout/stderr free for the program's own output. The package ships the host-side frame codec and hostile-frame validators (`src/protocol.ts`) plus the Python-side mirror of the same message vocabulary (`py/protocol.py`), so every consumer of the wire shares one vocabulary. It is the protocol layer for the Python backend — the package carries no subprocess execution path, so nothing here spawns `python3` outside the cross-language mirror test. The host treats every inbound frame as hostile, because model code has full access to fd 3 and can post anything through it.

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

Choose this package when you build or consume the CPython code-runtime wire: implement the Python backend or the host that drives it, or debug a Python code run's framing. The package is the wire protocol intended for a CPython code-runtime provider — such a provider runs each model program in a fresh `python3 -I` subprocess — and this package supplies the protocol both sides speak, so its exports are the single TS-side source of truth for the wire.

### What you get

The package re-exports the host-side protocol vocabulary from `src/index.ts`: `validateChildFrame` (rebuilds every inbound frame before the host reads it), the lossless-JSON codec and meters (`encodeJsonPlain`, `checkDoneValue`, `hasUnsafeIntegerToken`, `hasNonLosslessNumber`), and `logTruncationMarker` (the shared truncation-marker text). The Python side mirrors the message shapes as `TypedDict`s in `py/protocol.py` and re-declares the two surfaces both sides execute against — `PROTOCOL_FD = 3` and the marker text.

### The wire

Frames travel on the child's fd 3 as JSON-lines — one object per line — so stdout/stderr stay clear for the program's own output. Child → host: `boot-ack`, `call`, `log`, `done`. Host → child: `boot` (first frame, carrying every cap and the namespace declarations), `run` (after `boot-ack`, carrying only the program body), and one `reply` per `call`. A forged frame can carry both `value` and `error` on `done`, so a consumer must check `error` first and ignore `value` when it is set.

### What can go wrong

Host-side validation drops junk without throwing, so a malformed or forged frame never crashes the host process: `validateChildFrame` returns `undefined` for anything that does not rebuild cleanly, a non-number call id can never be echoed into a reply, and forged extra fields never ride along. A completion value that is not lossless JSON, or that exceeds the configured byte budget, is rejected explicitly (`non-lossless` / `over-budget`) rather than silently rounded or truncated.

-----

<a id="understand-the-implementation"></a>
## Understand the implementation

<details>
<summary>Implementation internals — click to expand</summary>

This section explains the design behind the wire protocol; observable behavior is fully covered in [Use this package](#use-this-package).

### Design concept

The protocol assumes one direction of trust: the host treats every inbound frame as hostile (model code can forge anything on fd 3) and REBUILDS it field by field before reading; the Python side trusts host replies, because the host is not model-controlled. The package is deliberately the protocol layer only — the Python-side JSON codec lives in the backend's bootstrap, not in `py/protocol.py`, so the mirror stays the pure wire-vocabulary counterpart of `src/protocol.ts`.

### Wire contract

The frames are `boot` / `run` (host → child) and `boot-ack` / `call` / `log` / `done` plus one `reply` per call (child → host). The `log` frame's `truncated` flag marks the frame that IS the child ledger's truncation marker, so the host stops capturing at the same point the child did instead of inferring it from its own budget. `done.error.kind` is one of `exception`, `invalid-output`, `output-limit`; wall/CPU budgets, aborts, and substrate death are observed host-side, not carried as frames.

### Lossless JSON crossing

Completion values and binding arguments cross as exact JSON: values serialize without recursion, so a deep payload below the byte budget survives instead of dying on `JSON.stringify`'s stack limit, and integral doubles beyond the safe range cross as exact digits rather than silently rounded tokens; the meters in [`src/protocol.ts`](src/protocol.ts) enforce byte budgets and number losslessness before anything else reads the payload.

### Mirror alignment

`tests/protocol-mirror.e2e.ts` spawns a real `python3` and asserts, against `src/protocol.ts`, both `PROTOCOL_FD` / the truncation-marker text and each `TypedDict`'s required/optional wire field set in `py/protocol.py`, so a renamed or dropped field — or one side making a field optional the other requires — fails the test. Field *types* are not compared across the language boundary; that residue stays with review plus the backend's real-subprocess suite.

### Source map

| File | Role |
|---|---|
| [`src/index.ts`](src/index.ts) | Plugin entry: re-exports the protocol vocabulary for every consumer of the wire |
| [`src/protocol.ts`](src/protocol.ts) | Host side: frame codec, hostile-frame validators, lossless-JSON meters, shared marker text |
| [`py/protocol.py`](py/protocol.py) | Python side: `PROTOCOL_FD`, `TypedDict` frame mirrors, `log_truncation_marker` |
| [`tests/protocol-mirror.e2e.ts`](tests/protocol-mirror.e2e.ts) | Cross-language mirror test against a real `python3` |
| [`src/invariant.ts`](src/invariant.ts) | Invariant companion (no runtime invariant; the package registers no mutable data relation) |

</details>

-----

<a id="further-exploration"></a>
## Further Exploration

Read these when the protocol contract is not enough. They move from the seam definition to the protocol's design record and the companion backend.

- [Code runtime seam](../code-runtime/README.md) — the abstract contract the Python backend implements.
- [fd-3 protocol Agent Note](../../../.agents/notes/implemented/architecture/2026-07-31-code-runtime-python-fd3-protocol.md) — design rationale, wire contract, and the mirror-alignment decision.
- [Worker-thread backend](../code-runtime-worker-thread/README.md) — the shipped TypeScript sibling, the model for the Python backend's behavior.
- [Code runtime subsystem reference](../../../docs/subsystems/code-runtime.md) — request/result vocabulary, bindings, and failure taxonomy.

-----

<a id="model-experience"></a>
## Model Experience

Indirectly, through PTC mode in `dsh-tools`, which renders the program's completion value or failure into a retained `run_code` result.

#### KV Cache effect

No direct invalidation; the named consumer owns any request-prefix changes.

## Known Limitations and Deferred Work

<a id="known-limitations-and-deferred-work"></a>


These limits define what the package does and does not cover; they are current package constraints, not a task backlog.

- **The cross-language guard covers the executed surfaces and the frame field shapes, not the field types** — the mirror e2e compares required/optional field sets, not that `cpuSeconds` is an `int` on both sides; comparing type declarations across TypeScript and Python has no mechanical equivalent here, so a type-level drift is caught by review plus the backend's real-subprocess suite.
- **`src/index.ts` exports the protocol vocabulary only** — the package carries no subprocess execution path and no Python-side JSON codec, so nothing here spawns `python3` outside the mirror test.

<a id="dev-note"></a>
### Dev Note

<details>
<summary>Working context for maintainers — click to expand</summary>

None.

</details>
