# StaffDeck × DSH — 可插拔运行时（实现说明）

对应设计：《StaffDeck × DSH 模块目录、装配关系与调用架构》。本文件描述**已落地**的部分、与设计的映射、怎么运行、以及尚未完成的项。

## 1. 结论先行

- 代码全部在 `backend/src/staffdeck_dsh/`（并行包）。现有 `backend/app/` 只有三处开关：`Settings.dsh_*` / `security_profile`（`app/config.py`）和 `AgentLoop._open_engine()`（`app/core/agent_loop.py`），以及 `app/main.py` 的 startup/shutdown 钩子与条件路由挂载（`dsh_enabled` 或 `dsh_admin_api_enabled`）。`dsh_enabled=False`（默认）时行为与主干完全一致。
- DSH（`deepseek-harness 0.1.2-alpha.2`，TypeScript/Node）**以独立子进程运行**，通过官方 Python SDK 的 stdio JSON-RPC 驱动。Python 与 TS 共存：DSH 内核=TS（黑盒，不改），StaffDeck 与 Bridge=Python。
- 能力回调不走 SDK（协议无 server→client 请求），而是 **StaffDeck 以 MCP Server 形态把能力挂进 DSH**（`@deepseek-ai/dsh-mcp-client` 是 DSH 原生插件）。模型看到的是稳定代理工具 `mcp__staffdeck__{knowledge_search, general_skill_read, tool_invoke, sandbox_execute, capability_describe, finish_task}`。每次调用回到 Python 的 `CapabilityHost`：激活围栏 → Ledger 幂等 → Guarded Facade（PEP + 活行复核 + 复用现有服务）→ Ledger 回执。
- 已用服务器数据库里的真实模型（网关 `llm-center.modelbest.co`，deepseek-v4-flash）跑通端到端：知识检索 + HTTP 工具调用 + PEP 拒绝未绑定工具 + Ledger 回执 + 引用回流；并做了 Legacy vs DSH 黄金链路对比。

## 2. 设计 → 实现映射

| 设计模块 | 类型 | 实现位置 | 说明 |
|---|---|---|---|
| Module SDK / Contracts | K | `contracts/` | `ModuleManifest`/`SlotBinding`/`SlotName`、`ModuleInvocation`/`Receipt`/`ModuleResult`、`HookContext`/`HookDecision`/`merge_decisions`、`PepPort`/`SecurityContext`/`ResourceRef`/`Decision`、错误码 |
| SecurityProfile（二选一） | K | `security/` | `OSS_LOCAL`（真实本地 RBAC+owner+binding 规则，非 no-op）、`BUSINESS_BASE`（Base authz fail-closed，pending 结算后拒绝，batch filter）。`Guard` 是每个 Host 持有的 PEP 绑定；缺失即 `PepBindingMissing` |
| StaffComposition / SOP Slot / Snapshot | C/K | `composition/` | `project_staff()` 只读投影 `AgentProfile`+绑定表；`slots.py` 声明式逻辑槽（`metadata.slots`）与隐式槽（`capability_refs`）双轨、Staff 侧 `slot_bindings` 存在 SOP 绑定行 metadata；`CompositionCompiler` 产出不可变 `CompositionSnapshot`（sha256），校验 Required Slot / SlotNotBound / 契约版本 / 子SOP 环 / Hook 环 |
| CapabilityHost + Guarded Facade + Ledger | T/K | `capabilities/` | `CapabilityHost.invoke_proxy/invoke`；`KnowledgeFacade`/`GeneralSkillFacade`/`ToolFacade`(HTTP/MCP/A2A)/`SandboxFacade` 全部复用 `KnowledgeService`/`ToolExecutor`/`HarnessExecutor`；`InvocationLedger` 复用 `harness_invocations` 表，把 `completed/failed/outcome_unknown/cancelled/denied` 状态机扩展到所有能力类型 |
| InteractionPipelineHost + Hook | K | `interactions/` | 固定 4 点 `pre_step/pre_tool/post_tool/turn_stopping`，默认 handler：persona、memory.recall、sop.execution_slice、activation.allowlist、capability.pep、ledger.record、citations.collect、sop.output_supervisor、handoff.detect；最严决策优先 |
| EngineHost + Bridge | K/T | `bridge/` | `EngineHost.open()` 选引擎（`dsh_enabled` + `dsh_staff_allowlist` 灰度 + `dsh_fallback_to_legacy`）；`DshEngine(HarnessV2Engine)` 只替换 `task_agent`；`DshTaskAgent.run()` 与 `HarnessTaskAgent.run()` 同签名同返回；`worker.py` 生成 profile patch + 拉起 `dsh`；`capability_mcp.py` 是 CapabilityCallbackPort |
| Handoff Core + 可插槽 | T/A | `handoff/` | 五态状态机（兼容 legacy pending/answered/cancelled）；`AssignmentStrategy`/`Notifier`/`ReplyResolver` 三槽；回复走 legacy `_apply_handoff_reply` → 新 Turn |
| Channel Host | T | `channels/` | Receive PEP（未映射身份拒绝 + `channel.receive` + `staff.use`）与 Send PEP（`channel.send`，未监管文本拒发）包住现有 durable inbox/outbox 与适配器注册表 |
| Event Observer | A/T | `events/` | `SessionEventRelay` 把 DSH `session.event` 映射为 legacy 事件词表并扇出到 `event.observer` |

