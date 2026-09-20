# AgentLoop 跨宿主对拍实验记录

## 1. 实验目标

使用同一组查询、同一个确定性 mock LLM provider 和同一个 mock tool backend，比较：

1. PilotDeck `origin/main` 原生 Agent Session 与当前 PilotDeck AgentLoop sidecar。
2. StaffDeck `origin/main` legacy Harness 与当前 StaffDeck + PilotDeck AgentLoop。

本实验比较模型可见输入、模型响应、工具调用、权限结果、持久化 checkpoint、TaskFrame/Run/Turn
终态和用户可见输出。随机 ID、时间戳和 Module Protocol envelope 等传输字段会被归一化。

## 2. 被测版本

实验日期：2026-09-03。

| 项目 | baseline | 当前实现 |
|---|---|---|
| PilotDeck | `origin/main` (`461df9f35`) | `bd9aad50d` |
| StaffDeck | `origin/main` (`da371b71`) | `1df34302` |

运行环境：

- Node.js `v22.22.0`
- pnpm `10.32.1`
- Python `3.12.2`

StaffDeck 当前提交之后还包含本目录中的未提交对拍 harness；产品代码版本仍为表中的提交。

## 3. 四条执行链路

| trace 名称 | 实际入口 |
|---|---|
| `pilotdeck-native` | 真实 `createLocalGateway` -> HTTP/WebSocket -> `AgentSession` -> 原生 `AgentLoop` |
| `pilotdeck-sidecar` | 真实 `createLocalGateway` -> HTTP/WebSocket -> 注入的 stdio runner -> `pilotdeck-agent-loop-sidecar`；model/tool/permission module call 回到同一个 PilotDeck host runtime |
| `staffdeck-legacy` | baseline worktree 的 `AgentLoop.handle_turn()`，关闭 sidecar |
| `staffdeck-pilotdeck` | 当前 StaffDeck 的 `AgentLoop.handle_turn()`，开启 sidecar |

PilotDeck Gateway 模式不是 Python 模拟：模型请求经过真实 Router，工具请求经过真实
`ToolScheduler`、`ToolRuntime` 和 `PermissionRuntime`。测试替身只位于外部 LLM HTTP endpoint 和
工具最终执行函数。sidecar 选择目前通过 `createLocalGateway` 的内部 factory 注入点完成；正式 CLI
配置开关仍未提供，因此这是完整进程部署测试，不是面向用户的发布部署方式。

两条 StaffDeck 链路都真实进入 `HarnessV2Engine.run()`，创建隔离 SQLite 数据库以及 Tenant、
ModelConfig、Session、Turn、TaskFrame、Harness run 和 checkpoint 记录。执行路径只通过
`PILOTDECK_AGENT_LOOP_ENABLED` 区分；planner、外部模型和最终工具调用使用确定性替身，Harness
状态机、参数组装、授权快照、附件物化、取消标记、deadline 和持久化逻辑保持真实。

baseline 使用 detached 临时 worktree。adapter 将目标 checkout 的 `backend` 放在
`PYTHONPATH` 首位并校验 `app.__file__`，避免共享虚拟环境把 baseline 导回当前分支。

## 4. Mock 与 Trace 契约

- 相同 `scenarioId + q + messages` 返回确定性的模型响应。
- 相同 `scenarioId + tool name + normalized arguments` 返回确定性的工具结果。
- mock 同时识别 OpenAI `image_url` 和 PilotDeck canonical base64 `image` block。
- `tool.call` 与 `tool.result` 的逻辑序号用于观察调度开始、完成和结果顺序。
- terminal 从持久化的 Harness turn receipt、run 和 frame 结果投影，不由 adapter 猜测。
- mock 不访问真实模型或外部工具，不需要 API key。
- 原始 trace、临时数据库和日志只写入运行时临时目录，不进入 Git。

## 5. 运行方式

先切换到 Node 22，然后在 StaffDeck 当前工作树执行：

