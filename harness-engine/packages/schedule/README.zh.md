---
description: "schedule 组地图：基于会话日志的会话本地持久提醒，供浏览本组的用户与维护者阅读。"
kind: "package-group"
---

# schedule/ — 仅限会话内的提醒

[English](README.md) | 中文

## 概述

schedule 组为运行中的会话提供会话本地提醒：让 agent 在稍后、绝对时间或固定间隔提醒你，每条提醒到期时都会作为同一会话中的普通消息到达。它的宿主包拥有三个管理工具，并可通过可选的 Session projection registry 发布完整活动记录集合。独立的 [`ui-schedule`](../client/ui-schedule/README.zh.md) 浏览器插件把该 projection 渲染为只读的当前状态目录，[`ui-workspace`](../client/ui-workspace/README.zh.md) 则为尽力而为的列表值明确非空的普通行与搜索结果显示闹钟。该标识只报告缓存所知的活动状态，不保证 live runtime 存在。提醒在重启后依然存在，但只留在会话内部：没有电子邮件、短信或推送通知。本页是组地图；各包 README 拥有自己的约定。

## 目录

- [包](#packages)
- [相关文档](#related-documentation)
- [开发备注](#dev-note)

-----

<a id="packages"></a>
## 包

| 包 | 职责 | ctx key |
|---|---|---|
| [`schedule/`](schedule/README.zh.md) | 会话本地提醒：安排、列出并取消活动记录；发布供 header 目录与列表行标识读取的可选只读 projection；把到期提醒作为会话消息交付 | —（工具只注册在精确的 agent scope 中） |

-----

<a id="related-documentation"></a>
## 相关文档

- [仅限会话内的 Schedule 子系统](../../docs/subsystems/schedule.zh.md)——持久记录、转换、视图与交付约定。
- [生成的工具目录](../../docs/tool-catalog.zh.md#deepseek-aidsh-schedule)——模型接收的 `schedule_create`／`schedule_list`／`schedule_delete` schema。
- [Schedule 用户指南](../../docs/user/guide/schedule.zh.md)——挂载本包的官方配置路径。
- [Web Schedule 目录](../client/ui-schedule/README.zh.md)——活动记录的可选只读浏览器呈现。

-----

<a id="dev-note"></a>
## 开发备注

<details>
<summary>维护者的工作上下文——点击展开</summary>

无。

</details>
