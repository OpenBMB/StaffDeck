# StaffDeck 飞书渠道接入开发设计

## 1. 目标与范围

在现有微信、企业微信渠道内核上新增飞书渠道，使企业自建应用通过飞书官方长连接接收消息，并将消息路由到 StaffDeck 数字员工。首版支持：

- 飞书企业自建应用的 `App ID + App Secret` 配置与连接状态管理。
- 单聊文本消息。
- 群聊中明确 `@` 当前机器人的文本消息。
- StaffDeck 身份、会话、对话日志、自动路由和多员工挂载。
- 文本回复、投递日志、失败重试和重启恢复。
- 同一 StaffDeck 租户接入多个飞书应用，以及不同租户之间的身份隔离。

首版不支持图片、文件、语音、卡片、消息编辑/撤回和主动群发。这些事件必须被安全忽略，不能创建用户、会话或对话轮。文本消息持久接收后，父进程通过独立 receipt outbox 添加飞书官方 `Get` 表情，最终回复送达后异步移除；receipt 失败不得阻塞 ACK、AgentLoop 或正文回复。企业微信和微信当前没有对应的机器人 reaction API，不共享该能力。

## 2. 接入方式与依赖

使用飞书长连接，不使用公网 Webhook。该方案适合 StaffDeck 桌面版和私有部署，不要求用户配置公网域名。

- 固定依赖 `lark-channel-sdk==1.2.0`，避免 SDK 生命周期和回调语义漂移。
- 入站使用官方 SDK 的 `EventDispatcherHandler` 和 WebSocket client。
- 出站使用官方消息 API，显式传入 StaffDeck 派生的 `uuid`。
- 打包配置收集 `lark_channel` 的动态子模块，并增加打包后的导入冒烟测试。

不能直接使用 SDK 高层 `FeishuChannel` 的默认入站回调：它会先调度异步处理再 ACK，且用户 handler 异常不会传回飞书，无法保证“数据库提交成功后才 ACK”。

### 2.1 开发前置 SDK spike（P0 gate）

`lark-channel-sdk==1.2.0` 的底层 WebSocket client 使用模块级 event loop，高层 `start()` 会对同一个 loop 调用 `run_until_complete()`。因此正式编码前先做一个不接业务逻辑的适配验证：

1. 使用 `multiprocessing.get_context("spawn")` 为两个 binding 启动两个独立 connector 子进程，每个进程只持有一个 SDK client 和 event loop。
2. 单独停止、重启其中一个进程，另一个连接不受影响。
3. 同步 dispatcher 在数据库提交后返回成功；模拟提交失败时向飞书返回失败。
4. 确认 SDK 不会在 StaffDeck 持久化前执行内部去重或 stale 丢弃。
5. 反复 start/stop、断网重连，无 loop 泄漏、线程残留或互相 stop。
6. 正常数据库提交后的 wire ACK 小于 3 秒；watchdog 从收到完整 DATA frame 时 arm，直到 response frame 的 `await conn.send()` 成功后才 disarm。2.5 秒内未完成真实 wire ACK 就终止 connector 进程，覆盖数据库锁、fsync、commit 和 ACK send 卡顿。
7. 使用生产代码同位置的 `BEFORE_COMMIT`、SQLite `COMMIT` trace、`AFTER_COMMIT_BEFORE_ACK`、`ACK_WRITTEN` 故障注入点证明回滚和重投去重，不能用固定 sleep 猜测时序。SQLite trace 仅证明进入 `commit()` 调用栈但 native commit 尚未完成，不宣称可确定暂停在 fsync 内部；native fsync 强杀留给 frozen artifact fault harness。
8. 两个 connector 并行时，一个 binding 的慢事务只能保证另一个 binding 在 deadline 内独立 ACK 或 NACK 并完成收敛；共享 SQLite 写锁场景不承诺另一个一定 ACK success。
9. 父进程异常退出后，子进程通过 Pipe EOF/租约检测自行退出，并由 child 持有的 per-binding OS lock 保证 StaffDeck 重启时同一 binding 最多一个 connector。
10. 在真实 macOS `.app`、Windows exe/PyInstaller 产物验证 spawn、Pipe 控制、独立重启、父进程死亡和强杀回滚，不能只 monkeypatch 平台或做 import smoke。

