---
description: "web 访问能力家族的包映射：搜索/抓取服务、其提供方后端，以及消费它们的面向模型工具。"
kind: "package-group"
---

# web/：web 访问能力家族

[English](README.md) | 中文

## 概述

`web/` 组为 harness 提供 web 访问能力——搜索 web 与抓取 URL——通过一个与提供方无关的服务（`ctx.web`）以及使用它的后端和工具。部署可以挂载一个或多个后端——搜索用 Exa、Perplexity 或 DeepSeek，抓取用匿名 HTTP(S)——服务按操作挑选可用的提供方，因此后端来来去去，面向模型的工具保持稳定。六个包构成该家族：负责提供方选择与错误的 `web/` 服务、三个搜索后端、一个抓取后端，以及向模型公开 `web_search` 与 `web_fetch` 的 `tool-web/`。该组只拥有 web 访问本身：没有浏览或提取，没有逐 URL 策略，各后端保留自己的资源上限。搜索与抓取有意共用一项服务，使选择、取消、错误与配置只有一个归属方。

## 目录

- [包](#packages)
- [相关文档](#related-documentation)
- [开发备注](#dev-note)

-----

<a id="packages"></a>
## 包

六个包分别承担 web 角色；子系统参考文档拥有穷尽式词汇与约定。

| 包 | 职责 | ctx 键 |
|---|---|---|
| [`web/`](web/README.zh.md) | 搜索/抓取服务：通过可互换的后端搜索与抓取 URL，统一选择与错误策略 | `ctx.web` |
| [`web-search-exa/`](web-search-exa/README.zh.md) | 通过 Exa 搜索 web | 注册到 `ctx.web` |
| [`web-search-perplexity/`](web-search-perplexity/README.zh.md) | 通过 Perplexity 搜索 web | 注册到 `ctx.web` |
| [`web-search-deepseek/`](web-search-deepseek/README.zh.md) | 通过 DeepSeek 原生搜索搜索 web | 注册到 `ctx.web` |
| [`web-fetch-http/`](web-fetch-http/README.zh.md) | 匿名抓取公共 HTTP(S) 页面 | 注册到 `ctx.web` |
| [`tool-web/`](tool-web/README.zh.md) | 向模型公开 `web_search` 与 `web_fetch` | 注册到 `ctx.tools` |

-----

<a id="related-documentation"></a>
## 相关文档

先从子系统参考文档了解共享词汇，再看单一提供方选择服务背后的设计决策。

- [web 子系统](../../docs/subsystems/web.zh.md)——搜索/抓取请求与结果、提供方可用性、`WebError` 与公开地址强制规则。
- [web 能力 seam 决策](../../.agents/notes/implemented/architecture/2026-06-24-web-capability-seam.zh.md)——搜索与抓取为何共用一项提供方选择服务。

<a id="dev-note"></a>
## 开发备注

<details>
<summary>维护者的工作上下文——点击展开</summary>

无。

</details>
