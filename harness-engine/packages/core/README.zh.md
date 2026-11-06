---
description: "core 分组地图：构成产品 API 主干的会话日志、系统提示词组装、工具注册表、agent 词汇与默认循环。"
kind: "package-group"
---

# packages/core

[English](README.md) | 中文

## 概述

core 分组提供 DeepSeek Harness 的产品 API 主干：仅追加的会话日志、系统提示词组装、工具注册表、`Agent` 句柄，以及驱动它们的具体循环。每个组合都会启动这些包，插件与消费方构建所依赖的正是它们稳定的约定。一个轮次会流经其中全部环节——循环领取提示词，在会话日志上打开轮次，通过 system-prompt 组装请求，流式接收模型响应，通过注册表分发工具调用，并把每个模型可见的事实追加回日志。构建或扩展 agent 时请选择本分组；默认可运行组合是 [`examples/agent-spine-demo`](../examples/agent-spine-demo/README.zh.md)。

## 目录

- [包](#packages)
- [相关文档](#related-documentation)
- [开发备注](#dev-note)

-----

<a id="packages"></a>
## 包

| 包 | 职责 | ctx key |
|---|---|---|
| [`scope/`](scope/README.zh.md) | 隔离单个 agent 贡献的作用域注册与事件路由 | 库，不使用 ctx key |
| [`session/`](session/README.zh.md) | 每个 agent 的历史都由其派生的仅追加会话事件日志 | `ctx.sessions` |
| [`system-prompt/`](system-prompt/README.zh.md) | 由有序段、工具 schema 与变量进行的系统提示词组装 | `ctx.systemPrompt` |
| [`tools/`](tools/README.zh.md) | 循环分发所经过的工具注册表与受守卫的执行流水线 | `ctx.tools` |
| [`agent-tool-presentation/`](agent-tool-presentation/README.zh.md) | 为 preset 提供按 agent 的工具呈现方式选择器 | 无 ctx key |
| [`agent/`](agent/README.zh.md) | 插件面向编程的 `Agent` 句柄，以及其实时注册表与事件 | `ctx.agents` |
| [`agent-default-model/`](agent-default-model/README.zh.md) | 入口对全新 agent 应用的部署默认模型选择 | `ctx.agentDefaultModel` |
| [`agent-loop/`](agent-loop/README.zh.md) | 默认 agent 驱动器：创建 agent 并运行轮次与步骤生命周期 | `ctx.agentLoop` |

`scope` 提供共享作用域原语；`agent` 拥有公开的 `Agent` 约定，而 `agent-loop` 是其默认实现，因此扩展插件依赖 `agent`，驱动器保持可替换。`agent-default-model` 拥有会话自身没有选择时由入口应用的部署选择。可运行组合位于 [`examples/agent-spine-demo`](../examples/agent-spine-demo/README.zh.md)；本分组只拥有可替换的主干组件。

-----

<a id="related-documentation"></a>
## 相关文档

- [Core 子系统](../../docs/subsystems/core.zh.md)——逐包循环图与 `Agent` 句柄约定。
- [会话子系统](../../docs/subsystems/session.zh.md)——会话事件词汇与派生历史。
- [系统提示词子系统](../../docs/subsystems/system-prompt.zh.md)——提示词段、动态上下文与工具 schema 类型。
- [工具子系统](../../docs/subsystems/tools.zh.md)——工具执行流水线与呈现词汇。
- [作用域注册子系统](../../docs/subsystems/scope.zh.md)——这些注册表所依赖的作用域层原语。
- [架构](../../docs/architecture.zh.md)——轮次流与新行为归属。
- [agent-spine-demo](../examples/agent-spine-demo/README.zh.md)——默认可运行组合。

-----

<a id="dev-note"></a>
## 开发备注

<details>
<summary>维护者的工作上下文——点击展开</summary>

无。

</details>
