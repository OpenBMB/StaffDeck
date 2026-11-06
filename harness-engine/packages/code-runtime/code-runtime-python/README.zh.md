---
description: "Node host 与 CPython 子进程之间的 fd-3 协议格式（wire protocol），供用户与维护者构建或排查 Python 代码执行后端。"
kind: "package-library"
---

# @deepseek-ai/dsh-code-runtime-python

[English](README.md) | 中文

## 概述

`dsh-code-runtime-python` 持有 [`dsh-code-runtime`](../code-runtime/README.zh.md) seam 的 Node host 与 CPython 子进程之间的无版本协议格式（wire protocol）：子进程 fd 3 上每行一个 JSON 对象，让 stdout/stderr 空出给程序自己的输出。本包提供 host 侧的帧编解码与敌意帧校验器（`src/protocol.ts`），以及同一套消息词汇的 Python 侧镜像（`py/protocol.py`），因此每个 wire 消费方都共享同一套词汇。它是 Python 后端的协议层——本包不含子进程执行路径，因此除跨语言镜像测试之外，没有任何地方会启动 `python3`。host 把每个入站帧都当作敌意输入，因为模型代码对 fd 3 有完全访问权、可通过它发送任意内容。

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

当你要构建或消费 CPython 代码运行时 wire 时选择本包：实现 Python 后端或驱动它的 host，或排查 Python 代码运行的帧。本包是为 CPython code-runtime 提供方准备的 wire 协议——这样的提供方会在全新的 `python3 -I` 子进程中运行每个模型程序——本包提供两侧共同使用的协议，因此其导出是 wire 的 TS 侧唯一真源。

### 你得到什么

本包从 `src/index.ts` 重新导出 host 侧的协议词汇：`validateChildFrame`（在 host 读取前重建每个入站帧）、无损 JSON 编解码与计量器（`encodeJsonPlain`、`checkDoneValue`、`hasUnsafeIntegerToken`、`hasNonLosslessNumber`），以及 `logTruncationMarker`（共享的截断标记文本）。Python 侧在 `py/protocol.py` 中把消息形状镜像为 `TypedDict`，并重新声明两侧都执行的两个表面——`PROTOCOL_FD = 3` 与标记文本。

### 协议格式

帧在子进程 fd 3 上以 JSON-lines 传输——每行一个对象——因此 stdout/stderr 保持空闲，供程序自己的输出使用。子进程 → host：`boot-ack`、`call`、`log`、`done`。host → 子进程：`boot`（首帧，携带所有上限与命名空间声明）、`run`（在 `boot-ack` 之后，只携带程序主体），以及每个 `call` 一个 `reply`。伪造帧可以在 `done` 上同时携带 `value` 与 `error`，因此消费方必须先检查 `error`，在它存在时忽略 `value`。

### 可能出什么问题

host 侧校验会静默丢弃垃圾，因此格式错误或伪造的帧绝不会让宿主进程崩溃：`validateChildFrame` 对任何无法干净重建的内容返回 `undefined`，非数字的 call id 绝不会被回显进 reply，伪造的额外字段绝不随行。不是无损 JSON、或超出配置字节预算的完成值会被明确拒绝（`non-lossless`／`over-budget`），而不会被静默舍入或截断。

-----

<a id="understand-the-implementation"></a>
## 理解实现

<details>
<summary>实现细节——点击展开</summary>

