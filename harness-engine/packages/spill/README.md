---
description: "Package map for the tool-output spill capability family: what the storage service, the local backend, and the result policy each provide."
kind: "package-group"
---

# spill/ — tool-output spill capability family

English | [中文](README.zh.md)

## Summary

The `spill/` group keeps oversized tool output out of the model's context without losing it: when a tool result exceeds a deployment's byte cap, the full text is saved to a spill artifact and the model sees a bounded preview plus a locator it can read or search later. The family splits into three packages — the storage service in `spill/`, the local filesystem backend in `spill-local/`, and the result policy in `spill-policy/` that decides when a final tool result is too large. Spilling is opt-in and best-effort: the policy acts only when `maxInlineBytes` is configured, and a storage failure leaves the original result visible. The group owns storage and result replacement only; preview mechanics live in `dsh-output-retention`, and provider resource caps remain separate.

## Table of Contents

- [Packages](#packages)
- [Related documentation](#related-documentation)
- [Dev Note](#dev-note)

-----

<a id="packages"></a>
## Packages

Three packages play the spill roles; the subsystem reference owns the exhaustive vocabulary and contracts.

| Package | Role | ctx key |
|---|---|---|
| [`spill/`](spill/README.md) | Storage service: saves oversized tool text and returns a locator plus retrieval guidance | `ctx.spillStore` |
| [`spill-local/`](spill-local/README.md) | Saves spilled text to private session-scoped files on this machine | registers on `ctx.spillStore` |
| [`spill-policy/`](spill-policy/README.md) | Replaces oversized plain-text tool results with a preview and locator | listens on `ctx.tools` |

-----

<a id="related-documentation"></a>
## Related documentation

Start with the subsystem reference for the shared vocabulary, then the design decision and its durable-log extension.

- [Spill subsystem](../../docs/subsystems/spill.md) — the `SaveTextSpill`/`SpillRef` vocabulary, ownership, and backend relationships.
- [Tool output spill decision](../../.agents/notes/implemented/architecture/2026-07-08-tool-output-spill-files.md) — the capability boundary between storage, retention, and tool-owned output handling.
- [Code dispatch-log spill decision](../../.agents/notes/implemented/feature/2026-07-26-ptc-dispatch-log-spill.md) — why the durable copy of `run_code` sub-call results is bounded too.

<a id="dev-note"></a>
## Dev Note

<details>
<summary>Working context for maintainers — click to expand</summary>

None.

</details>