该进程隔离方案是对 SDK 1.2.0 module-global loop 的明确边界。子进程内允许一个集中、最小的固定版本 compatibility observer/subclass，只用于在真实 DATA frame 进入时 arm watchdog、在 response frame 实际写成功后 disarm，并保留 SDK 的 frame correlation；业务归一化和数据库逻辑不得复制进该层。若 spike 证明跨平台进程生命周期或这个 wire hook 无法稳定实现，则停止业务开发，改用带 per-instance loop 和正式 pre-ACK hook 的固定 fork；不能以“仅支持一个绑定”或接受 ACK 后丢消息窗口作为交付方案。

### 2.2 Process spike 状态机与验收契约

父进程状态固定为：

```text
ABSENT -> STARTING -> RUNNING -> STOPPING -> ABSENT
                     |                     -> CRASH_BACKOFF
                     -> CRASH_BACKOFF
```

每条 IPC 必须携带 `binding_id + config_revision + child_nonce + pid`。`READY`、`CONNECTED`、`DISCONNECTED`、`DISPATCH_STARTED`、`DISPATCH_FINISHED`、`STOPPED` 只接受当前 nonce 和 revision，迟到的旧进程状态仅记录，不得覆盖新进程或数据库连接状态。

重配顺序固定为：pause reconcile -> 请求旧进程停止 -> 确认 OS 进程退出且 binding lock 释放 -> CAS 提交 revision N+1 -> spawn N+1。分别在“旧进程退出前”“退出后更新前”“更新后新进程启动前”杀父进程，重启都必须收敛到数据库指向的唯一代次。

watchdog 使用双保险：子进程原生控制线程以 monotonic deadline 监控当前 frame token，超时直接 `os._exit()`；父进程收到 `DISPATCH_STARTED` 后也建立 deadline，未收到同 token 的完成/进程退出就 terminate，再升级 kill。若 commit 未完成，进程退出使事务回滚；若 commit 已完成但 ACK 未写成功，飞书重投由唯一键去重。

连续 watchdog/crash 使用指数退避和 circuit breaker。默认连续 3 次快速失败后进入可观测的 `CRASH_BACKOFF`，避免 poison event 的重投触发 spawn 风暴；管理员手动重试或冷却期到期后才恢复。

## 3. 飞书侧配置要求

用户需要在飞书开放平台创建企业自建应用，并完成：

1. 启用机器人能力。
2. 订阅 `im.message.receive_v1`。
3. 选择长连接接收事件。
4. 申请单聊消息、群聊中被 `@` 消息和发送消息所需权限。
5. 发布应用并在当前企业内安装。

首版不申请读取群内全部消息的敏感权限。群聊只有明确 `@` 当前机器人时才响应。

## 4. 核心数据语义

### 4.1 入站幂等键

使用 `event.message.message_id` 作为 `ChannelInbound.event_id`。不能使用 `header.event_id`，因为飞书对同一条消息的重投可能产生不同事件 ID。

现有 `(binding_id, event_id)` 唯一约束可直接保证同一绑定内一条飞书消息只进入一个对话轮。

### 4.2 用户身份

- 外部用户 ID 使用 `sender.sender_id.open_id`。
- 不使用可能缺失的 `user_id`，也不使用展示名作为身份键。
- 账号作用域由 `app_id` 和事件头 `tenant_key` 共同组成：

  `app:<app_id长度>:<app_id>:tenant:<tenant_key长度>:<tenant_key>`

- 身份唯一键继续使用 `(staffdeck tenant_id, channel, external_account_scope, external_user_id)`。
- `header.app_id` 或 `header.tenant_key` 与绑定已固定值不一致时拒绝事件。

凭证首次保存时以 `app_id` 建立待激活配置，并把 `external_account_key` 固定为全局唯一的 `feishu:app:<app_id长度>:<app_id>`。`ChannelBinding` 增加独立、可 CAS 的 `provider_tenant_key`，不把首次观测值并发写回整个 `config_json`。CAS 成功后，`identity_scope_key` 固定为 `app:<app_id长度>:<app_id>:tenant:<tenant_key长度>:<tenant_key>`。

