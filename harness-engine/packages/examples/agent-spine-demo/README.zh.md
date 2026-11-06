---
description: "默认的无执行器、无 UI agent（智能体）主干，以一个 Cordis 组合包插件交付：应用包只需添加入口与后端，即可组合出固定服务集、具体循环与面向模型的消费方。"
kind: "package-reference"
---

# @deepseek-ai/dsh-agent-spine-demo

[English](README.md) | 中文

## 概述

`dsh-agent-spine-demo` 用一个插件给你一个可工作的 agent（智能体）：挂载它、添加 LLM（大语言模型）适配器与执行器，即可运行完整的 agent 对话——带自动标题的内存会话、包含你的 persona 与工作区指令的系统提示词、bash、skill 与后台任务工具，以及带自动重试的运行轮次循环。你可以用用户术语配置它：persona、工具顺序、工作区上下文预算、预创建哪些 agent、可选持久目标与后台任务上限。它不附带 UI、执行器或持久化后端，且不添加任何自己的提示词或工具 schema——模型只看到你的配置产生的内容。当你构建无头、ACP（Agent Client Protocol）或 JSON-RPC agent、希望复用通用 agent 机制而不必自己搭建时使用它。阅读本包可了解开箱即得的内容，以及需要你自行提供的内容。

## 目录

