---
description: "持久终端能力家族的包映射：限定所有者范围的 ctx.terminals 服务、启动交互式 bash 或 pwsh 的 shell 后端，以及 6 个面向模型的工具。"
kind: "package-group"
---

# terminal/：持久 PTY 能力家族

[English](README.md) | 中文

## 概述

`terminal/` 组为 agent 提供持久且限定所有者范围的终端会话：shell 与 REPL 状态——cwd、导出的变量、激活的环境、正在运行的交互式子进程——都能跨工具调用存活。三个包共同覆盖整个家族：`terminal/` 提供限定所有者范围的 `ctx.terminals` 会话服务（会话获得不透明 id，每个操作都限制在所属 agent 内）；`terminal-bash/` 在共享沙箱策略下启动交互式 bash 或 pwsh shell；`tool-terminal/` 提供 6 个结果有界的面向模型工具。终端是单次 bash 与文件系统工具的补充：仅在需要交互式 stdin 或跨调用状态时使用。会话只存在于进程本地，harness 重启后不会恢复。

## 目录

- [包](#packages)
- [相关文档](#related-documentation)
- [开发备注](#dev-note)

-----

<a id="packages"></a>
## 包

该家族包含一个会话服务、一个 shell 后端与一组面向模型的工具。完整约定由各子级 README 负责；共享词汇与生成的服务接口面由子系统参考负责。

| 包 | 角色 | ctx 键 |
|---|---|---|
| [`terminal/`](terminal/README.zh.md) | 会话服务：限定所有者范围的会话、不透明 id、精确到所有者的限制与等待完成的清理 | `ctx.terminals` |
| [`terminal-bash/`](terminal-bash/README.zh.md) | shell 后端：在共享沙箱策略下启动交互式 bash 或 pwsh，带就绪检测与有界输出 | 注册后端到 `ctx.terminals` |
| [`tool-terminal/`](tool-terminal/README.zh.md) | 6 个面向模型的工具，带所有者隔离与可选后台发送 | 注册到 `ctx.tools` |

-----

<a id="related-documentation"></a>
## 相关文档

先从子系统参考了解共享类型与服务接口面，再从 Agent Note 了解设计理由与暂缓边界。

- [终端子系统参考](../../docs/subsystems/terminal.zh.md)——id、后端与会话约定、发送就绪、有界读取，以及生成的 `ctx.terminals` API。
- [持久 PTY Agent Note](../../.agents/notes/implemented/feature/2026-07-16-persistent-pty-sessions.zh.md)——设计决策、备选方案与延期工作。
- [能力 seam](../../docs/capability-seams.zh.md)——本家族遵循的 Service Definition / Service Provider / Consumer 拆分。

<a id="dev-note"></a>
## 开发备注

<details>
<summary>维护者的工作上下文——点击展开</summary>

无。

</details>