首次事件在一个事务中完成以下操作：按 binding ID 和启动时 `config_revision` 重载绑定；校验 `app_id`；执行 `provider_tenant_key IS NULL -> header.tenant_key` 条件更新；回读权威值；固定 `identity_scope_key=app_id+tenant_key`；最后插入 inbox。两个不同 tenant key 并发时只能一个成功，失败者安全 ACK 丢弃并记录安全日志，避免错误事件被无限重投。

首次固定 tenant key 是单调元数据更新，**不递增**连接 `config_revision`，也不需要同步更新 manager 内存。每次 callback 的 staging 事务都从数据库读取权威值；CAS 提交后即使进程退出，重启也能继续消费已落库事件。固定后不允许通过更新凭证更换 `app_id` 或 `tenant_key`；App Secret 可以轮换，更换应用应删除并重建绑定。`config_json.tenant_key` 即使存在也只是展示副本，不能参与安全校验。

### 4.3 会话与投递目标

| 场景 | 会话锚点 | 回复目标 |
| --- | --- | --- |
| 单聊 | `open_id` | 新消息：`receive_id_type=open_id`, `receive_id=open_id` |
| 普通群聊 | `chat_id` | 回复源 `message_id` |
| 群话题 | `(chat_id, thread_id)` | 回复源 `message_id`，保持在原话题 |

首版将每个飞书话题视为独立会话，稳定键同时编码 `chat_id + thread_id`；只有飞书明确返回 `thread_id` 时才按话题处理。`root_id` 也会出现在普通回复树中，不能用于推断话题。非话题群消息共享 `chat_id` 会话。群聊仍保留发送者 `open_id` 用于审计和显示；现有渠道内核的群会话主体语义保持不变，不在本次重构为“一群多 StaffDeck 用户”。

`ChannelInbound` 增加显式 `thread_id` 和可覆盖的 `external_conv_id`，避免用 `group_id` 隐式推导飞书话题会话。`ChannelInboundEvent` 增加不可变 `target_json`，stage 时固化 `receive_id_type`、`receive_id`、源 `message_id`、`chat_id` 和 `thread_id`。用户消息已有的 `metadata_json.client_turn_id` 关联到入站 `event_id`；Outbox staging 通过 `(binding_id, client_turn_id)` 查到入站事件，并把目标复制到 `ChannelDelivery.target_json`。异步回复不再从会被新消息覆盖的 `ChatSession.channel_target_json` 读取飞书目标。

群聊回复源消息失败时按原目标进入 Outbox 重试或最终失败，不自动降级为群内新消息，避免重复发送或脱离话题。

## 5. 入站可靠性设计

飞书要求事件处理在 3 秒内返回，否则会重试。AgentLoop 可能运行数十秒，所以回调只负责校验、归一化和持久化，不能同步执行对话轮。

```text
飞书 WS 回调
  -> 校验 binding 代次、app_id、tenant_key 和消息类型
  -> 归一化消息
  -> INSERT channel_inbound_events(status=received)
  -> COMMIT 成功后 ACK
  -> 唤醒后台 worker
  -> worker 原子 claim received -> processing
  -> process staged inbound
  -> done / failed
```

新增两个通用 intake 能力：

- `stage_inbound(binding, inbound)`：只登记 `received` 事件并提交。唯一键冲突视为已接收，成功 ACK；其他数据库错误必须抛出，让 SDK 返回失败并等待飞书重投。
- `process_staged_inbound(inbound_event_pk)`：使用 `ChannelInboundEvent.id` 原子领取已登记事件，并复用现有身份、会话、AgentLoop、outbox 和崩溃恢复逻辑。

`payload_json` 固定保存可版本化的重放 envelope，worker 不能依赖 SDK 对象或内存队列：

```json
{
  "schema_version": 1,
  "inbound": {
    "message_id": "...",
    "app_id": "...",
    "tenant_key": "...",
    "external_account_key": "...",
    "identity_scope_key": "...",
    "sender_open_id": "...",
    "sender_name": "...",
    "chat_type": "p2p|group",
    "chat_id": "...",
    "thread_id": "...",
    "root_id": "...",
    "text": "清洗机器人 mention 后的文本",
    "target": {}
  },
  "raw": {}
}
```

