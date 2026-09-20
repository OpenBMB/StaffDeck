# AgentLoop 对拍行为差异

对拍基于 2026-09-03 的完整 10 场景运行。PilotDeck 使用真实 Gateway、HTTP/WebSocket、
Session、ToolRuntime、PermissionRuntime 和 sidecar 进程；外部 LLM/tool endpoint 使用确定性 mock。
两条 PilotDeck 路径均成功执行，没有 `BLOCKED`。
“对齐”指关键执行语义、终态和用户输出一致；“部分对齐”表示最终结果一致，但模型输入、
执行边界或中间状态仍不同。

## PilotDeck Native 与 Sidecar

| 场景 | PilotDeck native | PilotDeck sidecar | 结论与影响 |
|---|---|---|---|
| 纯文本 | 单条当前 user message；host ContextRuntime 组装完整 system prompt | 相同消息，并通过 context module 调用同一个 host ContextRuntime | **对齐**。同版本请求逐字段一致。 |
| 单工具 | host 根据 `canPrompt` 暴露有效工具，并执行一次 `lookup` | 保留 `requiresUserInteraction` 后得到相同工具目录，由同一个 host runtime 执行 | **对齐**。模型输入、调用和结果一致。 |
| 多工具 | 一次 `executeAll()`，先批量 permission preflight，再开始两个工具 | 一个 `execute_batch` module call 转给同一个 `executeAll()` | **对齐**。permission、tool-start、结果顺序和输出一致。 |
| 工具错误 | host `ToolRuntime` 将 `MOCK_TOOL_ERROR` 标准化为 `tool_execution_failed` recovery | 同一个 host `ToolRuntime` 完成相同标准化 | **对齐关键行为**。送给模型的 tool-result 逐字段一致，不是 sidecar 自行解释错误。 |
| 权限拒绝 | host `PermissionRuntime` 拒绝，工具后端未调用 | 同一个 host `PermissionRuntime` 拒绝，工具后端未调用 | **对齐关键行为**。送给模型的 `permission_denied` recovery 一致。 |
| 最大轮次 | `failed/agent_max_turns_reached` | `failed/agent_max_turns_reached` | **对齐关键行为**。终态、错误码和工具执行一致。 |
| Deadline | Gateway `turn_timeout`，最终 failed | 同一 Gateway `turn_timeout`，并取消 sidecar module call | **对齐**。无阻塞，晚到非终态不再污染结果。 |
| 取消 | `cancelled/aborted_streaming` | `cancelled/aborted_streaming` | **对齐**。取消在模型请求开始后发出并得到 Gateway 确认。 |
| 图片 | 文本和图片位于同一 user message | 文本和相同图片位于同一 user message | **对齐**。data/MIME/分组/顺序一致；比较器仅忽略派生 `bytes`。 |
| Checkpoint 恢复 | 历史 assistant 后接当前 user | 相同顺序，最终消息回写 host transcript | **对齐关键行为**。浏览器重开 session 后回复仍可读取。 |

## StaffDeck Legacy 与 PilotDeck AgentLoop

| 场景 | StaffDeck legacy | StaffDeck + PilotDeck AgentLoop | 结论与影响 |
|---|---|---|---|
| 纯文本 | Harness system prompt、TaskRequirement 和运行字段进入模型 | canonical messages/tools 进入模型 | **关键行为对齐**。模型输入协议仍不同，但响应、终态和用户输出一致。 |
| 单工具 | `lookup` 1 次，模型看到结果后完成 | `lookup` 1 次，模型看到结果后完成 | **对齐**。此前重复调用由 bridge 丢失 `tool_result` 导致，已修复。 |
| 多工具 | 顺序执行 `lookup`、`summarize` 后完成 | 顺序执行 `lookup`、`summarize` 后完成 | **对齐**。bridge 现支持完整 action sequence。 |
| 工具错误 | `lookup_error` 1 次，模型看到不可重试错误后完成 | `lookup_error` 1 次，模型看到不可重试错误后完成 | **对齐**。错误码和 retryability 保留在 host result。 |
| 权限拒绝 | 拒绝 1 次，无工具副作用，模型随后完成 | 拒绝 1 次，无工具副作用，模型随后完成 | **对齐**。拒绝结果已投影回下一轮模型上下文。 |
| Action budget | 执行一次 `loop` 后得到 `action_budget/ACTION_BUDGET_EXHAUSTED` | 相同调用次数、终态、frame/run 状态和用户输出 | **对齐**。验证的是 StaffDeck 外层预算，不代表 PilotDeck 核心具备通用 action budget。 |
| Deadline | `failed/SOP_STEP_TIMEOUT`，frame/run 为 failed | `failed/SOP_STEP_TIMEOUT`，frame/run 为 failed | **对齐**。sidecar deadline 由 StaffDeck glue 聚合为 legacy step timeout。 |
| 取消 | 在模型调用前观察取消；turn/frame/run 均 cancelled | 启动 sidecar 前复用正式取消检查；turn/frame/run 均 cancelled | **关键行为对齐**。并行调度下仍可能出现取消竞态，但不接受迟到成功。 |
| 图片 | 通过真实 attachment/image payload 输入模型 | 通过相同 attachment 链路输入模型 | **关键行为对齐**。图片内容、终态和输出一致；消息包装不同。 |
| Checkpoint 恢复 | 读取预建 transcript 并完成 | 读取相同 transcript 和通用 `agentLoopSeedState` 并完成 | **关键行为对齐**。消息顺序和输出一致；`allowedReadFiles` 仍需实际文件工具场景验证。 |

完整 trace、原始 diff 数量、版本和运行命令见
[`EXPERIMENT_RESULTS.zh.md`](./EXPERIMENT_RESULTS.zh.md)。同版本 PilotDeck native/sidecar 的十个场景
均为零 canonical diff。`origin/main` 与当前工作树仍存在非 AgentLoop 的版本漂移，主要是 skill 路径和
shell 工具描述更新；该结果单独记录，不作为 sidecar 行为差异。

## 扩展场景汇总

| Suite | 场景数 | 对拍结果 | 需要关注的差异 |
|---|---:|---|---|
| `core-regression` | 10 | 通过，0 semantic diff | 无 |
| `core-resilience` | 20 | 通过，0 semantic diff | 无；文件权限场景已改为使用 PilotDeck 内置 `read_file`，不再有 adapter 启动阻塞 |
| `staffdeck-workflow` | 24 | 未通过，0 BLOCKED，23 个场景有 semantic/oracle diff | SOP step/slot/handoff/knowledge budget、deadline/unknown、action budget 的状态聚合仍不一致，见实验结果第 14 节 |
| `known-gap` | 1 | 按声明稳定复现 | `auto_compact` 的 host context overflow 与 sidecar 完成路径不同，精确 16 条 diff |

StaffDeck workflow 的差异是当前真实 Harness 与 sidecar glue 的可观察行为，不是比较器
宽泛归一化造成的结果。PilotDeck core 两个 suite 才是通用 AgentLoop sidecar 的零差异 gate；
`known-gap` 单独作为预期失败报告，不计入零差异 gate。
