---
description: "The default executor-less, UI-less agent spine as one Cordis bundle plugin: the fixed service set, concrete loop, and model-facing consumers an app package composes by adding an entry point and backends."
kind: "package-reference"
---

# @deepseek-ai/dsh-agent-spine-demo

English | [中文](README.zh.md)

## Summary

`dsh-agent-spine-demo` gives you a working agent in one plugin: mount it, add an LLM adapter and an executor, and you can run a full agent conversation — in-memory sessions with automatic titles, a system prompt with your persona and workspace instructions, tools for bash, skills, and background jobs, and a loop that runs turns with retries. You configure it in user terms: persona, tool order, workspace-context budget, which agents to pre-create, optional persisted goals, and background-job limits. It ships no UI, executor, or persistence backend, and it adds no prompts or tool schemas of its own — the model sees only what your configuration produces. Use it when you are building a headless, ACP, or JSON-RPC agent and want the common agent machinery without building it yourself. Read this package for what you get out of the box and what you must supply.

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

Use this bundle when you are building an agent without a UI and want the common machinery handled for you: sessions, prompts, tools, and the turn loop. The common path is explicit — mount the bundle with a workspace-context budget, add an LLM adapter and a bash executor as sibling plugins, and provide `agents` when your entry point pre-creates them. When everything is configured, your agent accepts a prompt and returns a completed answer while the process keeps its session in memory.

### What the bundle includes

Out of the box you get: in-memory sessions with automatic fallback titles; a system prompt assembled from your persona, the Harness identity, and optional workspace instructions; model-facing tools for bash, local skills, and background jobs; optional persisted goals the agent can create and track; and a loop that runs turns with provider-routed retries. Add a session-persistence provider when sessions must survive process exit. The full plugin list lives in the implementation section below.

### What stays outside the bundle

These pieces are yours to supply; the bundle leaves them out so each entry point can pick its own:

- **The LLM adapter** — you add the provider that calls a model; pick `llm-deepseek`, `llm-pi-ai`, or `llm-replay`.
- **Model-backed session-title providers** — you get deterministic fallback titles out of the box; add at most one model-based titling provider if you want smarter titles.
- **The bash executor** — the bundle provides the bash tool; you add the executor that runs commands (`bash-local` or a sandboxed implementation).
- **Non-local skill providers** — local skills load out of the box; add providers for embedded or remote catalogs if you need them.
- **The entry point and per-app infrastructure** — headless, ACP, and JSON-RPC apps own transport and output choices; you pick the one that fits your deployment.

### Minimal configuration

The smallest working setup mounts the bundle with a workspace-context budget, plus an LLM adapter and an executor:

```yaml
- name: '@deepseek-ai/dsh-agent-spine-demo'
  config:
    workspaceContext:
      maxBytes: 4096
- name: '@deepseek-ai/dsh-llm-deepseek'   # concrete adapter for ctx.llm
- name: '@deepseek-ai/dsh-bash-local'     # executor for ctx.shell
```

`workspaceContext` is the one required field: give it a byte budget so workspace files load into the agent's context, or set `false` for hermetic prompts. `agents` defaults to none, so pass the agents you want running — or omit it when your entry point creates them on demand (the ACP app does this). You know the setup works when the agent answers a first prompt and its session is saved.

| Field | Default | What it configures |
|---|---|---|
| `agents` | `[]` | which agents your entry point pre-creates; omit to create them on demand |
| `maxParallelToolCalls` | agent-loop default | how many tool calls may run at once; `1` is serial |
| `includeHarnessIdentity` | `true` | whether the system prompt names the DeepSeek Harness identity |
| `includeRuntimeContext` | `true` | whether the agent's history includes dynamic runtime-context snapshots |
| `persona` | `''` | the deployment persona text in the system prompt |
| `toolOrder` | lexicographic | the order the model sees tools in |
| `tools` | `{ mode: 'native' }` | how tools reach the model: native schemas, PTC mode, or both |
| `dshHome` | `$DSH_HOME` or `~/.dsh` | the harness home used for the bash environment and local skill folders |
| `sessionTitle` | example limits | fallback title limits: 5 words, 40 fallback bytes, 80 accepted bytes |
| `workspaceContext` | required | byte budget for loading workspace files into context, or `false` |
| `skills` | enabled | whether local skills load and the skill tool is available |
| `toolBash` | mounted | the bash tool; set `false` when another plugin owns `bash` |
| `jobs` | owner default | how many background jobs each owner may run at once |
| `toolJobs` | mounted | the background-job control tools; `false` keeps jobs without the tools |
| `invariants` | owner default | developer setting: which package checks run and which packages are filtered |
| `goals` | unmounted | optional persisted goals the agent can create and track across turns |