`raw` 只用于诊断并遵循日志/数据保留策略；处理以版本化 `inbound` 为准。版本读取器必须显式支持当前和存量版本，未知版本标记 failed 并报警，不能猜测字段。

`ChannelInboundEvent` 增加观测用 `config_revision`，stage 时一并保存。`config_revision` 只用于 **stage 前** fence 旧连接 callback：事件一旦成功提交为 `received`，只要其 `external_account_key/app_id/tenant_key` 仍与绑定一致，即使随后轮换 App Secret，也必须继续处理，不能按当前 revision 丢弃。worker 不用 revision 过滤已经 durable 的合法事件。凭证更新只有在旧 ingress 完全停止并越过 callback barrier 后才能提交；停止超时则更新失败且数据库保持不变。

worker 使用数据库作为事实来源，内存事件只负责降低轮询延迟：

- 启动后立即扫描本绑定所有 `received` 事件。
- 被回调唤醒或按短周期轮询时继续领取。
- 按 `ChannelInboundEvent.id` 通过 `received -> processing` 条件更新完成单条领取，避免重复 worker 并发处理。
- 停止接收新 callback 后，等待在飞书回调内的数据库事务完成，再有界停止 worker。
- 现有 `processing` 事件的进程代次恢复逻辑继续适用。

### 5.1 ACK / NACK / drop 矩阵

| 情况 | 对飞书结果 | StaffDeck 行为 |
| --- | --- | --- |
| 新合法消息，inbox commit 可见 | ACK success | 唤醒 worker |
| 重复 `(binding_id, message_id)` | ACK success | 不创建第二个 turn |
| 非文本、未 @ 当前机器人、bot/self sender、空文本 | ACK success | 永久忽略并计数 |
| JSON/必需 ID 缺失 | ACK success | poison event 安全丢弃并记录受限日志 |
| app/tenant 不匹配、binding disabled/deleted | ACK success | 安全丢弃并记录告警 |
| 旧 `config_revision` callback | ACK success | barrier 外迟到事件丢弃并告警 |
| 数据库暂时不可用、锁冲突、commit 失败 | NACK/failure | 不留半条数据，等待飞书重投 |
| 未分类的 staging 内部异常 | NACK/failure | 报警，等待重投 |

compatibility adapter 必须暴露可测试的明确结果，而不是用普通 Python `True/False` 猜测 SDK wire response。测试要从独立数据库连接确认 commit 已可见后，才允许观察到 success ACK。

## 6. 消息归一化与群聊策略

只接受 `message_type == text` 且 JSON `content.text` 为非空字符串的消息。

- 单聊文本直接处理。
- 群聊必须包含当前机器人的 mention。
- 仅删除当前机器人的 mention 占位符，保留其他人的 mention 和剩余文本。
- 删除机器人 mention 后为空则忽略。
- `sender_type` 为 `app`、`bot` 或当前机器人自身时忽略，防止机器人互聊。
- 缺少 `message_id`、`open_id`、群聊 `chat_id`、`app_id` 或 `tenant_key` 时拒绝归一化。
- 非文本和无效事件在持久化前忽略，不创建身份、会话和对话轮。

## 7. 长连接生命周期

先把现有硬编码的 `_ingress_manager(channel)` 改为 provider/manager registry，再新增 `FeishuStreamManager`，接入现有 connector 进程锁、binding lifecycle lock 和 reconcile 生命周期。

官方 SDK WebSocket client 使用模块级 event loop，因此不能把多个 binding 放入同一父进程 event loop。只有第 2.1 节 spike 全部通过后，才采用：