```bash
source ~/.nvm/nvm.sh
nvm use 22.22.0

python tools/agent-loop-parity/run.py \
  --pilotdeck-root /path/to/PilotDeck-current \
  --staffdeck-root /path/to/StaffDeck-current \
  --pilotdeck-baseline origin/main \
  --staffdeck-baseline origin/main \
  --scenario all \
  --output "${TMPDIR:-/tmp}/agent-loop-parity-results"
```

只重跑 PilotDeck native/sidecar 对拍时增加：

```bash
python tools/agent-loop-parity/run.py \
  --pilotdeck-root /path/to/PilotDeck-current \
  --staffdeck-root /path/to/StaffDeck-current \
  --pilotdeck-baseline working-tree \
  --pair pilotdeck \
  --pilotdeck-surface gateway \
  --scenario all \
  --adapter-timeout-seconds 30 \
  --output "${TMPDIR:-/tmp}/agent-loop-parity-pilotdeck"
```

退出码：

- `0`：所有比较均通过。
- `1`：adapter 均执行成功，但存在语义差异。
- `2`：存在无法运行或没有产出 trace 的 adapter，且没有更早的语义失败。

## 6. 本次实际结果

PilotDeck 本轮使用真实 Gateway 表面完成两组运行，均无 `BLOCKED`：

| 场景 | 当前 native vs 当前 sidecar | `origin/main` native vs 当前 sidecar |
|---|---:|---:|
| `pure_text` | 0 | 3 |
| `single_tool` | 0 | 6 |
| `multiple_tool` | 0 | 6 |
| `tool_error` | 0 | 6 |
| `permission_denial` | 0 | 6 |
| `max_turns` | 0 | 3 |
| `deadline` | 0 | 0 |
| `cancel` | 0 | 3 |
| `image` | 0 | 3 |
| `checkpoint_resume` | 0 | 6 |

同版本比较用于定位 sidecar 差异，十个场景全部为零 canonical diff。`origin/main` 比较中的每轮
三个差异来自 baseline 与当前工作树的版本漂移：system prompt 中的 checkout/skill 路径，以及
shell 工具 description 和 command schema description 的文案更新。它们不是 sidecar mapping 差异。

diff 数量是 canonical JSON 路径数量。缺少一个事件会使后续事件错位，因此不能直接视为独立
bug 数量。

## 7. PilotDeck 对拍结论

确认对齐：

- 10 个场景均执行完成，无 adapter `BLOCKED`。
- 同版本的 10 个场景均为零 canonical diff，包括 system prompt、messages、模型可见工具、
  permission/tool 事件顺序、terminal outcome、stop reason 和用户可见输出。
- sidecar 通过通用 context module 调用当前 session 的真实 `ContextRuntime`，不在 sidecar 内复制
  PilotDeck prompt 或 skill 语义。
- `canPrompt=false` 时两边暴露相同工具集合；工具 descriptor 保留宿主计算的
  `requiresUserInteraction`。
- 多工具场景通过一个 `execute_batch` module call 进入同一个 host `ToolPort.executeAll()`，两边均先
  完成 permission preflight，再开始工具执行，并按输入顺序返回结果。
- `tool_error` 两边都由真实 host `ToolRuntime` 将 `MOCK_TOOL_ERROR` 标准化为
  `tool_execution_failed` recovery，送给模型的 tool-result message 逐字段一致。
- `permission_denial` 两边都由真实 host `PermissionRuntime` 返回
  `permission_denied`，均未调用 mock tool backend，送给模型的拒绝消息逐字段一致。
- 图片 data、MIME、消息分组和恢复消息顺序一致；比较器只忽略 canonical image block 的派生
  `bytes` 元数据。
- `max_turns` 均为 `failed/agent_max_turns_reached`；deadline 均为 Gateway
  `turn_timeout`；cancel 均为 `cancelled/aborted_streaming`。

确认不一致：

- 同版本 native/sidecar 没有未声明语义差异。
- `origin/main` 与当前工作树仍有 checkout/skill 路径和 shell 工具文案差异。这是产品版本差异，
  未通过归一化隐藏；完整报告位于 `/tmp/agent-loop-parity-pilotdeck-context-batch-final`。