**未在本轮改动、直接复用**：TurnPlanner、TaskFrameStore、SOP CAS/`_apply_step_result`、Memory capture、ResponseGenerator、五个渠道适配器、Team 唤醒/竞标、Scheduler。它们在 `DshEngine` 继承链里原样运行。

## 3. 一次 DSH Turn 的调用顺序（对应设计图二）

1. `AgentLoop.handle_turn` → `EngineHost.open` → `DshEngine.run`（= `HarnessV2Engine.run`：claim、user message、planner、TaskFrame）。
2. `DshEngine._run_frame`：首帧 `project_staff` + `CompositionCompiler.compile` → `composition_snapshot_compiled` 事件；`Guard.require(staff.use/v1)`（Staff PEP）。
3. `DshTaskAgent.run`：`ActivationSlot`（snapshot+generation+SOP 位置+allowed_next_steps）+ `LifecycleFence`；注册 activation token；`pre_step` hooks → step prompt。
4. 拉起 `dsh --profile sdk --patch staffdeck.patch.yml`（禁用 DSH 自带 fs/shell/web/subagent/todo/goal/jobs 工具，挂 `mcp__staffdeck__*`，模型路由=本 Turn 的 `ModelConfig`）。
5. DSH loop 内每次工具调用 → MCP → `CapabilityHost`：围栏（不授权，只收窄）→ `InvocationLedger.replay_or_block` → Facade（**PEP 用活行**，撤权即拒）→ 复用 legacy service → `ledger.finish`。
6. 模型调用 `finish_task` 提交 `status/reply_fragment/slot_updates/next_step_id`；未调用则 `turn_stopping` hooks 可 steer 一次，否则按 required_slots 推断。
7. 返回 `TaskExecutionResult` → 继承的 legacy 后处理：`_enforce_required_slots`、`_apply_step_result`（SOP CAS）、handoff、memory capture、response、`_finalize_turn`（渠道 outbox）。

## 4. 运行

```bash
# 一次性：构建 DSH（需 pnpm、node>=22.19）
cd .codex-tmp/dsh-inspect/deepseek-harness-dsh-v0.1.2-alpha.2
pnpm install --frozen-lockfile && pnpm run build:lib:host && pnpm run build:lib:client
# 注意：DSH postinstall 会向仓库根写 lefthook.yml 与 .git/hooks/prepare-commit-msg，需删除

# backend/.env
DSH_ENABLED=true
DSH_ROOT=/abs/path/to/deepseek-harness-dsh-v0.1.2-alpha.2
DSH_HOME=/abs/path/to/staffdeck-dsh-home        # 部署自有 home，绝不使用 ~/.dsh
DSH_STAFF_ALLOWLIST=agent_xxx                    # 可选：单 Staff 灰度
SECURITY_PROFILE=OSS_LOCAL                       # 或 BUSINESS_BASE（需 BASE_AUTHZ_URL/BASE_AUTHZ_DECISION_TOKEN）

# 测试
cd backend
.venv/bin/python -m pytest tests_dsh -q                         # 单测（无外部依赖）
set -a; source ../.codex-tmp/server-models.env; set +a
DSH_E2E=1 .venv/bin/python -m pytest tests_dsh/test_e2e_dsh_turn.py tests_dsh/test_golden_legacy_vs_dsh.py -q -s
```
集成测试调用真实模型网关，默认跳过；显式设 `DSH_E2E=1` 才运行（因为该二题模型会因检索措辞/动作预算产生非确定性，仅结构不变量被断言）。

`staffdeck_dsh` 通过 `backend/.venv/lib/python3.12/site-packages/staffdeck_dsh.pth` 加入 sys.path（等价于把 `backend/src` 加进 `pyproject` 的 packages；正式化时在 `pyproject.toml` 加 `[tool.setuptools.packages.find] where=["src", "."]`）。

## 5. 验收标准对照（设计第七节）