- 父进程 `FeishuProcessManager` 仅负责 reconcile、spawn、健康状态和停止边界。
- 每个 active binding 使用 `spawn` 启动一个 connector 子进程；子进程启动后再导入/构造 SDK client、创建 SQLite 连接并按 binding ID 从数据库读取和解密 Secret，禁止继承父进程 DB/loop/thread 状态。
- Secret 不放入命令行或可见环境变量；父进程只向子进程传 binding ID 和不可逆的运行代次信息。
- 子进程使用独立 `NullPool` 或单连接 staging engine，连接级 busy timeout 小于 watchdog，不复用默认 30 秒全局 engine。首版仅支持文件 SQLite；非文件 SQLite 或其他未验证数据库在激活飞书绑定时 fail fast。
- child 在连接飞书前获取数据库路径派生、binding ID 哈希命名的 OS lock，并持有到进程退出。新 manager 只能等待 lock 释放，绝不根据存量 PID 单独强杀未知进程，避免 PID 复用误伤。
- 同一应用的连接数受飞书 50 连接限制；StaffDeck 默认一个 binding 一个连接，并在 UI/API 对超限给出明确错误。
- reconnect 在子进程内由 SDK 负责，父 manager 通过受限 IPC 健康事件把实际状态对账到 `binding.connected`。
- 配置更新统一执行 pause spawn/reconcile -> 请求优雅断开 -> join/waitpid -> 超时 terminate/kill -> 再次确认 PID 已退出 -> update -> start。旧进程未确认退出时不得提交新凭证或 revision。
- callback 携带启动时的 `config_revision`；旧代次 callback 即使晚到也不得落库。
- SDK client 在子进程主线程创建；控制线程通过 `run_coroutine_threadsafe(client._disconnect(), sdk_loop)` 再 `loop.stop()` 退出阻塞 `start()`。自动 reconnect 睡眠或 callback 阻塞无法优雅退出时，父进程升级 terminate/kill，并始终 `join/waitpid` 确认退出。
- 子进程监控父子 Pipe EOF 或短租约；父进程崩溃后必须自行退出并释放 binding lock。重启 manager 只有成功获取 binding lock 才能连接，不以 PID 存活判断唯一性。
- disable/delete/父进程退出时先暂停新 spawn，再按相同确认退出流程收敛所有 connector。
- 全局 shutdown 先并行通知所有子进程，再共享一个绝对 deadline 执行 join -> terminate -> kill，不能让每个 binding 分别消耗完整超时。任一 child 未收敛时保留全局 connector lock并返回失败。

manager registry 的最小协议固定为 `start()`、`stop(deadline)`、`ensure_binding()`、`pause_binding()`、`resume_binding()` 和 `wait_binding_stopped()`。全局启动/停止遍历 registry；微信和企微仅注册现有 manager factory，不重写内部实现。

飞书同步 callback 内只允许纯本地、有界的解析、校验和一次短数据库事务，禁止调用飞书 HTTP API、AgentLoop 或其他外部服务。SQLite `busy_timeout` 设为小于 watchdog 的值，但它不作为总 deadline；进程 watchdog 才是解析、锁等待、fsync、commit 和 ACK write 卡顿的最终边界。ACK 路径记录从完整 frame 到 wire send 成功的耗时。

桌面入口必须在主入口 guard 内、调用 `main()` 前执行 `multiprocessing.freeze_support()`。worker target 是模块顶层可 pickle 函数，模块导入不得加载 FastAPI、启动 GUI/Uvicorn、探测端口或打开浏览器。frozen child 必须解析到与父进程一致的用户数据目录和数据库；PyInstaller 收集 worker、`lark_channel` 及其动态子模块。

子进程只负责 durable inbox staging。AgentLoop、durable received 轮询、Outbox 和 token cache 均留在父进程；commit 后 IPC 唤醒只是优化，父进程必须轮询数据库以覆盖 commit 后、通知前子进程退出。

若实现必须调用 SDK 私有 `_connect`/`_disconnect` API，应集中封装在一个兼容层，并通过固定版本的 SDK contract tests 锁定签名和行为；业务代码不得直接散落调用私有 API。

## 8. 出站投递与幂等

扩展 adapter 协议：

```python
send(binding, target, text, *, idempotency_key: str | None = None)
```

`service_outbox` 将 `ChannelDelivery.idempotency_key` 传入 adapter。微信和企微接受该参数但可忽略；飞书对每个文本分片生成稳定 UUID：

```text
sha256("<delivery idempotency key>:<chunk index>")[:40]
```

该值小于飞书 50 字符限制。同一投递重试使用相同 UUID，不同分片使用不同 UUID，从而覆盖飞书一小时去重窗口内“飞书发送成功但 StaffDeck 尚未标记 delivered 就崩溃”的重复发送窗口。群话题调用 reply API 时传源 `message_id`、同一稳定 `uuid`，并显式设置 `reply_in_thread=true`。