The generated [configuration catalog](../../../docs/config-catalog.md#deepseek-aidsh-agent-spine-demo) is the exhaustive source for every accepted field and its source declaration.

### Request retry and billing

Provider requests are retried automatically: if a call fails, the agent tries again in a new numbered step. Retries can cost you — each attempt may be billed, and `always` mode retries without limit — so usage accounting stays at your entry point. The retry itself does not clutter the conversation; you see the successful result.

-----

<a id="understand-the-implementation"></a>
## Understand the implementation

<details>
<summary>Implementation internals — click to expand</summary>

This section explains how the bundle composes the spine and points at the code that realizes it; the observable behavior is fully covered in [Use this package](#use-this-package).

### Composition model

`apply(ctx, config)` mounts each child plugin under the bundle fiber and forwards every config field to the child that owns it. Cordis pends each fiber on its `inject` declarations until the services it needs exist, so load order is irrelevant to correctness; the listing below mirrors dependency layering for readability. `pickSpineConfig()` copies only the bundle-owned fields from an app config, so entry-point settings never leak into the spine; conflicting `dshHome` values fail during composition because local skills and the managed bash environment must share one harness home. The retry policy keeps retry status, provider errors, and failed partial chunks outside model history, and it reconstructs a retried request with its prior prefix intact so provider caches stay reusable.

### The tree it loads

```text
@deepseek-ai/cordis-plugin-timer      timer service (writes nothing to stdout)
@deepseek-ai/dsh-llm                  abstract LLM service + content-block vocabulary
@deepseek-ai/dsh-session              event-sourced session log + store
@deepseek-ai/dsh-session-title        log-backed title service + deterministic fallback
@deepseek-ai/dsh-system-prompt        prompt-section + tool-schema assembly
@deepseek-ai/dsh-tools                registry + guarded pre/around/post/final-result pipeline
@deepseek-ai/dsh-skill                skill provider registry
@deepseek-ai/dsh-skill-filesystem     local filesystem skill provider
@deepseek-ai/dsh-agent                agent registry + initiator scope + agent/* events
@deepseek-ai/dsh-goal                 optional persisted same-session goal domain
@deepseek-ai/dsh-tool-goal            optional model-facing goal controls
@deepseek-ai/dsh-goal-round-driver    optional same-session goal-round driver
@deepseek-ai/dsh-llm-retry            provider-routed request retry policy
@deepseek-ai/dsh-jobs-local           generic background-job registry
@deepseek-ai/dsh-invariants           configurable invariant registry service
@deepseek-ai/dsh-session/invariant
@deepseek-ai/dsh-agent/invariant
@deepseek-ai/dsh-scope/invariant
@deepseek-ai/dsh-agent-loop/invariant package-owned relational checks
@deepseek-ai/dsh-shell-env            managed DSH_* shell environment for model shell calls (unless toolBash=false)
@deepseek-ai/dsh-tool-bash            the model-facing bash schema (unless toolBash=false)
@deepseek-ai/dsh-agent-instructions   AGENTS.md/CLAUDE.md workspace context loader
@deepseek-ai/dsh-tool-skill           session-prefix skill catalog + model-facing loader schema
@deepseek-ai/dsh-tool-jobs            job_output/job_list/job_kill schemas + completion notices
@deepseek-ai/dsh-agent-loop           THE concrete loop (gets the forwarded `agents`)
```

### Why a code bundle, not a shared YAML include

A YAML include can deduplicate config but cannot own a bin or provide entry-point defaults; the ACP app package makes protocol-pure stdout wiring the default, though a leaf can still add an unsafe logger. Bundle children register services in the root isolate-keyed store, so injected leaf siblings see them without load-order coupling.

### Invariant companions

The bundle mounts the invariant registry and its four package companions (`session`, `agent`, `scope`, `agent-loop`). `invariants.enabled: false` or package filters suppress the checks but do not remove the service or companion registrations; Session's always-on validation and freezing are separate. The package's own companion ([`src/invariant.ts`](src/invariant.ts)) installs no runtime invariant because this composition package owns no independent event stream or mutable data.

### Source map

| File | Role |
|---|---|
| [`src/index.ts`](src/index.ts) | Plugin entry: `Config` schema, `pickSpineConfig()`, `apply()` mounting every child |
| [`src/invariant.ts`](src/invariant.ts) | Invariant companion (no runtime invariant; composition wiring is covered by tests) |

</details>

-----

<a id="further-exploration"></a>
## Further Exploration

Read these pages when the package-level contract is not enough. They move from the sibling demo apps to the subsystems this spine mounts and the exhaustive configuration.

- [Examples group map](../README.md) — sibling demo bundles and how the runnable leaves consume them.
- [ACP application bundle](../../bundle/acp-app/README.md) — the `dsh --profile acp` application that composes this spine without pre-created agents.
- [SDK application bundle](../../bundle/sdk-app/README.md) — the `dsh --profile sdk` application that composes this spine for JSON-RPC clients.
- [Core subsystem](../../../docs/subsystems/core.md) — the services this spine mounts and the agent-loop contract.
- [Generated configuration catalog](../../../docs/config-catalog.md#deepseek-aidsh-agent-spine-demo) — every accepted config field and its source declaration.
- [Service Definition / Service Provider / Consumer separation](../../../.agents/notes/implemented/architecture/2026-06-13-capability-seams.md) — why the bundle owns the shared spine while leaves own the backends.

-----

<a id="model-experience"></a>
## Model Experience

Indirectly, through the model-facing child plugins the bundle mounts — `dsh-system-prompt`, `dsh-tools`, `dsh-tool-skill`, `dsh-tool-bash`, `dsh-tool-jobs`, and `dsh-llm-retry`, plus `dsh-tool-goal` and the goal-round driver's prompts when `goals` is enabled; the bundle adds no model-bound wrapper content of its own.

#### KV Cache effect

The bundle adds no request-prefix content of its own; provider cache reuse depends on the mounted consumers' contributions, and `dsh-llm-retry` reconstructs a retried request with its prior prefix intact.

## Known Limitations and Deferred Work

<a id="known-limitations-and-deferred-work"></a>


These limits define when the bundle is a poor fit or needs special operational care. They are current package constraints, not a comparison with other composition approaches or a task backlog.

- **Most of the spine set is fixed in code** — `apply()` always mounts the core services; config can omit bundled goals, skills, bash, and task-control tools, but swapping the loop or dropping another spine member means composing a different bundle.
- **The invariant service and companions remain fixed members** — `invariants.enabled: false` or package filters suppress checks but do not remove the service or companion registrations; Session's always-on validation and freezing are separate.
- **Retry can duplicate provider billing** — each provider attempt may incur billing, and `always` mode has no attempt limit; usage accounting stays with the entry point.

<a id="dev-note"></a>
### Dev Note

<details>
<summary>Working context for maintainers — click to expand</summary>

This Dev Note is working context for maintainers; it is explicitly non-authoritative — shipped behavior, limits, and accepted rationale live in the sections above and the package code.

The fallback title limits (5 words, 40 fallback bytes, 80 accepted-title bytes) are an overridable example policy owned by this bundle rather than by `dsh-session-title`; an entry point that needs different bounds passes its own `sessionTitle` config. The `workspaceContext` field is required (not defaulted) because it changes model-visible input; keep that requirement if the field is ever re-shaped.

</details>