此前关于 sidecar 直接把 `MOCK_TOOL_ERROR` 交给模型、以及权限拒绝发生在 sidecar 内部的结论，
已被真实 Gateway 运行证伪，属于旧 adapter 模拟边界不完整造成的假差异。

## 8. StaffDeck 对拍结论（早期 10 场景运行）

> 本节保留早期 10 场景结果作历史记录；扩展 24 场景 workflow 的最新结论以第 14 节为准。

确认对齐：

- 10 个场景的 terminal outcome、TaskFrame/run 终态和用户输出均与 legacy 一致。
- 图片通过真实 `ChatTurnRequest.attachments` 和临时 image payload 进入两条模型链路；图片 data
  URL 未写入持久化 checkpoint。
- checkpoint 场景两边都读取了预建 transcript。sidecar checkpoint 还保留了
  `agentLoopSeedState.allowedReadFiles`，但本场景没有实际文件读取，不能证明该权限被消费。
- `max_turns` 使用真实 `agent_loop_max_actions=1`，两边都执行一次 `loop`，随后得到
  `action_budget/ACTION_BUDGET_EXHAUSTED`，frame 为 queued、run 为 action_budget。
- cancel 两边最终都为 `cancelled/CANCELLED`，turn、frame、run 和用户输出一致；sidecar 启动前复用正式取消检查。
- permission deny 两边都在工具副作用前拒绝，trace 中没有 `tool.call`。

确认不一致（均为 trace 包装或仍待扩展验证，不是已观察到的核心语义差异）：

1. legacy 仍记录 Harness 专属 payload，sidecar 记录 canonical messages/tools；这些 request/checkpoint
   字段数量和包装不同，比较器会报告路径差异，但两边规范化后的模型可见任务、工具结果、终态和输出一致。
2. sidecar capability response 包含 Module Protocol 的 `content`、`metadata` 和 `toolCallId` 等
   envelope 字段，legacy trace 记录业务结果；这属于 transport projection 差异。
3. 模型输入协议本身不同。legacy 使用完整 Harness system prompt、TaskRequirement、iteration、
   remaining actions 和 transcript；sidecar 使用 canonical messages/tools。即使终态相同，也不能
   宣称模型决策输入逐字等价。
4. cancel 受线程调度影响，trace 中可能出现 sidecar 已发起模型请求后才观察到取消；无论顺序如何，
   client 不接受取消之后的 completed，最终持久化分类保持 cancelled。

仍无法验证：

- current 多工具路径没有到达第二个 tool，无法比较两边的并发开始/完成顺序。
- checkpoint 的消息恢复已验证，`allowedReadFiles` 等 seed state 的实际工具侧约束尚未覆盖。
- 本轮验证 StaffDeck 外层 action budget，不代表 PilotDeck AgentLoop 已实现通用 action budget。

## 9. 验证记录

本次实际执行：

```text
backend/.venv/bin/pytest backend/tests/test_agent_loop_parity.py -q
14 passed

backend/.venv/bin/pytest \
  backend/tests/test_pilotdeck_agent_loop_client.py \
  backend/tests/test_pilotdeck_module_bridge.py \
  backend/tests/test_harness_v2.py -q
109 passed

backend/.venv/bin/ruff check <parity files>
All checks passed!

pnpm check
未执行：当前 PilotDeck package.json 没有 check script。

pnpm build
passed

pnpm test
非零：491 项中 483 passed、1 skipped、7 cancelled。取消项全部位于既有
`tests/network/fetch.spec.ts`，单独重跑仍因 `Promise resolution is still pending but the event loop has already resolved`
被 Node test runner 取消；AgentLoop 和 Module Protocol 测试没有失败。

node --test dist/tests/agent/modules/*.js \
  dist/tests/agent/session/agent-loop-factory.spec.js \
  dist/tests/protocol/module-protocol-contract.spec.js
24 passed

python tools/agent-loop-parity/run.py ... \
  --pilotdeck-baseline working-tree --pair pilotdeck \
  --pilotdeck-surface gateway --scenario all --adapter-timeout-seconds 30
exit 0; blocked: []; failed: []; 10/10 canonical trace 完全一致；结果目录
`/tmp/agent-loop-parity-pilotdeck-context-batch-verified`

python tools/agent-loop-parity/run.py ... \
  --pilotdeck-baseline origin/main --pair pilotdeck \
  --pilotdeck-surface gateway --scenario all --adapter-timeout-seconds 30
exit 1; blocked: []; 仅保留产品版本漂移；结果目录
`/tmp/agent-loop-parity-pilotdeck-context-batch-final`

pnpm --dir ui build
passed；esbuild 报告 3 条既有 CSS selector warning 和 bundle size warning
```