发送目标必须来自已持久化的 `target_json`，不能在重试时根据当前配置重新推断。飞书 SDK 自身的业务层重试关闭或限制为单次请求，由 StaffDeck Outbox 统一控制重试和状态。

`ChannelDelivery` 增加 `first_attempt_at`。进程恢复发现飞书 delivery 卡在 `sending` 时：

- 首次尝试距今仍在安全去重窗内，使用相同分片 UUID 重试。
- 已超过保守阈值（默认 55 分钟），不自动重发，标记 `failed` 且错误码为 `remote_state_unknown`，由管理界面提示人工确认。
- 分片部分成功按同一规则处理；去重窗内重试时已成功分片被飞书去重，失败分片继续发送。

因此首版提供的是去重窗口内的 effectively-once；超过窗口的模糊投递优先避免重复，不宣称无限期 exactly-once。

### 8.1 实时执行步骤卡片（trace streaming）

飞书渠道支持在对话执行过程中实时展示智能体每一步（SOP 匹配 / 步骤推进 / 工具调用 / 知识检索），以一张独立交互式卡片（`msg_type=interactive`）随事件推进逐步 PATCH 更新，与正文回复分开。

- **开关**：`channel_feishu_trace_enabled`（默认 `True`），仅影响飞书渠道；可在 binding `config_json` 中设置 `trace_enabled: false` 细粒度关闭。
- **生命周期**：intake worker 在 `handle_turn` 前调 `FeishuTraceStreamer.start()` 创建"正在执行"卡片；执行中通过 `EventLog.event_sink` 钩子把每个 trace 事件转发给 `streamer.on_event`，复用网页端 `_event_trace_lines` 渲染为可读行，节流（最小 1s 间隔）PATCH 更新卡片；结束后 `finish()` 定格为完成状态，异常路径 `abort()` 定格为失败状态。
- **adapter 扩展**：`create_card(binding, target, card_json, *, idempotency_key)` 发送交互式卡片并返回 `message_id`；`update_card(binding, message_id, card_json)` 调 `PATCH /im/v1/messages/{message_id}` 更新卡片内容。`_request()` 支持 PATCH 方法。
- **解耦**：卡片是"进度展示"，不进入 outbox 重试体系；正文回复仍走 outbox 幂等投递，两者互不影响。
- **失败隔离**：卡片创建/更新失败仅记日志，绝不影响 turn 成功与回复投递。
- **崩溃恢复**：intake worker 崩溃时卡片可能停留在"正在执行"状态（可接受）；重启后不重放卡片更新。

### 8.2 Token 与错误分类

`tenant_access_token` 由后端 token provider 管理：

- 缓存键包含 `external_account_key + provider_tenant_key + config_revision`，不同 App/企业/Secret 代次绝不共享。
- 在官方 `expire` 前 5 分钟主动刷新；同一缓存键用 single-flight，避免并发刷新风暴。
- Secret 更新提交后立即失效旧 token cache。
- HTTP 401/Token 失效只允许强制刷新并重试一次。
- HTTP 429、5xx、连接中断和 timeout 视为临时失败，交给 Outbox 退避重试。
- 权限不足、目标非法、消息已不可回复等确定性 4xx 或飞书确定性业务码直接标记 failed。
- HTTP 200 仍必须检查飞书响应 `code == 0`；错误映射集中维护并保守分类，未知错误默认按有限次数临时失败处理。
- 日志、异常、API 和测试快照对 App Secret 与 token 全量脱敏。

## 9. 凭证 API 与前端

新增渠道元数据：

- `channel=feishu`
- 名称：飞书
- 配置方式：credentials
- 字段：`app_id`、`app_secret`

保存凭证时调用飞书 API 校验凭证并获取机器人 `open_id` 和名称；密钥只加密保存，任何读取 API、日志和异常都不能返回明文。

绑定配置建议展示：

- `app_id`
- `bot_open_id`
- `bot_name`

`provider_tenant_key` 是模型独立字段，不以 `config_json` 为权威来源。

前端新增独立 `FeishuSetup.tsx`，复用企微凭证页的状态与交互，但展示飞书开放平台的准确步骤、App ID/Secret 输入、连接状态和重新连接。非登录密码字段设置合适的 `autocomplete`，避免浏览器将 App Secret 当作账号密码记忆。

