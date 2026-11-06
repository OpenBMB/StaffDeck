---
description: "test-support 组地图：面向编写与运行仓库测试的开发者，提供无密钥测试工具、LLM mock 与回放服务器以及 Loader 冒烟测试辅助。"
kind: "package-group"
---

# packages/test-support

[English](README.md) | 中文

## 概述

test-support 组为仓库测试提供确定性、无密钥地运行真实产品的方式。它包含 Loader 应用 harness、session-log 快照适配器、回放 LLM（大语言模型）插件和可编脚本的 OpenAI 兼容故障服务器。每个包都是支持层基础设施；当某个包获得产品约定与产品消费方时，它就会移出本组。

## 目录

- [包](#packages)
- [相关文档](#related-documentation)
- [开发备注](#dev-note)

-----

<a id="packages"></a>
## 包

| 包 | 职责 |
|---|---|
| [`session-snapshot`](session-snapshot/README.zh.md) | 为 profile 驱动的测试提供 session-log 快照支持与协议适配器 |
| [`agent-loop-testkit`](agent-loop-testkit/README.zh.md) | 为运行具体 AgentLoop 的测试提供共享先决服务 |
| [`client-runtime`](client-runtime/README.zh.md) | 为浏览器功能测试提供 jsdom slot 测试台 |
| [`loader-smoke`](loader-smoke/README.zh.md) | 启动由 Loader 组合的应用并驱动 fixture 轮次以执行冒烟测试 |
| [`llm-mock-server`](llm-mock-server/README.zh.md) | 为恢复测试提供可编脚本的 OpenAI 兼容故障服务器 |
| [`llm-replay`](llm-replay/README.zh.md) | 为无密钥测试与演示回放已记录的模型流 |

-----

<a id="related-documentation"></a>
## 相关文档

- [测试策略](../../docs/testing.zh.md)——这些 harness 所服务的无密钥快照层及其适用时机。
- [运行时不变式子系统](../../docs/subsystems/invariants.zh.md)——每个 test-support 包以 `./invariant` 形式随附的包自有运行时检查。
- [包组](../README.zh.md)——支持组与产品组的关系。

-----

<a id="dev-note"></a>
## 开发备注

<details>
<summary>维护者的工作上下文——点击展开</summary>

无。

</details>