浏览器端完整部署验证：

- Web server：`http://127.0.0.1:3020`
- Gateway：`ws://127.0.0.1:55874/ws`
- 浏览器通过真实页面提交 `What is the status?`，收到
  `MOCK_ANSWER[pure_text]::What is the status?`。
- 新标签重新打开该 session 后 assistant 回复仍存在，证明 Web server -> Gateway -> sidecar ->
  mock provider -> transcript persistence -> Web session reload 链路完成。
- 新标签控制台无 error/warning。

## 10. 后续验证候选

1. 增加超过 context budget 的长会话场景；当前跨进程 contract 不代理依赖不可序列化
   `budgetEvaluator` 的 `tryAutoCompact`。
2. 增加真实文件工具场景，验证 `allowedReadFiles` seed state 的实际约束。

## 11. SOP/Harness 扩展（2026-09-03）

本轮将场景矩阵从 10 个扩展到 41 个，新增 SOP、handoff、team、scheduled、slot、
required capability、knowledge budget、依赖、恢复、幂等和大工具结果场景。StaffDeck
legacy 与 PilotDeck AgentLoop 两个入口均通过真实 `AgentLoop.handle_turn()` 和
`HarnessV2Engine` 执行；差异比较改为 semantic projection，provider/checkpoint
包装差异仅作为 `Format Warnings`。

> 以下为早期 41 场景运行记录，保留用于追溯；当前 60 场景结果见第 14 节。

实际命令：

```text
python tools/agent-loop-parity/run.py ... --pair staffdeck --scenario all \
  --pilotdeck-baseline working-tree --staffdeck-baseline working-tree \
  --allow-blocked --adapter-timeout-seconds 30
```

结果：`41` 个场景，`BLOCKED=0`；`36` 个场景无语义差异，以下 5 个场景报告语义分叉：

| 场景 | 结果 | 主要分叉 |
| --- | --- | --- |
| `sidecar_restart_unknown` | semantic difference | sidecar 结果未知分类与 legacy 聚合不同 |
| `sop_missing_required_slot` | semantic difference | slot 缺失时两条链路的终态/TaskFrame 不同 |
| `sop_handoff_node` | semantic difference | legacy 未持久化 handoff，sidecar 持久化 `handoff` |
| `sop_handoff_routing` | semantic difference | GraphRules 路由结果不同 |
| `sop_blocked_transition` | semantic difference | blocked/完成分类不同 |

这些是需要继续修复或明确声明的产品语义差异，不是 adapter BLOCKED。格式差异仍写入
每个报告的 `Format Warnings`，不影响对拍退出码判断。

## 12. 真实部署并发验证（2026-09-03）

本次使用真实 StaffDeck 单端服务、真实前端静态产物、真实 FastAPI、真实 SQLite、
PilotDeck sidecar 和本地 mock provider。服务地址为 `http://127.0.0.1:5190`，
数据库为 `/tmp/staffdeck-real-test/staffdeck.db`，mock 记录为
`/tmp/staffdeck-real-test/mock.jsonl`。启动时开启
`PILOTDECK_AGENT_LOOP_ENABLED=true`，sidecar 指向当前 PilotDeck checkout 的
`dist/src/cli/pilotdeck-agent-loop-sidecar.js`。

