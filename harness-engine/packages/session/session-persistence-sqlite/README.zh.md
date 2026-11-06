---
description: "面向部署方与维护者的 SQLite 会话持久化说明，用于选择、配置或排查这个可选启用的分片行后端。"
kind: "package-reference"
---

# @deepseek-ai/dsh-session-persistence-sqlite

[English](README.md) | 中文

## 概述

`dsh-session-persistence-sqlite` 是 `SessionPersistence` 服务的可选存储后端：它不按会话各留一个文件，而是把所有会话的持久事件日志统一保存在同一个 SQLite 数据库中。它与 JSONL 后端提供完全相同的逻辑 `SessionEvent` 流，因此选择它不会改变 agent loop、模型或回放的任何行为——打包、压缩与恢复都是存储内部细节。仅当单一可查询数据库适合你的部署时才选择它；任何已发布的组合都不会默认启用它。这是预发布提供方：它拒绝而非迁移不属于自己的数据库文件，而且其同步 Node SQLite 驱动会在读写时阻塞 JavaScript 线程。设置、容量评估与迁移指引在前；实现内部细节放在下方可折叠的开发者章节中。

## 目录

- [使用本包](#use-this-package)
- [理解实现](#understand-the-implementation)
- [进一步探索](#further-exploration)
- [模型体验](#model-experience)
- [已知限制与延期工作](#known-limitations-and-deferred-work)
- [开发备注](#dev-note)

-----

<a id="use-this-package"></a>
## 使用本包

当组合需要由 SQLite 支撑的持久会话、且可以接受进程本地的同步数据库驱动时，挂载此提供方。常用路径是显式的：加载会话服务、挂载提供方，然后给出数据库路径。

### 何时选择

当本地部署受益于一个可查询数据库、而非每会话一个独立文件时，选择此后端。当消费方需要按会话产物时，请选择 JSONL 后端：本提供方的 `locate(meta)` 返回 `undefined`，不支持原始产物，也不暴露任何单会话文件。高并发服务在采用前还应考虑同步 SQLite 与压缩工作。

### 磁盘占用与性能

打包布局以部分 SQLite 本地延迟换取更小的可查询数据库。现有的 501 会话对比测量的是 schema 19，而不是 schema 20；该布局占用 233.18 MB，SQLite 对比基线占用 438.31 MB，压缩 JSONL 占用 148.15 MB。全量写入约比 JSONL 快 2.3 倍，后缀读取也仍快得多；完整读取与 fork 则略慢于 JSONL。方法、完整指标与取舍由[持久化延迟与 page size 决策](../../../.agents/notes/implemented/architecture/2026-08-25-persistence-latency-and-page-size.zh.md)记录。

磁盘成本换来的是结构化、可查询的会话历史视图：外部工具可以用 SQL 分析 `sessions` 与 `events`，按本提供方的方式解码物理行——这是内置全文搜索等功能的天然基础。

### 最小配置

先加载会话服务，再用数据库路径挂载提供方。除非位置允许依赖进程工作目录（相对路径从该目录解析），否则请使用绝对路径。`:memory:` 可用于进程内数据库，其内容随进程消失。

```yaml
- name: '@deepseek-ai/dsh-session'
- name: '@deepseek-ai/dsh-session-persistence-sqlite'
  config:
    path: /absolute/path/to/sessions.db
```

| 字段 | 默认值 | 含义 |
|---|---|---|
| `path` | 必填 | SQLite 数据库路径，或 `:memory:` |
| `journalMode` | `wal` | 持久 journal mode：`wal`、`delete`、`truncate` 或 `persist` |
| `busyTimeoutMs` | `5,000` | 等待另一连接锁的最长同步时间 |
| `preparedSessionCacheSize` | `5` | 为恢复复用而保留的冷会话准备结果数量 |
| `writeBatchMaxDelayMs` | `200` | 实时事件的固定聚合窗口，单位为毫秒 |

生成的[配置目录](../../../docs/config-catalog.zh.md#deepseek-aidsh-session-persistence-sqlite)是每个受支持字段及其 JSDoc 的穷尽式真源。

### 迁移现有 JSONL 会话

没有内置迁移工具：JSONL 与 SQLite 是两个独立存储，没有任何机制在两者之间复制会话。由于两个后端实现相同的逻辑约定，你可以直接用持久化 API 迁移会话——在 JSONL 侧读取，在 SQLite 侧写入。每个组合只有一个后端服务于 `ctx.sessionPersistence`，因此两步请分两次运行或分两个进程执行：

```text
// Export — run against the JSONL composition, per session id:
const { meta, events } = await ctx.sessionPersistence.load(id)

// Import — run against the SQLite composition, per exported session:
await ctx.sessionPersistence.create(meta)
await ctx.sessionPersistence.append(id, events)
```

用 `list()` 枚举已物化的会话。导出的事件 `seq` 从 0 开始连续，因此 `append` 可以一次性按序写入新会话；`load` 会先在源端提交所需的冷修复，导出的日志因此是平衡的。请把迁移当作一次性切换：确认导入的会话可以加载后，再把组合切换到 SQLite 提供方；之后继续写旧 JSONL 根目录会让两个存储分叉。

### 启动与安全运行

全新数据库直接初始化为 schema 版本 20，并使用 64 KiB page。已有文件不会被重新调参：任何其他版本、外来应用标识、无版本的非全新 schema 或意外 schema 对象，都会在任何数据暴露或变更之前被拒绝。本预发布提供方不提供迁移。每条语句和固定 pragma 都来自 `resources/sql/` 下打包的 `.sql` 资源，运行时的值以 SQLite 参数绑定，包代码从不拼装查询文本。

每个连接都会禁用 SQLite trusted schema 与内存映射 I/O、验证所请求的 journal mode，并固定 `synchronous=FULL`，保证成功返回的追加在操作系统崩溃或断电后依然持久。在 POSIX 上，数据库父目录和文件必须属于当前用户，父目录不得允许组或其他用户写入，文件也不得授予任何组或其他用户权限；Windows 还会拒绝符号链接和非普通文件，ACL 限制则由部署方负责。路径与所有权失败会拒绝插件初始化；Node 的 SQLite 驱动在首次持久化操作时才延迟加载。普通 `create` 会保持惰性直到首次 append，而 `ensureMaterialized` 会写入一条没有事件行的会话元数据记录。

-----

<a id="understand-the-implementation"></a>
## 理解实现

<details>
<summary>实现细节——点击展开</summary>

本节解释提供方背后的设计决策，并指出实现它们的代码位置；可观察行为已在[使用本包](#use-this-package)中完整说明。

### 设计理念

本提供方建立在一个分离与三项承诺之上：

- **逻辑约定，物理格式。** 调用方始终读写普通的 `SessionEvent[]`；行如何打包、存储与压缩是本包私有的存储行为。
- **schema 拥有格式。** Schema 20 是冻结的物理约定：任何其他版本、外来标识或意外 schema 对象的数据库都会被拒绝，绝不迁移。改变 schema、行 codec、page size 或字典字节都需要新的 schema 版本。
- **持久性是默认值。** 追加在立即事务中以 `synchronous=FULL` 提交，成功返回的 `append()` 意味着该批次已持久。普通追加仅插入：更早的事件行永远不会被重写。
- **在严格边界内追求效率。** 打包与压缩让数据库保持小巧，但每个上限都是硬性格式边界——每个打包行至多表示 1,024 个事件、1 MiB 载荷。

打包行基础由 [SQLite 物理分片行决策](../../../.agents/notes/implemented/architecture/2026-08-18-sqlite-physical-chunk-row-compression.zh.md)记录；当前压缩、键和 page-size 选择由[持久化延迟与 page size 决策](../../../.agents/notes/implemented/architecture/2026-08-25-persistence-latency-and-page-size.zh.md)记录。

### 源码地图

| 文件 | 职责 |
|---|---|
| [`src/index.ts`](src/index.ts) | 插件入口：`Config` schema、服务注册、协调器接线 |
| [`src/store.ts`](src/store.ts) | 存储原语：事务追加、读取、修复、路径与所有权验证 |
| [`src/schema.ts`](src/schema.ts) | schema 归属：版本门禁、连接加固、行解码 |
| [`src/codec.ts`](src/codec.ts) | 打包：哪些 `assistant/chunk` 连续段成为打包行、大小上限 |
| [`src/compression.ts`](src/compression.ts) | 物理编码：字典压缩、序列列表、行扫描与解码 |
| [`src/sql.ts`](src/sql.ts) + [`resources/sql/`](resources/sql/) | 所有 SQL 语句均为打包的闭名资源 |
| [`src/invariant.ts`](src/invariant.ts) | 不变式伴生插件（无运行时不变式；打包只能通过数据库往返观察） |

### 数据库 schema

全新数据库包含三张 STRICT 表，定义于 [`resources/sql/schema.sql`](resources/sql/schema.sql)：

| 表 | 用途 |
|---|---|
| `persistence_state` | 单行存储标识 |
| `sessions` | 每个会话一行：头部字段加单调递增的 revision |
| `events` | 物理事件行：一个逻辑事件，或一个打包连续段 |

确切的列定义见 [`resources/sql/schema.sql`](resources/sql/schema.sql)。`sessions.id` 是内部整数键，`sessions.session_key` 保留公开会话 id。`events.data` 存放文本或可独立解码的 Zstandard blob；仅在结果更小时才使用 schema 自有的共享字典压缩。`events.source_event_seqs` 使用带 tag 的 delta 或 run 编码。打包分片连续段的 `events.ignorable` 为 `0`，带 `ignorable: true` 的标量逻辑事件为 `1`，其余标量事件为 `NULL`，因此类型与物理分片标签同名的标量事件仍然明确。打包行沿用其首个逻辑事件的 `seq`，因此在复合主键 `(session_id, seq)` 下，物理顺序就是逻辑顺序。

### 写入路径

每次追加都会开启立即事务、重新验证 schema 归属、检查已存尾部以防止陈旧写入方扩展日志、只打包新批次、插入对应行、递增一次会话 revision，然后提交。协调器按配置窗口聚合实时事件，因此高频流会产生更大的打包行，而物理写入量始终与新持久批次成正比。

### 读取与恢复

完整读取先反向定位最后一个有效 `turn/end`，再按正向顺序把每个物理行解码为其逻辑事件，并拒绝已提交前缀中的缺口或格式错误行。格式错误的最后一行被视为撕裂尾部：执行恢复的加载可以在写锁下删除它，并用合成闭合事件关闭日志。后缀读取（`readFrom`）只检查可能包含目标序列的物理跨度，因此永远不会解析无关的更早行。

</details>

-----

<a id="further-exploration"></a>
## 进一步探索

当包级约定不够用时阅读以下页面。它们从共享持久化模型逐步进入穷尽式配置，以及物理布局背后的决策证据。

- [会话持久化子系统](../../../docs/subsystems/persistence.zh.md)——后端无关的服务语义与提供方关系。
- [会话包映射](../README.zh.md)——相邻的持久化、投影、标题与遥测包。
- [生成配置目录](../../../docs/config-catalog.zh.md#deepseek-aidsh-session-persistence-sqlite)——每个受支持配置字段及其源声明。
- [SQLite 物理分片行决策](../../../.agents/notes/implemented/architecture/2026-08-18-sqlite-physical-chunk-row-compression.zh.md)——打包布局背后的理由、备选方案与测量。
- [持久化延迟与 page size 决策](../../../.agents/notes/implemented/architecture/2026-08-25-persistence-latency-and-page-size.zh.md)——501 会话基准与当前存储取舍。

-----

<a id="model-experience"></a>
## 模型体验

### 恢复的对话历史

#### 模型看到什么

没有 SQLite 专有内容。恢复会还原与 JSONL 后端相同的逻辑事件和派生消息；物理打包标签永远不会进入提示词、工具、回放或实时 `session/event` 投递。

#### Token 影响

实时请求 token 为零。恢复只为保留的逻辑历史和当前请求信封消耗 token。

#### KV Cache 影响

物理打包不会改变请求前缀。提供方缓存复用取决于重建历史、当前信封与模型路由，与其他持久化后端完全相同。

## 已知限制与延期工作

<a id="known-limitations-and-deferred-work"></a>


这些限制说明本提供方何时不合适，或何时需要特别的运维注意。它们是当前包约束，不是通用 SQLite 对比或任务积压。

- **预发布设计，无迁移**——schema 20 是临时的 SQLite 专用设计；不保证 schema 稳定性或迁移支持。
- **打包依赖批次边界**——被写后窗口或显式 flush 拆开的兼容连续段仍分属不同物理行；这避免了重写先前行，代价是打包比例依赖时序。
- **同步 SQLite 与压缩**——Node 的 SQLite 驱动与 Zstandard 调用会阻塞 JavaScript 线程。
- **忙等待阻塞事件循环**——SQLite 在同步调用内部等待；竞争写入方最长可让线程停顿配置的 `busyTimeoutMs`。
- **外部 SQL 读取方必须解码物理行**——打包的 `events.type`（`text-chunks`、`reasoning-chunks`、`tool-call-chunks`）不是逻辑事件类型；受支持的消费方通过本提供方读取。
- **没有删除或历史压缩**——普通追加仅插入，没有任何机制移除旧行。

<a id="dev-note"></a>
### 开发备注

<details>
<summary>维护者的工作上下文——点击展开</summary>

501 会话语料包含私有会话数据，因此不提交到仓库。汇总方法、完整结果与未采用候选记录在[持久化延迟与 page size 决策](../../../.agents/notes/implemented/architecture/2026-08-25-persistence-latency-and-page-size.zh.md)中；带 hash 固定的打包字典资源是 schema 20 真源的一部分。

</details>
