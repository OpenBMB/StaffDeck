---
description: "The examples package group: reusable agent-spine composition bundles for tests and custom deployments."
kind: "package-group"
---

# examples/ — reusable composition bundles

English | [中文](README.zh.md)

## Summary

The examples group provides the reusable agent spine for tests and custom deployments that need a concrete composition without assembling it by hand. Its `-demo` npm suffix marks it as support infrastructure rather than a product interface. ACP, SDK, and one-shot applications launch through the `acp`, `sdk` or `sdk-minimal`, and `headless` profiles. This group contains no application entrypoint.

## Table of Contents

- [Packages](#packages)
- [Related documentation](#related-documentation)
- [Dev Note](#dev-note)

-----

<a id="packages"></a>
## Packages

| Package | npm name | Role |
|---|---|---|
| [`agent-spine-demo/`](agent-spine-demo/README.md) | `@deepseek-ai/dsh-agent-spine-demo` | Working agent core you mount and configure with your own LLM and executor |

`agent-spine-demo` is the shared agent core. Product application assemblies live under [`bundle/`](../bundle/README.md); this support package remains available to focused tests and custom compositions.

-----

<a id="related-documentation"></a>
## Related documentation

- [ACP application bundle](../bundle/acp-app/README.md) — the `dsh --profile acp` application for programmatic clients.
- [SDK application bundle](../bundle/sdk-app/README.md) — the `dsh --profile sdk` application for JSON-RPC clients.
- [Minimal SDK bundle](../bundle/sdk-minimal/README.md) — the standalone two-tool SDK profile used by the Python example.

<a id="dev-note"></a>
## Dev Note

None.