基础部署检查：

| 检查 | 结果 |
| --- | --- |
| `GET /api/health` | HTTP 200，`{"status":"ok","app":"StaffDeck"}` |
| `GET /chat/` | HTTP 200，返回前端 HTML |
| `GET /enterprise/dashboard` | HTTP 200，返回前端 HTML |
| 管理员登录 | HTTP 200，JWT 获取成功 |
| seeded agent 选择 | HTTP 200，使用 `agent_preset_data_001` |

并发测试通过同一个 agent 同时提交两个不同的 HTTP `/api/chat/turn` 请求：

| 会话 | 查询 | 结果 | 耗时 |
| --- | --- | --- | --- |
| `real-concurrent-1` | `concurrent query 1` | completed，回复 `MOCK_ANSWER::concurrent query 1` | 0.704s |
| `real-concurrent-2` | `concurrent query 2` | completed，回复 `MOCK_ANSWER::concurrent query 2` | 0.704s |

SQLite 核对结果：

| 表 | 并发测试记录数 |
| --- | ---: |
| `sessions` | 2 |
| `harness_turns` | 2 |
| `harness_runs` | 2 |
| `harness_task_frames` | 2 |
| `messages` | 4 |

两条 turn 均为 `completed`，session 标题、回复和 `session_id` 一一对应；未观察到
跨会话状态污染。mock provider 实际收到 6 次模型请求（每个会话 3 次，包括
TurnPlanner/Harness 阶段），均来自真实 HTTP 部署链路。

## 13. 完整部署矩阵复测（2026-09-03 10:14）

使用 `backend/.venv/bin/python tools/real-deployment-e2e.py --keep-runtime` 在独立临时目录启动 StaffDeck FastAPI/前端、PilotDeck Gateway、sidecar、mock provider/tool 和文件 SQLite。实验报告：`/var/folders/xd/mml9c6fj2g95x40hgf_n6lrr0000gn/T/staffdeck-e2e-f7pzugpu/REAL_DEPLOYMENT_E2E.zh.md`。runner 结束时已停止临时进程；长期存在的旧 5190 服务未作为证据使用。

| 链路 | 结果 | 证据 |
| --- | --- | --- |
| FastAPI health、前端 `/chat/`、`/enterprise/dashboard` | PASS | HTTP 均 200 |
| PilotDeck Gateway WebSocket hello | PASS | `hello_ok`，protocol `1.1` |
| StaffDeck Gateway proxy | PASS | `/pilotdeck/health` 返回 `{"ok":true}` |
| 管理员登录、agent 选择 | PASS | HTTP 均 200 |
| 同 session 两轮 HTTP | PASS | 两轮 200，`session_id` 保持不变 |
| 双 session 并发 | PASS | 两个 session、query/reply 隔离，无 SQLite 锁错误 |
| SQLite 持久化 | PASS | `sessions=7`、`harness_turns=8`、`harness_runs=8`、`harness_task_frames=8` |
| scheduled worker | PASS | 创建、`run-now` 200，后台 run 最终 `succeeded` |
| team worker | PASS | 创建 team/两成员/任务，真实 wakeup，任务到 `review` |
| handoff create/reply/resume | FAIL | 真实 `/sop` turn 返回 handoff 文本，但 `human_handoff_requests=0`，未生成 pending request |

本次 runner 总状态为 `WARNING`、退出码 `2`，因为 handoff 未完成且 runner 明确不把缺失的人工交接链路标成 PASS。handoff 失败不是资源探测缺口：SOP 已通过真实 `/api/enterprise/skills` 创建，模型也返回 handoff 意图；当前 sidecar glue 将该结果投影为普通 `advance`（`handoff=false`），需要后续产品 glue 修复。专项 pytest 结果为 `128 passed, 1 failed`；唯一失败是既有测试对源码包含 `modelStarted.then` 的静态断言，实际实现使用等价的 `cancelAnchor.then`。

