---
description: "extensions 组地图：用于定义、运行与移除动态 Cordis 包的模型侧工具和双半 runner，供浏览本组的用户与维护者阅读。"
kind: "package-group"
---

# packages/extensions

[English](README.md) | 中文

## 概述

extensions 组让运行中的 agent 修改它自己所在的运行时：模型可以检查当前 DSH 进程里加载的插件与服务，定义动态 Cordis 包（可含 host 半、浏览器半或两者），运行、停止并彻底移除它，浏览器面板则操作全部定义。包按插件演进：一个插件持有若干不可变的包版本，可以在它们之间运行或更新。定义只存在于进程内存中，因此 DSH 重启即清空，本组不会写仓库文件，也不改任何配置。四个包构成整个子系统：模型侧工具加 host 半 runner，浏览器半 runner 加浏览器 UI。

## 目录

- [包](#packages)
- [相关文档](#related-documentation)
- [开发备注](#dev-note)

-----

<a id="packages"></a>
## 包

| 包 | 职责 | ctx 键 |
|---|---|---|
| [`tool-cordis`](tool-cordis/README.zh.md) | 七个模型侧工具：检查实时运行时，定义、运行、停止并移除动态包 | 注册到 `ctx.tools` |
| [`cordis-host-runner`](cordis-host-runner/README.zh.md) | host 半：定义注册表、沙箱化的 host 半生命周期，以及浏览器查询应答的 inspect 注册表 | 提供 `ctx.dynamicCordisRunner` 与 `ctx.cordisInspect` |
| [`cordis-client-runner`](cordis-client-runner/README.zh.md) | 浏览器半：把浏览器半源码求值成活插件，并应答运行请求 | client 面；提供浏览器侧 `ctx.dynamicCordisRunner` |
| [`ui-cordis`](ui-cordis/README.zh.md) | 浏览器面：全局面板、生命周期工具卡片与 `@pluginId` 输入源 | client 面；注册 slot |

-----

<a id="related-documentation"></a>
## 相关文档

- [extensions 子系统](../../docs/subsystems/extensions.zh.md)——生成的 `ctx.cordisInspect` 与 `ctx.dynamicCordisRunner` 服务 API。
- [生成的工具目录](../../docs/tool-catalog.zh.md#deepseek-aidsh-tool-cordis)——七个模型侧工具 schema。
- [生成的配置目录](../../docs/config-catalog.zh.md#deepseek-aidsh-cordis-host-runner)——runner 的受支持配置字段。
- [自引用 Cordis 工具集 Agent Note](../../.agents/notes/implemented/feature/2026-07-08-self-referential-cordis-toolset.zh.md)——沙箱语义、生命周期与组合的设计居所。
- [客户端外壳与动态包 Agent Note](../../.agents/notes/implemented/architecture/2026-08-15-client-shells-and-dynamic-packages.zh.md)——浏览器半的包归属与构建面。

-----

<a id="dev-note"></a>
## 开发备注

<details>
<summary>维护者的工作上下文——点击展开</summary>

两个浏览器半包住在本组而不是 `packages/client/` 下，因为它们是本子系统双半包的其中一半；client 面经由 client program 编译它们，host program 只引用 host runner。

</details>
