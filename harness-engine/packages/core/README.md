---
description: "The core group map: the session log, system-prompt assembly, tool registry, agent vocabulary, and default loop that form the product API spine."
kind: "package-group"
---

# packages/core

English | [中文](README.zh.md)

## Summary

The core group provides the product API spine of the DeepSeek Harness: an append-only session log, system-prompt assembly, a tool registry, the `Agent` handle, and the concrete loop that drives them. Every composition boots these packages, and plugins and consumers build against their stable contracts. A turn flows through all of them — the loop claims a prompt, opens a turn on the session log, assembles the request through system-prompt, streams the model response, dispatches tool calls through the registry, and appends every model-visible fact back to the log. Choose this group when you build an agent or extend one; the runnable default composition is [`examples/agent-spine-demo`](../examples/agent-spine-demo/README.md).

## Table of Contents

- [Packages](#packages)
- [Related documentation](#related-documentation)
- [Dev Note](#dev-note)

-----

<a id="packages"></a>
## Packages

| Package | Role | ctx key |
|---|---|---|
| [`scope/`](scope/README.md) | Scoped registration and event routing that isolate one agent's contributions | library — no ctx key |
| [`session/`](session/README.md) | The append-only session event log every agent's history derives from | `ctx.sessions` |
| [`system-prompt/`](system-prompt/README.md) | System-prompt assembly from ordered sections, tool schemas, and variables | `ctx.systemPrompt` |
| [`tools/`](tools/README.md) | The tool registry and guarded execution pipeline the loop dispatches through | `ctx.tools` |
| [`agent-tool-presentation/`](agent-tool-presentation/README.md) | Per-agent tool-presentation selector for presets | no ctx key |
| [`agent/`](agent/README.md) | The `Agent` handle plugins program against, plus its live registry and events | `ctx.agents` |
| [`agent-default-model/`](agent-default-model/README.md) | The deployment default model selection entry points apply to fresh agents | `ctx.agentDefaultModel` |
| [`agent-loop/`](agent-loop/README.md) | The default agent driver: creates agents and runs the turn and step lifecycle | `ctx.agentLoop` |

`scope` supplies the shared scoping primitive; `agent` owns the public `Agent` contract, while `agent-loop` is its default implementation, so extension plugins depend on `agent` and the driver stays swappable. `agent-default-model` owns the deployment selection an entry point applies when a session has none of its own. Runnable compositions live in [`examples/agent-spine-demo`](../examples/agent-spine-demo/README.md); this group owns only the swappable spine pieces.

-----

<a id="related-documentation"></a>
## Related documentation

- [Core subsystem](../../docs/subsystems/core.md) — the package-by-package loop map and the `Agent` handle contracts.
- [Session subsystem](../../docs/subsystems/session.md) — the session event vocabulary and derived history.
- [System-prompt subsystem](../../docs/subsystems/system-prompt.md) — prompt section, dynamic context, and tool-schema types.
- [Tools subsystem](../../docs/subsystems/tools.md) — the tool execution pipeline and presentation vocabulary.
- [Scoped registration subsystem](../../docs/subsystems/scope.md) — the scoped-layer primitive these registries build on.
- [Architecture](../../docs/architecture.md) — the turn flow and where new behavior goes.
- [agent-spine-demo](../examples/agent-spine-demo/README.md) — the default runnable composition.

-----

<a id="dev-note"></a>
## Dev Note

<details>
<summary>Working context for maintainers — click to expand</summary>

None.

</details>