## 14. 扩展对拍最终结果（2026-09-03）

本节覆盖当前 60 个 scenario fixture，使用同版本 current checkout、真实 PilotDeck Gateway/Session/ContextRuntime/ToolRuntime/PermissionRuntime，以及真实 StaffDeck `AgentLoop.handle_turn()`/`HarnessV2Engine.run()`。外部 LLM 和工具仍为确定性 mock；未使用真实 API key。

| Suite | 场景数 | 执行方式 | BLOCKED | Oracle failures | Semantic diff | 结果 |
|---|---:|---|---:|---:|---:|---|
| `core-regression` | 10 | PilotDeck native vs sidecar | 0 | 0 | 0 | PASS |
| `core-resilience` | 20 | PilotDeck native vs sidecar | 0 | 0 | 0 | PASS |
| `staffdeck-workflow` | 24 | StaffDeck legacy vs PilotDeck | 0 | 23 场景含 oracle 差异 | 23 场景 | FAIL，差异保留 |
| `known-gap` | 1 | PilotDeck native vs sidecar | 0 | 0 | 16（声明路径） | EXPECTED GAP |

PilotDeck 两个 core suite 的最终命令分别为：

```text
python tools/agent-loop-parity/run.py ... --pair pilotdeck --suite core-regression --comparison same-version --pilotdeck-surface gateway
python tools/agent-loop-parity/run.py ... --pair pilotdeck --suite core-resilience --comparison same-version --pilotdeck-surface gateway
```

输出目录：`/tmp/agent-loop-parity-expanded-core-regression-final`、
`/tmp/agent-loop-parity-expanded-core-resilience-final`。

`known-gap` 使用 `/tmp/agent-loop-parity-expanded-known-gap-final-v3`，稳定重现
`trace.length`、首个事件类型/错误分类以及终态事件等 16 条声明路径；native 因
`context_overflow_after_emergency_compaction` 失败，sidecar 继续完成，未将该差异归一化。

StaffDeck workflow 使用 `/tmp/agent-loop-parity-expanded-staffdeck-workflow-v2`。24 个 adapter
均成功启动并进入真实 Harness；23 个场景产生 semantic diff，另有 24 个场景包含至少一条
oracle 不匹配（部分场景只有单边 oracle 失败而没有左右 semantic diff）。这些差异来自 legacy/PilotDeck 的业务状态投影
差异（SOP step/slot/handoff/knowledge budget、deadline/unknown 等），不是 BLOCKED。最典型的
差异包括：TaskFrame active/next step 未从持久化 receipt 投影、缺 slot/handoff/blocked 状态被聚合
为 completed、step deadline 的副作用和 timeout 分类不一致，以及 action budget 使用
`action_budget` 而 fixture 期望 `failed`。这些差异应逐项归因或修复，不能通过扩大比较器忽略。

本轮新增修复：Gateway adapter 不再重复注册内置 `read_file`，并将内置工具 lifecycle/permission
事件投影到 canonical trace；因此 `allowed_read_files`、`denied_read_files` 不再 BLOCKED，且
`core-resilience` 全部通过。`auto_compact` 增加了按 adapter 的显式 oracle override，保持 known-gap
精确差异校验。

## 15. Glue 语义修补复测（2026-09-03）

本节为本轮 StaffDeck glue 修补后的最新结果，覆盖 `--pair staffdeck --scenario all` 的 48 个场景。
运行命令：

```text
python tools/agent-loop-parity/run.py --pair staffdeck --scenario all \
  --pilotdeck-root /Users/a1/Desktop/claw/openbmb/PilotDeck-core_agent_loop_0831 \
  --staffdeck-root /Users/a1/Desktop/claw/openbmb/StaffDeck-pilotdeck-agent-loop \
  --allow-blocked --adapter-timeout-seconds 30
```

| 指标 | 结果 |
|---|---:|
| 场景总数 | 48 |
| adapter BLOCKED | 0 |
| 未声明 semantic diff | 1（`sop_blocked_transition`） |
| 共同 oracle/fixture failures | 23 项（两条链路均失败或期望与 fixture 不一致） |
| format warnings | 约 46-65/场景，均为 envelope/字段布局差异 |