| 验收项 | 状态 | 证据 |
|---|---|---|
| A 模块只依赖 Module SDK | ✅ | `contracts/` 无 ORM import；Facade 是唯一触库层 |
| Required Slot 缺失/契约不兼容/依赖环/Hook 环拒绝发布 | ✅ | `test_composition.py` |
| 同一 SOP 挂两个 Staff 绑不同 Knowledge/Tool | ✅ | `test_same_sop_two_staffs_different_bindings` |
| 缺 PEP Binding 无法启动 | ✅ | `Guard(...)` 抛 `PepBindingMissing` |
| OSS/Business 同一套测试只换 SecurityProfile | ✅ | `test_security_profiles.py` 两个 PEP 同接口 |
| Business Base 故障 fail closed | ✅ | `test_business_base_fails_closed_no_local_fallback` |
| Tool 对模型可见但权限被撤销时仍拒绝执行 | ✅ | Facade 用 `live_resource_ref` 复核；`test_dsh_turn_denies_unbound_tool` |
| Bridge 不保存 Token/Secret、不拥有 SOP/Knowledge/Channel/Handoff 状态 | ✅ | 密钥经 env 传给子进程；patch 文件用 `!!js process.env.*` |
| SOP 前处理/in-loop Hook/后处理同一 Snapshot | ✅ | `ActivationSlot.snapshot` 贯穿 |
| Handoff Reply 必须开新 Turn | ✅ | `HandoffCore.reply` → legacy `_apply_handoff_reply` → 异步 resume |
| Channel 不得直接发送未监管文本 | ✅ | `ChannelHost.stage_send(supervised=False)` 拒绝 |
| Legacy vs DSH 黄金链路对比 | 🟡 | 普通对话+Knowledge+Tool 已对比（`test_golden_legacy_vs_dsh.py`）；SOP/子SOP/Team/Scheduler/Handoff/Channel/Memory/Artifact/Sandbox/取消/恢复待补 |

## 6. 本轮新增（2026-09-03）

- **Module Registry**（`modules/`）：所有可插拔物（能力、Hook、Handoff 槽、渠道适配器、Observer、引擎、安全配置）以 `ModuleManifest` 登记，`seal()` 时校验 PEP-bound 插槽、契约版本、单提供者插槽、未满足的 `requires_operations`；内置 24 个模块，可在 `seal` 前用 `dsh_disabled_modules` 禁用，第三方经 entry point `staffdeck_dsh.modules` 接入。`CapabilityHost`/`EngineHost`/`InteractionPipelineHost`/`HandoffCore` 均从注册表解析，而非硬编码 import。
- **管理 API**（`api/admin.py`，`/api/enterprise/dsh/*`）：`status`、`modules`、`snapshot`（编译一份 Staff 组成快照，含逻辑槽与 DSH 代理工具）、`ledger/unknown|recent` + `ledger/{id}/reconcile`、`staff/{id}/engine`（按员工持久化 `metadata_json.execution_engine` 选择引擎，配合部署默认与灰度名单，下一 Turn 生效）、`events/recent`。
- **前端**（`frontend-enterprise`）：新增「运行时与插件」管理页（总览/模块装配/组成快照/调用台账），`DshRuntimePage`；侧边栏 + 路由 + API client；聊天 trace 渲染新增 DSH/PEP 事件行（`composition_snapshot_compiled`、`dsh_process_started`、`capability_provider_selected`、`capability_denied`、`dsh_task_finished`）。部署到 `http://127.0.0.1:5173`（`scripts/dev_up.sh` 单端口，`frontend-enterprise/dist` 已构建）。
- **真机验证**：服务器网关 `llm-center.modelbest.co`（GLM-5.2 key 可服务 deepseek-v4-flash）经 DSH 完成知识检索 Turn，回复带 `[2]`/`[3]` 引用；Ledger 记录 `knowledge:knowledge.search/v1 completed engine=dsh`；事件流 `composition_snapshot_compiled → dsh_process_started → … → capability_invoked`。

## 7. 已知差异与后续

- **取消**：DSH 0.1.2 协议无 cancel；`DshTaskAgent` 在事件循环里轮询 `is_chat_turn_cancelled`，命中后抛 `HarnessExecutionCancelled` 并关闭子进程（进程级中断），legacy 的取消回执路径原样生效。
- **进程模型**：每个 TaskFrame 一个 `dsh` 子进程（~1s 启动），因为 MCP 激活 token 是插件加载时从 env 读的。后续改为进程池 + 每帧换绑（进程内串行，避免改 DSH）。
- **Business 端**：`BUSINESS_BASE` 的 Identity/Workload 与企业版 `app/trust/` 线协议对齐但未在企业库上跑；接入时把 `BaseTrustClient` 适配到 `BaseAuthzClient` 接口即可。
- **黄金链路**：目前已覆盖普通对话 + Knowledge + Tool；SOP/子SOP/Team/Scheduler/Handoff/Channel/Memory/Artifact/Sandbox/取消/恢复的对比仍待补。观察到 legacy 同请求因 action budget 未一轮完成而 DSH 一轮完成（已记为 divergence）。
- **生产化**：`staffdeck_dsh` 正式纳入 `pyproject`；`DshRuntime` 挂到 app lifespan（`runtime/assembly.py` 已提供 `start_dsh_runtime/stop_dsh_runtime/dsh_health`）；`sandbox_execute` 目前跟随 `UIConfig.sandbox_*`，Business 远程 sandbox 需接 `sandbox_manager_url`。