## 10. 代码改动范围

预期主要改动：

- `backend/app/channels/adapters/feishu.py`
- `backend/app/channels/feishu_process.py`（父 supervisor 与 spawn worker 入口）
- `backend/app/channels/adapters/base.py`
- `backend/app/channels/service_intake.py`
- `backend/app/channels/service_outbox.py`
- `backend/app/channels/service_identity.py`
- `backend/app/channels/__init__.py`
- `backend/app/api/channels.py`
- `backend/app/db/models.py` 和数据库迁移
- `backend/pyproject.toml` 与桌面打包配置
- `frontend-enterprise/src/pages/channels/FeishuSetup.tsx`
- `frontend-enterprise/src/pages/ChannelsPage.tsx`
- 前后端类型和中英文文案
- 飞书 adapter、manager、intake、outbox、API、迁移和前端测试

其中 `tenant_key` 的权威值保存在绑定独立字段，`config_json` 即使为展示方便保留副本也不能作为并发校验来源。

不在本次顺带重构微信/企微 manager；只有 durable inbox 和 adapter 幂等参数作为渠道内核的必要通用改动。

## 11. 测试与验收

### 11.1 自动化测试

必须覆盖：

- 单聊文本的归一化、身份、会话和回复目标。
- 群聊 `@bot`、未 `@bot`、`@bot + @其他人`、只 `@bot`。
- 机器人发送者、自身发送者和非文本消息被忽略。
- 无效 JSON 和必需 ID 缺失。
- 同一 `message_id`、不同 `event_id` 只处理一次。
- stage commit 成功后 ACK；commit 失败时 NACK。
- ACK 后进程退出，重启能消费 `received` 事件。
- stage revision N 后 ACK、轮换 Secret 到 N+1、重启仍恰好消费一次 N 事件。
- 并发重复 staging 只保留一条事件。
- 两个不同 tenant key 首次并发只能一个 CAS 成功；相同 tenant key 并发均 ACK 且按 message ID 去重。
- tenant CAS commit 后、任何内存通知前退出，重启仍可处理。
- StaffDeck tenant、飞书 app、飞书 tenant 三个维度的身份隔离。
- 首次固定 tenant key，以及后续 scope 变化拒绝且数据不变。
- 单聊 `open_id`、群聊 `chat_id` 的出站路由。
- 同投递同分片 UUID 稳定，不同分片 UUID 不同。
- 配置重启后旧连接退出、新凭证接管、旧代次 callback 无效。
- 断线重连、disable、delete、停止超时和启动失败。
- SDK 固定版本的真实 dispatcher ACK/NACK 与 lifecycle contract。使用本地 endpoint + 真实 WebSocket + 完整 P2 DATA frame，从服务端发帧并以服务端实际收到 response frame 的时间断言 ACK；不能以 callback 返回值或 child IPC 代替 wire ACK。
- 逐项验证 ACK/drop 矩阵，包含 handler 慢、SQLite lock、fsync/commit 卡顿、ACK send 卡顿，以及 commit success 后 ACK 前退出再重投。
- 两个飞书 App 并行连接、单独停止/重启互不影响。
- 在 `BEFORE_COMMIT` kill 后断言 0 行；SQLite `COMMIT` trace 内 kill 后断言 `PRAGMA integrity_check=ok`，但不把它表述为确定性 fsync 中断；`AFTER_COMMIT_BEFORE_ACK` kill 后严格 1 行；同 message ID 重投后最终严格 1 inbox/1 turn；`ACK_WRITTEN` 用于确认 watchdog 解除。callback 超时强杀后旧进程绝无晚提交。
- 重配只有在旧 PID 确认退出后才提交；优雅断开、terminate 和 kill 三条路径均覆盖。
- IPC 状态机拒绝旧 nonce/revision 的迟到 CONNECTED、DISCONNECTED 和 STOPPED，不覆盖当前 binding 状态。
- 在旧退出前、退出后更新前、更新后 spawn 前杀父进程，重启均收敛到唯一正确代次。
- 用 grandparent 启动 parent + child，强杀 parent 后立即启动新 parent；本地飞书服务端观测同 binding 任意时刻连接数不超过 1。新 parent 只等待 child 持有的 binding OS lock，不按陈旧 PID 杀进程。
- 正常 stop、自动 reconnect 睡眠期间 stop、callback 阻塞期间 stop，最终均确认 waitpid/join；无法收敛时主进程 shutdown 返回失败并保留全局 connector lock。
- spawn 子进程不继承父 DB 连接、event loop、线程或明文 Secret 参数。
- `Process.args`、环境、IPC、stdout/stderr、runtime log、异常 repr 和 API 快照均不含 App Secret/token；child 只接收 binding ID、revision、nonce。继承的 StaffDeck 解密根密钥不等于允许 App Secret 出现在参数或日志。
- 连续 3 次 watchdog 超时进入 crash backoff/circuit breaker，不持续 spawn；状态和手动恢复入口可观测。
- 真正构建并运行 macOS `.app` 与 Windows exe 的隐藏 contract 模式：`freeze_support()` 生效、worker 不递归启动 GUI/Uvicorn/浏览器、数据库/用户目录与父进程一致，两个 child 可 spawn/stop，强杀 parent 后无残留。普通 import smoke 不算通过。
- SDK 内部去重和 stale 策略不会先于 StaffDeck durable inbox 丢弃事件。
- 现有微信、企微、渠道日志和身份绑定测试全部通过。
- 前端凭证保存、密钥不回显、连接状态和错误提示。
- token 隔离、提前刷新、single-flight、Secret 轮换失效和 401 单次刷新。
- 429/5xx/timeout/确定性业务错误及 HTTP 200 非零业务码分类。
- 小于一小时的稳定 UUID 重试、超过窗口不盲发、分片部分成功和话题回复 wire payload。
- replay envelope 仅靠数据库跨进程恢复、未知 schema version 失败可观测。
- 旧数据库真实数据迁移、迁移幂等重跑、失败回滚，微信/企微 key 和 outbox/session 数据不变；迁移完成前不得 spawn 飞书 child。

