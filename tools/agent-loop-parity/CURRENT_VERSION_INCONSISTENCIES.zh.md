# 当前版本 AgentLoop 对拍不一致记录

更新时间：2026-09-03

本文只记录当前 checkout 的已观察差异和仍未证明等价的边界。它不把随机 ID、时间戳、JSONL
transport envelope 或 StaffDeck 自己的结果包装差异当成 AgentLoop 语义 bug。

## 被测版本与范围

- PilotDeck：当前 `Kaguya-19/refactor/core_agent_loop_0831` checkout，sidecar 已重新 build。
- StaffDeck：当前 `codex/pilotdeck-agent-loop` checkout，`PILOTDECK_AGENT_LOOP_ENABLED=true`。
- 对拍输入：同一组 10 个确定性 scenario、同一 `q`、同一 mock LLM provider 和 mock tool backend。
- StaffDeck 真实部署：前端、FastAPI、PilotDeck sidecar 和本地 mock provider 均通过真实 HTTP/浏览器入口运行过。
- 对拍结果：10 个场景均没有 `BLOCKED`；比较器退出码为 `1`，仍有声明外差异。
- 本轮 glue 修补后：StaffDeck AgentLoop 相关定向测试 `123 passed`；PilotDeck Node 22
  `tsc --noEmit` 和 sidecar/default-factory 测试通过。

## 已观察的不一致

### PilotDeck Native 与 Sidecar

| 级别 | 差异 | 证据 | 当前影响 | 是否已证明为产品语义差异 |
|---|---|---|---|---|
| P2 | 工具/权限事件边界不同。Native trace 记录 PermissionRuntime 决策，sidecar trace 记录 host `module_call` 和 capability response。 | 工具、权限和 max-turn 场景的 raw trace 路径和事件数量不同，但 canonical tool name、arguments、result、终态和用户输出一致。 | 影响审计 trace 的逐事件对比和观测指标。 | 否，当前属于协议/trace projection 差异。 |
| P2 | capability response 携带 `content`、`metadata`、`toolCallId` 等 Module Protocol 字段，native result 只保留业务结果。 | sidecar response envelope 与 native semantic tool result 字段集合不同。 | 下游若直接消费 raw trace，需先做 semantic projection。 | 否。 |
| P2 | Permission 决策执行位置不同。 | Native 在 PilotDeck PermissionRuntime 内判断；sidecar 由宿主 module port 返回结果。 | 宿主 permission adapter 必须保持相同规则；当前场景最终均 deny/无副作用。 | 当前场景否，泛化到所有 permission rule 尚未完全证明。 |
| P2 | 工具批量调用的并发时序没有被完整证明。 | 对拍比较了调用集合和结果顺序；现有场景没有稳定地产生两个真实 sidecar tool call 的开始/完成时序。 | 无法据此承诺 native 与 sidecar 的并发窗口完全一致。 | 未验证。 |

### StaffDeck Legacy 与 StaffDeck + PilotDeck AgentLoop

| 级别 | 差异 | 证据 | 当前影响 | 是否已证明为产品语义差异 |
|---|---|---|---|---|
| P2 | 模型输入仍存在 provider-facing envelope 差异。Legacy 使用 Harness system prompt、TaskRequirement、iteration、remaining actions 和 StaffDeck transcript；sidecar 现在通过通用 `contextOverride.systemPrompt/messages/metadata` 携带同一上下文，但 bridge 仍保留 StaffDeck provider 的 `conversation_context`/`harness_transcript` 投影。 | 修补后 sidecar payload 已包含完整 canonical task/history/image/tool-result context；全量对拍仍报告 raw request envelope 差异。 | 语义投影已补齐，但不能宣称 provider 最终序列化逐字相同；需在真实 provider 请求层继续校验。 | 未完全证明，当前主要是 glue/provider serialization 差异。 |
| P2 | Harness payload/checkpoint 与 sidecar checkpoint 的持久化包装不同。 | Legacy 保存 StaffDeck `TaskRequirement`/Harness 字段；sidecar 保存 canonical AgentLoop state/seed projection。 | 影响恢复审计和 raw checkpoint 对拍。 | 否，当前 checkpoint 场景的可支持 messages 恢复结果一致。 |
| P2 | cancel 的观察时点可能不同。 | Legacy 在模型/工具边界检查取消；sidecar 可能已发起一次 module/model call 后才收到取消。 | 中间 trace 顺序可能不同；最终均聚合为 `cancelled/CANCELLED`，迟到 completed 不被接受。 | 当前终态否；竞态下的完整中间事件等价未完全证明。 |
| P2 | `allowedReadFiles` 的实际文件约束尚未验证。 | checkpoint 对拍只验证了 seed state/messages 投影，没有执行真实受限文件读取。 | 不能证明 sidecar 工具对允许读取路径的拒绝/放行行为与 legacy 完全一致。 | 未验证。 |
| P3 | 真实 HTTP 部署目前覆盖的是纯文本 conversation 任务。 | 浏览器实际发送消息并完成 Harness run；数据库有 `agent.model_request_started`、`agent.model_event`、`agent.turn_completed`。 | 已证明部署、sidecar 启动和基本聊天链路可用，但不是完整工具/图片/取消生产验收。 | 未验证。 |

## 已排除的严重差异

本轮对拍没有观察到以下 P1 级结果差异：

- completed / failed / cancelled / result_unknown 被错误互相转换；
- `max_turns` 被错误映射为 completed；
- 图片 data URL 在 StaffDeck sidecar 链路中丢失；
- canonical `tool_result` 被丢弃导致模型无法看到工具结果；
- permission deny 产生工具副作用；
- cancel 之后迟到的 completed 被宿主接受为成功；
- StaffDeck action budget 在外层 Harness 中失效。

这里的“排除”仅针对已运行的确定性场景，不等同于所有 provider、工具和生产数据组合都已形式化证明。

## 非 AgentLoop 失败

StaffDeck 全量 pytest 本次结果为 `2084 passed, 12 failed`。失败集中在 channel/Feishu/WeChat/WeCom
迁移测试的旧 schema 场景，例如测试表缺少 `team_id` 或后台 handoff 测试缺少表；没有证据表明这些
失败由本次 AgentLoop glue 改动引入，因此不列为本次对拍语义差异。

## 后续验证项

1. 增加真实 sidecar 多工具场景，记录 dispatch start/finish 序号并与 native 对比并发语义。
2. 增加 `allowedReadFiles` 的真实文件工具 scenario，验证路径放行和拒绝。
3. 在真实 HTTP 部署中加入图片、工具错误、permission deny、cancel 和 deadline 请求，并从数据库
   Harness run/frame/turn 结果生成同一套 semantic trace。
4. 为 native/sidecar 定义稳定的 semantic trace projection，避免 Module Protocol envelope 差异
   继续导致比较器退出码为 `1`。
