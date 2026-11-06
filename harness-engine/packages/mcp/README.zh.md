---
description: "MCP 包组：挂载外部 Model Context Protocol 服务器，让它们的工具可以作为原生工具调用。"
kind: "package-group"
---

# MCP — 模型上下文协议

[English](README.md) | 中文

## 概述

`mcp/` 组把 harness 连接到 Model Context Protocol（MCP）工具服务器生态。本组的唯一一个包挂载外部服务器——文件系统、GitHub、数据库或记忆服务器——让模型把它的工具当作原生工具、以稳定的服务器限定名称使用。每台服务器是一条配置项；默认不启用任何服务器，因此按需逐台开启。只桥接 Tools 能力：MCP resources 与 prompts 不受支持。本页映射该组；逐包约定由包 README 负责。

## 目录

- [包](#packages)
- [相关文档](#related-documentation)
- [开发备注](#dev-note)

-----

<a id="packages"></a>
## 包

本组只包含一个包；包 README 与下方链接拥有细节。

| 包 | 提供的能力 |
|---|---|
| [`mcp-client/`](mcp-client/README.zh.md) | 挂载一台外部 MCP 服务器，让模型可以把它的工具当作原生工具调用 |

-----

<a id="related-documentation"></a>
## 相关文档

先用可运行的示例配置体验插件，再阅读 Agent Note 了解其背后的行为决策。

- [MCP 客户端插件 Agent Note](../../.agents/notes/implemented/feature/2026-07-07-mcp-client-plugin.zh.md)——桥接的设计：服务器限定命名、发现、执行与环境清洗。
- [MCP 客户端自动重连 Agent Note](../../.agents/notes/implemented/feature/2026-08-06-mcp-client-auto-reconnect.zh.md)——重连策略、单次中断的尝试预算与退出开关。
- [第三方记忆 MCP 示例 Agent Note](../../.agents/notes/implemented/feature/2026-07-31-third-party-memory-mcp-examples.zh.md)——作为参考配置交付的三个默认关闭的记忆服务器 overlay。
- [第三方记忆 MCP 指南](../../docs/user/guide/mcp-memory.zh.md)——可运行的 overlay 配置行与设置说明。
- [工具子系统参考](../../docs/subsystems/tools.zh.md)——接收已注册工具的 `ToolRuntime`。

<a id="dev-note"></a>
## 开发备注

<details>
<summary>维护者的工作上下文——点击展开</summary>

无。

</details>