### 11.2 真实飞书验收

使用两个测试用户、一个测试群和一个企业自建应用完成：

1. 单聊发送文本，StaffDeck 回复且双方日志可见。
2. 群聊不 `@` 无响应；`@bot` 有且仅有一次响应。
3. 注入首次 stage 数据库故障使 callback NACK，观察飞书约 15 秒后以同一 `message_id` 重投；恢复后只产生一个 turn。
4. 人工断网后恢复，连接状态恢复且消息不丢失。
5. 回复过程中重启 StaffDeck，已 ACK 的 `received` 消息可恢复处理。
6. 禁用或删除绑定后不再收发消息。
7. 使用第二个飞书应用制造相同用户场景，确认身份和会话不串。

## 12. 完成标准

以下条件全部满足后才建议合并：

- 飞书长连接在开发环境完成真实账号验收。
- SDK spike 的双连接、独立生命周期和同步 ACK/NACK gate 已通过。
- 真实 frozen 产物、wire ACK watchdog、binding OS lock、generation IPC 和确定故障点 contract 全部通过。
- durable stage-before-ACK、`message_id` 幂等、`open_id` 身份和 app/tenant scope 已实现。
- 飞书出站稳定 UUID 已实现并覆盖崩溃重试窗口。
- 群聊只响应当前机器人 mention，机器人消息不会形成循环。
- 全量后端测试、ruff、前端测试/类型检查和构建通过。
- 凭证明文不出现在 API、日志、测试快照或提交内容中。

## 13. 官方参考

- 长连接配置：<https://open.feishu.cn/document/ukTMukTMukTM/uYDNxYjL2QTM24iN0EjN/event-subscription-configure-/request-url-configuration-case>
- 事件订阅概览：<https://open.feishu.cn/document/server-docs/event-subscription-guide/overview>
- 接收消息事件：<https://open.feishu.cn/document/server-docs/im-v1/message/events/receive>
- 发送消息：<https://open.feishu.cn/document/server-docs/im-v1/message/create>
- 回复消息：<https://open.feishu.cn/document/server-docs/im-v1/message/reply>
- 官方 Python SDK：<https://github.com/larksuite/channel-sdk-python>