本节解释协议格式（wire protocol）背后的设计；可观察行为已在[使用本包](#use-this-package)中完整说明。

### 设计理念

协议假定单向信任：host 把每个入站帧都当作敌意输入（模型代码可以在 fd 3 上伪造任何内容），并在读取前逐字段重建；Python 侧信任 host 回复，因为 host 不受模型控制。本包刻意只是协议层——Python 侧 JSON codec 位于后端的 bootstrap 中，而非 `py/protocol.py`，因此镜像保持为 `src/protocol.ts` 的纯 wire 词汇对侧。

### 协议约定

帧为 `boot`／`run`（host → 子进程）与 `boot-ack`／`call`／`log`／`done` 加每个 call 一个 `reply`（子进程 → host）。`log` 帧的 `truncated` 标志标记「就是子进程 ledger 截断标记」的那个帧，因此 host 在子进程停下的同一点停止捕获，而不是根据自己的预算推断。`done.error.kind` 是 `exception`、`invalid-output`、`output-limit` 之一；墙钟／CPU 预算、中止与基底终止在 host 侧观测，不作为帧携带。

### 无损 JSON 穿越

完成值与 binding 参数以精确 JSON 穿越：值无递归地序列化，因此低于字节预算的深层 payload 能完整穿越，而不是死在 `JSON.stringify` 的栈限制上；超出安全范围的整数型 double 以精确数字穿越，而不是被静默舍入的 token；[`src/protocol.ts`](src/protocol.ts) 中的计量器在任何其他代码读取 payload 之前强制执行字节预算与数字无损性。

### 镜像对齐

`tests/protocol-mirror.e2e.ts` 启动一个真实 `python3`，对照 `src/protocol.ts` 断言 `PROTOCOL_FD`／截断标记文本，以及 `py/protocol.py` 中每个 `TypedDict` 的必填/可选 wire 字段集，因此重命名或删除字段——或一侧把另一侧必填的字段改为可选——都会让测试失败。跨语言边界不比较字段*类型*；该残留由 review 加后端的真子进程套件负责。

### 源码地图

| 文件 | 职责 |
|---|---|
| [`src/index.ts`](src/index.ts) | 插件入口：为每个 wire 消费方重新导出协议词汇 |
| [`src/protocol.ts`](src/protocol.ts) | host 侧：帧编解码、敌意帧校验器、无损 JSON 计量器、共享标记文本 |
| [`py/protocol.py`](py/protocol.py) | Python 侧：`PROTOCOL_FD`、`TypedDict` 帧镜像、`log_truncation_marker` |
| [`tests/protocol-mirror.e2e.ts`](tests/protocol-mirror.e2e.ts) | 对照真实 `python3` 的跨语言镜像测试 |
| [`src/invariant.ts`](src/invariant.ts) | 不变式伴生插件（无运行时不变式；本包不注册任何可变数据关系） |

</details>

-----

<a id="further-exploration"></a>
## 进一步探索

当协议约定不够用时阅读以下内容。它们从 seam 定义进入协议的设计记录与配套后端。

- [代码运行时 seam](../code-runtime/README.zh.md)——Python 后端实现的抽象约定。
- [fd-3 协议 Agent Note](../../../.agents/notes/implemented/architecture/2026-07-31-code-runtime-python-fd3-protocol.zh.md)——设计理由、协议约定与镜像对齐决策。
- [Worker 线程后端](../code-runtime-worker-thread/README.zh.md)——已发布的 TypeScript 兄弟包，是 Python 后端行为的模板。
- [代码运行时子系统参考](../../../docs/subsystems/code-runtime.zh.md)——请求／结果词汇、绑定与失败分类体系。

-----

<a id="model-experience"></a>
## 模型体验

通过 `dsh-tools` 中的 PTC mode 间接提供；后者把程序的完成值或失败渲染进一个保留的 `run_code` 结果。

#### KV Cache 影响

不会直接失效；由上述消费方负责请求前缀变更。

## 已知限制与延期工作

<a id="known-limitations-and-deferred-work"></a>


这些限制说明本包覆盖什么、不覆盖什么；它们是当前包约束，不是任务积压。

- **跨语言 guard 覆盖执行表面与帧字段形状，但不覆盖字段类型**——镜像 e2e 比较必填/可选字段集，而不比较 `cpuSeconds` 两侧是否都是 `int`；跨 TypeScript 与 Python 比较类型声明在此无机械等价物，因此类型级漂移由 review 加后端的真子进程套件捕获。
- **`src/index.ts` 只导出协议词汇**——本包不含子进程执行路径，也不含 Python 侧的 JSON codec，因此除镜像测试之外没有任何地方会启动 `python3`。

<a id="dev-note"></a>
### 开发备注

<details>
<summary>维护者的工作上下文——点击展开</summary>

无。

</details>