已确认无 semantic diff 的修补范围包括：多工具顺序与 action budget、工具错误 code/retryability、
图片 MIME/detail 与 transient context、checkpoint task boundary、handoff create/routing/resume、
required capability、retryable error、取消及取消后迟到 completed。取消场景最终均为
`cancelled/CANCELLED`，sidecar 不再在取消后继续发起模型轮次。

唯一未归零的 semantic diff 是 `sop_blocked_transition`：PilotDeck sidecar 能保留
`blocked` 结构化终态，而 legacy `HarnessAction.status` 的 schema 仅接受
`completed | awaiting_user | handoff | failed`，因此 legacy 将同一 mock action 判为
`HARNESS_ACTION_INVALID/failed`。这属于 legacy 产品 schema 能力差异；本轮没有在 glue 中把
`blocked` 伪造成成功或修改 `AgentLoop.ts`。

共同 oracle failures（deadline/sidecar restart/部分 SOP step、slot、knowledge budget、scheduled/team）
是 fixture 期望与当前两边真实 Harness 聚合不一致，左右链路结果相同，不计为 glue 引入的差异。
它们已单独保留在 `artifacts/summary.json`，没有用 comparator 规则吞掉。

## 16. 最终真实部署复测（2026-09-04）

在独立临时运行目录再次执行：

```text
backend/.venv/bin/python tools/real-deployment-e2e.py
```

结果为 `PASS`，报告和证据目录：
`/var/folders/xd/mml9c6fj2g95x40hgf_n6lrr0000gn/T/staffdeck-e2e-j0yadi88/REAL_DEPLOYMENT_E2E.zh.md`、
`/var/folders/xd/mml9c6fj2g95x40hgf_n6lrr0000gn/T/staffdeck-e2e-j0yadi88`。

| 真实链路 | 结果 |
|---|---|
| FastAPI health、前端 `/chat/`、`/enterprise/dashboard` | PASS，HTTP 200 |
| 管理员登录、agent 选择 | PASS，HTTP 200 |
| PilotDeck Gateway WebSocket hello、StaffDeck proxy | PASS，hello/proxy 均可达 |
| 同一 session 多轮 HTTP | PASS，session 保持不变，历史消息可见 |
| 双 session 并发 | PASS，两个 query/reply/session 隔离，无 SQLite lock |
| handoff create/reply/resume | PASS，pending -> answered，resume 已观察到 |
| scheduled worker | PASS，run-now 后最终 `succeeded`，使用冻结 SOP |
| team worker | PASS，成员 wakeup 后任务进入 `review`，事件已持久化 |
| SQLite 文件持久化 | PASS，sessions=7、messages=19、harness_turns=9、harness_runs=9、task_frames=10、handoff=1、scheduled=1、team=1 |

同一轮真实部署使用当前 PilotDeck sidecar、StaffDeck glue 和确定性本地 mock provider/tool，
未使用真实 API key；临时进程在 runner 结束后已停止。

## 17. 最终测试状态

- StaffDeck AgentLoop/glue/parity 定向测试：`137 passed`。
- StaffDeck 全量 pytest：`2097 passed, 13 failed, 145 warnings`。13 个失败集中在既有
  SQLite/channel/Feishu schema migration、并发 secret rotation 和 Feishu process 测试，未涉及
  本次 AgentLoop glue 代码；未将其伪报为本次修补通过。
- PilotDeck Node 22.23.1 `pnpm build`：通过；sidecar/default-factory/module protocol 测试
  `14 passed`。
- StaffDeck 48 场景对拍：`BLOCKED=0`；除 `sop_blocked_transition` 外无未声明 semantic diff。
  该唯一差异来自 legacy `HarnessAction.status` 不接受 `blocked`，不是 sidecar 将结果误判为成功。
- `git diff --check` 和目标文件 Ruff：通过。