- [使用本包](#use-this-package)
- [理解实现](#understand-the-implementation)
- [进一步探索](#further-exploration)
- [模型体验](#model-experience)
- [已知限制与延期工作](#known-limitations-and-deferred-work)
- [开发备注](#dev-note)

-----

<a id="use-this-package"></a>
## 使用本包

当你在构建不带 UI 的 agent、希望会话、提示词、工具与轮次循环这些通用机制由本包代劳时使用此组合包。常用路径是显式的——用工作区上下文预算挂载组合包，把 LLM 适配器与 bash 执行器作为同级插件添加，并在入口预创建 agent 时提供 `agents`。配置完成后，你的 agent 就能接受提示词并返回完成的答案，当前进程会把该会话保留在内存中。

### 组合包包含的内容

开箱即得：带自动后备标题的内存会话；由你的 persona、Harness 身份与可选工作区指令组装的系统提示词；bash、本地 skill 与后台任务等面向模型的工具；agent 可以创建与跟踪的可选持久目标；以及带提供方路由重试、运行轮次的循环。会话需要在进程退出后继续存在时，请添加会话持久化提供方。完整插件清单见下方实现章节。

### 留在组合包外的内容

以下部分需要你自行提供；组合包把它们留在外部，让每个入口都能自行选择：

- **LLM 适配器**——你需要添加调用模型的提供方；可选择 `llm-deepseek`、`llm-pi-ai` 或 `llm-replay`。
- **基于模型的会话标题提供方**——开箱即用确定性后备标题；若想要更智能的标题，可以恰好添加一个基于模型的标题提供方。
- **bash 执行器**——组合包提供 bash 工具；你需要添加实际运行命令的执行器（`bash-local` 或沙箱化实现）。
- **非本地 skill 提供方**——本地 skill 开箱即用；需要嵌入式或远程目录时，可以添加相应提供方。
- **入口与各应用基础设施**——无头、ACP 与 JSON-RPC 应用负责传输与输出选择；请选择适合你部署的那一个。

### 最小配置

最小可用配置是：用工作区上下文预算挂载组合包，再加上 LLM 适配器与执行器：

```yaml
- name: '@deepseek-ai/dsh-agent-spine-demo'
  config:
    workspaceContext:
      maxBytes: 4096
- name: '@deepseek-ai/dsh-llm-deepseek'   # concrete adapter for ctx.llm
- name: '@deepseek-ai/dsh-bash-local'     # executor for ctx.shell
```

`workspaceContext` 是唯一必填字段：给它一个字节预算，工作区文件就会加载进 agent 上下文；设 `false` 则得到隔离提示词。`agents` 默认为空，因此请传入你想运行的 agent——或者当你的入口按需创建 agent 时省略它（ACP 应用就是这样）。当 agent 回答第一个提示词且会话被保存时，说明配置成功。

| 字段 | 默认值 | 配置内容 |
|---|---|---|
| `agents` | `[]` | 你的入口预创建的 agent；按需创建时省略 |
| `maxParallelToolCalls` | agent loop 默认值 | 同时运行的工具调用数；`1` 表示串行 |
| `includeHarnessIdentity` | `true` | 系统提示词是否点名 DeepSeek Harness 身份 |
| `includeRuntimeContext` | `true` | agent 历史是否包含动态运行时上下文快照 |
| `persona` | `''` | 系统提示词中的部署 persona 文本 |
| `toolOrder` | 字典序 | 模型看到工具的顺序 |
| `tools` | `{ mode: 'native' }` | 工具如何到达模型：native schema、PTC mode 或两者 |
| `dshHome` | `$DSH_HOME` 或 `~/.dsh` | bash 环境与本地 skill 目录使用的 harness 主目录 |
| `sessionTitle` | 示例限制 | 后备标题限制：5 个词、40 个后备字节、80 个可接受字节 |
| `workspaceContext` | 必填 | 加载工作区文件进上下文的字节预算，或 `false` |
| `skills` | 启用 | 是否加载本地 skill 并提供 skill 工具 |
| `toolBash` | 挂载 | bash 工具；当其他插件拥有 `bash` 时设 `false` |
| `jobs` | 拥有者默认值 | 每个 owner 可同时运行的后台任务数 |
| `toolJobs` | 挂载 | 后台任务控制工具；`false` 保留任务但不提供工具 |
| `invariants` | 拥有者默认值 | 开发者设置：运行哪些包检查、过滤哪些包 |
| `goals` | 不挂载 | agent 可创建并跨轮次跟踪的可选持久目标 |

生成的[配置目录](../../../docs/config-catalog.zh.md#deepseek-aidsh-agent-spine-demo)是每个受支持字段及其源声明的穷尽式真源。

### 请求重试与计费

提供方请求会自动重试：调用失败时，agent 会在新的编号步骤中重试。重试可能产生费用——每次尝试都可能计费，`always` 模式没有尝试次数上限——因此用量核算留在你的入口。重试本身不会弄乱对话；你看到的是成功的结果。

-----

<a id="understand-the-implementation"></a>
## 理解实现

<details>
<summary>实现细节——点击展开</summary>

本节解释组合包如何组装主干，并指出实现它的代码位置；可观察行为已在[使用本包](#use-this-package)中完整说明。

### 组合模型

`apply(ctx, config)` 把每个子插件挂载到组合包 fiber 下，并把每个配置字段转发给拥有它的子插件。Cordis 会按 `inject` 声明挂起每个 fiber，直到其所需服务存在为止，因此加载顺序不影响正确性；下方的清单只是按依赖分层排列，便于阅读。`pickSpineConfig()` 只从应用配置中复制组合包拥有的字段，因此入口设置绝不会泄漏进主干；`dshHome` 值冲突会在组合时失败，因为本地 skill 与受管 bash 环境必须共享同一个 harness 主目录。重试策略让重试状态、提供方错误与失败的部分分片不进入模型历史，并重建保留先前前缀的重试请求，使提供方缓存保持可复用。

### 它加载的插件树

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

### 为何用代码组合包，而非共享 YAML include

YAML include 可以去重配置，却无法拥有 bin 或提供入口默认值；ACP 应用包默认接出协议纯净的 stdout 接线，但叶节点仍可添加不安全的 logger。组合包子节点把服务注册到根 isolate-keyed store，因此叶节点的同级插件无需依赖加载顺序即可通过注入看到它们。

### 不变式配套插件

组合包挂载不变式注册表及其四个包配套插件（`session`、`agent`、`scope`、`agent-loop`）。`invariants.enabled: false` 或包筛选器会抑制检查，但不会移除服务或配套插件注册；Session 始终启用的校验与冻结是另一套机制。本包自己的配套插件（[`src/invariant.ts`](src/invariant.ts)）不安装任何运行时不变式，因为这个组合包不拥有独立事件流或可变数据。

### 源码地图

| 文件 | 职责 |
|---|---|
| [`src/index.ts`](src/index.ts) | 插件入口：`Config` schema、`pickSpineConfig()`、挂载所有子插件的 `apply()` |
| [`src/invariant.ts`](src/invariant.ts) | 不变式配套插件（无运行时不变式；组合接线由测试覆盖） |

</details>

-----

<a id="further-exploration"></a>
## 进一步探索

当包级约定不够用时阅读以下页面。它们从同级演示应用逐步进入本主干挂载的子系统与穷尽式配置。

- [examples 组映射](../README.zh.md)——同级演示组合包，以及可运行叶节点如何消费它们。
- [ACP 应用组合包](../../bundle/acp-app/README.zh.md)——不预创建 agent、组合本主干的 `dsh --profile acp` 应用。
- [SDK 应用组合包](../../bundle/sdk-app/README.zh.md)——为 JSON-RPC 客户端组合本主干的 `dsh --profile sdk` 应用。
- [核心子系统](../../../docs/subsystems/core.zh.md)——本主干挂载的服务与 agent loop 约定。
- [生成配置目录](../../../docs/config-catalog.zh.md#deepseek-aidsh-agent-spine-demo)——每个受支持配置字段及其源声明。
- [Service Definition／Service Provider／Consumer 职责分离](../../../.agents/notes/implemented/architecture/2026-06-13-capability-seams.zh.md)——为何组合包拥有共享主干，而叶节点拥有后端。

-----

<a id="model-experience"></a>
## 模型体验

模型体验由组合包挂载的面向模型子插件间接提供——`dsh-system-prompt`、`dsh-tools`、`dsh-tool-skill`、`dsh-tool-bash`、`dsh-tool-jobs` 与 `dsh-llm-retry`，启用 `goals` 时还包括 `dsh-tool-goal` 与目标轮次驱动器的提示词；组合包自身不添加任何面向模型的包装内容。

#### KV Cache 影响

组合包自身不添加请求前缀内容；提供方缓存复用取决于所挂载消费方的贡献，`dsh-llm-retry` 重建重试请求时会保留其先前前缀。

## 已知限制与延期工作

<a id="known-limitations-and-deferred-work"></a>


这些限制说明组合包何时不合适，或何时需要特别的运维注意。它们是当前包约束，不是与其他组合方式的对比，也不是任务积压。

- **大部分主干集合固定在代码中**：`apply()` 始终挂载核心服务；配置可以省略组合包内的目标、skill、bash 与任务控制工具，但要替换循环或删除其他主干成员，就必须组合另一个组合包。
- **不变式服务与配套插件仍是固定成员**：`invariants.enabled: false` 或包筛选器会抑制检查，但不会移除服务或配套插件注册；Session 始终启用的校验与冻结是另一套机制。
- **重试可能重复产生提供方计费**：每次提供方尝试都可能产生计费，`always` 模式没有尝试次数上限；用量核算由入口负责。

<a id="dev-note"></a>
### 开发备注

<details>
<summary>维护者的工作上下文——点击展开</summary>

本开发备注是维护者的工作上下文，明确不具权威性——已交付的行为、限制与既定理由以上文和包代码为准。

后备标题限制（5 个词、40 个后备字节、80 个可接受标题字节）是本组合包拥有、可覆盖的示例策略，而非 `dsh-session-title` 所有；需要不同上限的入口应传入自己的 `sessionTitle` 配置。`workspaceContext` 字段为必填（不设默认值），因为它改变模型可见输入；若该字段日后改型，请保留这一要求。

</details>
