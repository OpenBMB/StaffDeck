---
description: "Package map for verified external events, programmatic rules, and fire-and-forget DSH Session creation."
kind: "package-group"
---

# webhook/ — verified external events to DSH Sessions

English | [中文](README.zh.md)

## Summary

The Webhook family receives authenticated provider events and runs trusted programmatic rules. A rule can create an ordinary root Session inside a Web Workspace. Dispatch is process-local and fire-and-forget, with no delivery database, queue, retry, deduplication, or Agent-completion state.

## Table of Contents

- [Packages](#packages)
- [Related documentation](#related-documentation)
- [Dev Note](#dev-note)

-----

<a id="packages"></a>
## Packages

| Package | Role | ctx key |
|---|---|---|
| [`webhook/`](webhook/README.md) | Rule registry, callback lifecycle, and Workspace-backed Session creation | `ctx.webhookRuntime` |
| [`webhook-github/`](webhook-github/README.md) | Signed GitHub HTTP adapter | consumes `ctx.webhookRuntime` and `ctx.webServer` |

<a id="related-documentation"></a>
## Related documentation

Provider adapters authenticate and normalize deliveries. Rules own arbitrary conditions and external calls, then return `null` or one Session request. The [Webhook subsystem reference](../../docs/subsystems/webhook.md) owns the shared types and timing guarantees.

<a id="dev-note"></a>
## Dev Note

None.
