---
description: "凭据能力族的包映射：凭据引用 seam、环境与文件提供方、授权 flow 注册表，以及引用如何让机密值留在配置之外。"
kind: "package-group"
---

# credentials/：凭据与授权

[English](README.md) | 中文

## 概述

`credentials/` 组管理你的配置按名引用的机密值：API 密钥只存一次，在 settings 或 `cordis.yml` 中按名引用，轮换时无需编辑任何配置文件。它提供产品中负责存储与查询机密的运行时部分（`credentials/`）、默认的本机凭据文件（`credentials-local/`），以及授权 flow 注册表（`authorization/`）——用于获取无法配置、只能开口去要的凭据。轮换后的密钥会作用于紧随其后的下一次模型请求，而按次运行的环境覆盖（`DEEPSEEK_API_KEY=… dsh`）始终优先于存储值。机密值绝不进入你同步或渲染的配置文件——进去的只有它们的名字，而且本地文件只有同一 OS 用户可读，其他用户读不到。

## 目录

- [包](#packages)
- [相关文档](#related-documentation)
- [开发备注](#dev-note)

-----

<a id="packages"></a>
## 包

三个包共同提供凭据功能：一个在运行时存储、查询与移除机密，而配置只写名字；第二个是默认的本机存储；第三个让插件获取必须开口去要的凭据。它们的 README 覆盖日常使用；子系统参考拥有穷尽式约定。

| 包 | 角色 | ctx 键 |
|---|---|---|
| [`credentials/`](credentials/README.zh.md) | 在运行时存储、查询与移除机密，而配置只写名字 | `ctx.credentials` |
| [`credentials-local/`](credentials-local/README.zh.md) | 默认本机存储：一个私有 YAML 文件，环境覆盖优先 | 注册 `ctx.credentials` |
| [`authorization/`](authorization/README.zh.md) | 由插件拥有、通过询问人来取得凭据的 flow | `ctx.authorization` |

-----

<a id="related-documentation"></a>
## 相关文档

先从子系统参考了解共享词汇，再看能力 seam 表与本地存储的配置面。

- [凭据子系统参考](../../docs/subsystems/credentials.zh.md)——`CredentialRef` 与 `CredentialKey`、按操作解析、对 UI 安全的 `CredentialInfo`、授权 flow 与生成的 cordis 接口面。
- [能力 seam](../../docs/capability-seams.zh.md)——本家族遵循的 Service Definition / Service Provider / Consumer 拆分。
- [生成配置目录](../../docs/config-catalog.zh.md#deepseek-aidsh-credentials-local)——本地存储的每个受支持字段。

<a id="dev-note"></a>
## 开发备注

<details>
<summary>维护者的工作上下文——点击展开</summary>

无。

</details>
