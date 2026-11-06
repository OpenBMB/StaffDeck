---
description: "工具输出 spill 能力家族的包映射：存储服务、本地后端与结果策略各自提供什么。"
kind: "package-group"
---

# spill/：工具输出 spill 能力家族

[English](README.md) | 中文

## 概述

`spill/` 组在不丢失超大工具输出的前提下把它们挡在模型上下文之外：当某个工具结果超过部署配置的字节上限时，完整文本会保存到 spill 产物中，模型只看到有界预览和一个稍后可以读取或搜索的定位信息。该家族拆分为三个包——`spill/` 中的存储服务、`spill-local/` 中的本地文件系统后端，以及 `spill-policy/` 中决定最终工具结果何时过大并触发 spill 的结果策略。spill 是可选且尽力而为的：只有配置了 `maxInlineBytes` 时策略才会生效，存储失败时原始结果仍然可见。本组只负责存储与结果替换；预览机制归 `dsh-output-retention` 所有，提供方资源上限保持独立。

## 目录

- [包](#packages)
- [相关文档](#related-documentation)
- [开发备注](#dev-note)

-----

<a id="packages"></a>
## 包

三个包分别承担 spill 角色；子系统参考文档拥有穷尽式词汇与约定。

| 包 | 职责 | ctx 键 |
|---|---|---|
| [`spill/`](spill/README.zh.md) | 存储服务：保存过大的工具文本并返回定位信息与取回指引 | `ctx.spillStore` |
| [`spill-local/`](spill-local/README.zh.md) | 将 spill 文本保存到本机的私有会话级文件 | 注册到 `ctx.spillStore` |
| [`spill-policy/`](spill-policy/README.zh.md) | 用预览和定位信息替换过大的纯文本工具结果 | 监听 `ctx.tools` |

-----

<a id="related-documentation"></a>
## 相关文档

先从子系统参考文档了解共享词汇，再看设计决策及其持久日志扩展。

- [spill 子系统](../../docs/subsystems/spill.zh.md)——`SaveTextSpill`/`SpillRef` 词汇、归属与后端关系。
- [工具输出 spill 决策](../../.agents/notes/implemented/architecture/2026-07-08-tool-output-spill-files.zh.md)——存储、保留与工具自有输出处理之间的能力边界。
- [代码 dispatch-log spill 决策](../../.agents/notes/implemented/feature/2026-07-26-ptc-dispatch-log-spill.zh.md)——为何 `run_code` 子调用结果的持久副本同样设界。

<a id="dev-note"></a>
## 开发备注

<details>
<summary>维护者的工作上下文——点击展开</summary>

无。

</details>
