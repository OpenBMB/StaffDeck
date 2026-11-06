---
description: "examples 包组：供测试与自定义部署使用的可复用 agent-spine 组合 bundle。"
kind: "package-group"
---

# examples/：可复用组合 bundle

[English](README.md) | 中文

## 概述

examples 组提供可复用 agent 主干，供需要具体组合但不想手工组装的测试与自定义部署使用。其 npm 名称中的 `-demo` 后缀表明它是支持基础设施，而非产品接口。ACP、SDK 与单次执行应用分别通过 `acp`、`sdk` 或 `sdk-minimal`、`headless` profile 启动。本组不包含应用入口。

## 目录

- [包](#packages)
- [相关文档](#related-documentation)
- [开发备注](#dev-note)

-----

<a id="packages"></a>
## 包

| 包 | npm 名称 | 角色 |
|---|---|---|
| [`agent-spine-demo/`](agent-spine-demo/README.zh.md) | `@deepseek-ai/dsh-agent-spine-demo` | 可挂载、可用自己的 LLM 与执行器配置的工作 agent 核心 |

`agent-spine-demo` 是共享 agent 核心。产品应用装配位于 [`bundle/`](../bundle/README.zh.md)；这个支持包继续供聚焦测试与自定义组合使用。

-----

<a id="related-documentation"></a>
## 相关文档

- [ACP 应用组合包](../bundle/acp-app/README.zh.md)——面向程序化客户端的 `dsh --profile acp` 应用。
- [SDK 应用组合包](../bundle/sdk-app/README.zh.md)——面向 JSON-RPC 客户端的 `dsh --profile sdk` 应用。
- [极简 SDK 组合包](../bundle/sdk-minimal/README.zh.md)——Python 示例使用的独立双工具 SDK profile。

<a id="dev-note"></a>
## 开发备注

无。
