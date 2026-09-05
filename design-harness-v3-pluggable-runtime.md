# StaffDeck · 可插拔运行时（实现说明）

> 历史版本记录：当前九项修复与接口契约见 [模块化修订](design-harness-v3-modularity-repair.md)。下文旧继承关系与“已实现/待实现”标记仅描述当时的实现。

对应设计：《可插拔运行时 模块目录、装配关系与调用架构》。本文件描述**已落地**的部分、与设计的映射、怎么运行、以及尚未完成的项。

## 1. 结论先行

- 代码全部在 `backend/src/staffdeck_harness/`（并行包）。现有 `backend/app/` 只有三处开关：`Settings.harness_*` / `security_profile`（`app/config.py`）和 `AgentLoop._open_engine()`（`app/core/agent_loop.py`），以及 `app/main.py` 的 startup/shutdown 钩子与条件路由挂载（`harness_v3_enabled` 或 `harness_admin_api_enabled`）。`AgentLoop._open_engine` 在 `harness_v3_enabled` **或** `harness_admin_api_enabled` 打开时才去触碰 package（两者都关 = 纯 legacy，package 从不 import）；`harness_v3_enabled=False`（默认）时行为与主干完全一致。
- **Harness v3 引擎**（上游 `deepseek-harness 0.1.2-alpha.2`，TypeScript/Node）**以独立子进程运行**，通过官方 Python SDK 的 stdio JSON-RPC 驱动。Python 与 TS 共存：Harness v3 内核=TS（黑盒，不改），StaffDeck 与 Bridge=Python。
- 能力回调不走 SDK（协议无 server→client 请求），而是 **StaffDeck 以 MCP Server 形态把能力挂进 Harness v3 引擎**（`@deepseek-ai/dsh-mcp-client` 是引擎原生插件）。模型看到的是稳定代理工具 `mcp__staffdeck__{knowledge_search, general_skill_read, tool_invoke, sandbox_execute, capability_describe, finish_task}`。每次调用回到 Python 的 `CapabilityHost`：激活围栏 → pre_tool hooks → Ledger 回放/占位 → **宿主侧 PEP**（`CapabilityHost._pep`，未经策略映射的调用 fail-closed 拒绝）→ 提供方分发 → Ledger 回执 → post_tool hooks。
- 已用服务器数据库里的真实模型（网关 `llm-center.modelbest.co`，deepseek-v4-flash）跑通端到端：知识检索 + HTTP 工具调用 + PEP 拒绝未绑定工具 + Ledger 回执 + 引用回流；并做了 Harness v2 vs Harness v3 黄金链路对比。

## 2. 设计 → 实现映射

| 设计模块 | 类型 | 实现位置 | 说明 |
|---|---|---|---|
| Module SDK / Contracts | K | `contracts/` | `ModuleManifest`/`SlotBinding`/`SlotName`、`ModuleInvocation`/`Receipt`/`ModuleResult`、`HookContext`/`HookDecision`/`merge_decisions`、`PepPort`/`SecurityContext`/`ResourceRef`/`Decision`、错误码 |
| SecurityProfile（二选一） | K | `security/` | `OSS_LOCAL`（真实本地 RBAC+owner+binding 规则，非 no-op）、`BUSINESS_BASE`（Base authz fail-closed，pending 结算后拒绝，batch filter）。`Guard` 是每个 Host 持有的 PEP 绑定；缺失即 `PepBindingMissing` |
| StaffComposition / SOP Slot / Snapshot | C/K | `composition/` | `project_staff()` 只读投影 `AgentProfile`+绑定表；`slots.py` 声明式逻辑槽（`metadata.slots`）与隐式槽（`capability_refs`）双轨、Staff 侧 `slot_bindings` 存在 SOP 绑定行 metadata；`CompositionCompiler` 产出不可变 `CompositionSnapshot`（sha256），校验 Required Slot / SlotNotBound / 契约版本 / 子SOP 环 / Hook 环 |
| CapabilityHost + Guarded Facade + Ledger | T/K | `capabilities/` | `CapabilityHost.invoke_proxy/invoke`；`KnowledgeFacade`/`GeneralSkillFacade`/`ToolFacade`(HTTP/MCP/A2A)/`SandboxFacade` 全部复用 `KnowledgeService`/`ToolExecutor`/`HarnessExecutor`；`InvocationLedger` 复用 `harness_invocations` 表，把 `completed/failed/outcome_unknown/cancelled/denied` 状态机扩展到所有能力类型 |
| InteractionPipelineHost + Hook | K | `interactions/` | 固定 4 点 `pre_step/pre_tool/post_tool/turn_stopping`，默认 handler：persona、memory.recall、sop.execution_slice、activation.allowlist、capability.pep、ledger.record、citations.collect、sop.output_supervisor、handoff.detect；最严决策优先 |
| EngineHost + Bridge | K/T | `bridge/` | `EngineHost.open()` 按模块 id 解析 `engine.harness_v3`（`harness_v3_enabled`/`harness_admin_api_enabled` 决定是否触及包 + `harness_v3_staff_allowlist` 灰度 + `harness_v3_fallback_to_v2`），使按 Staff 的 Harness v3 选择可在部署默认为 v2 时作 canary；`HarnessV3Engine(HarnessV2Engine)` 只替换 step 执行（AgentLoop），turn claim / planner / TaskFrame 留在 StaffDeck；`worker.py` 生成 profile patch + 拉起引擎子进程；`capability_mcp.py` 是 CapabilityCallbackPort |
| Handoff Core + 可插槽 | T/A | `handoff/` | 五态状态机（兼容 legacy pending/answered/cancelled）；`AssignmentStrategy`/`Notifier`/`ReplyResolver` 三槽；回复走 legacy `_apply_handoff_reply` → 新 Turn |
| Channel Host | T | `channels/` | Receive PEP（未映射身份拒绝 + `channel.receive` + `staff.use`）与 Send PEP（`channel.send`，未监管文本拒发）包住现有 durable inbox/outbox 与适配器注册表 |
| Event Observer | A/T | `events/` | `SessionEventRelay` 把引擎 `session.event` 映射为 legacy 事件词表并扇出到 `event.observer` |

**未在本轮改动、直接复用**：TurnPlanner、TaskFrameStore、SOP CAS/`_apply_step_result`、Memory capture、ResponseGenerator、五个渠道适配器、Team 唤醒/竞标、Scheduler。它们在 `HarnessV3Engine` 继承链里原样运行。

## 3. 一次 Harness v3 Turn 的调用顺序（对应设计图二）

1. `AgentLoop.handle_turn` → `EngineHost.open` → `HarnessV3Engine.run`（= `HarnessV2Engine.run`：claim、user message、planner、TaskFrame）。
2. `HarnessV3Engine._run_frame`：首帧 `project_staff` + `CompositionCompiler.compile` → `composition_snapshot_compiled` 事件；`Guard.require(staff.use/v1)`（Staff PEP）。
3. `HarnessV3TaskAgent.run`：`ActivationSlot`（snapshot+generation+SOP 位置+allowed_next_steps）+ `LifecycleFence`；注册 activation token；`pre_step` hooks → step prompt（附件描述 + 校验过的图片 data URL 变 SDK image 内容块）。
4. 拉起 `dsh --profile sdk --patch staffdeck.patch.yml`（禁用引擎自带 fs/shell/web/subagent/todo/goal/jobs 工具，挂 `mcp__staffdeck__*`，模型路由=本 Turn 的 `ModelConfig`）。
5. 引擎 loop 内每次工具调用 → MCP → `CapabilityHost`：激活围栏（不授权，只收窄）→ **pre_tool hooks**（deny 返回 `PRE_TOOL_DENIED`，不进 ledger）→ `InvocationLedger.replay_or_block` + `start` 占位 → **宿主侧 PEP**（`CapabilityHost._pep`：tool/mcp/a2a 在活行 Tool row + 其 MCP server、general_skill、knowledge 按选中 Base、sandbox；无策略映射的操作一律拒绝，fail closed）→ `_dispatch`（按 provider pin 解析）→ 复用 legacy service → `ledger.finish` → **post_tool hooks**（`ledger.record`、`citations.collect`；`modify` 决策带 `replacement` 时换掉结果）。Guarded Facade 仍按活行复核（defence in depth）；PEP 是宿主义务，从不调用 `guard.require` 的提供方绕不过去。
6. 模型调用 `finish_task`（`task.finish/v1`）提交 `status/reply_fragment/slot_updates/next_step_id`，并关 `ActivationSlot`（`slot.closed=True`：本轮后续能力调用返回 `ACTIVATION_FENCED`，引擎 turn 循环在捕获 finish 后立即退出，子进程在 agent finally 关闭）；未调用则 `turn_stopping` hooks 可 steer 一次，否则按 required_slots 推断。
7. 返回 `TaskExecutionResult` → 继承的 legacy 后处理：`_enforce_required_slots`、`_apply_step_result`（SOP CAS）、handoff、memory capture、response、`_finalize_turn`（渠道 outbox）。

## 4. 运行

```bash
# 一次性：构建上游 deepseek-harness 0.1.2-alpha.2（需 pnpm、node>=22.19）
cd .codex-tmp/harness-v3-engine/deepseek-harness-0.1.2-alpha.2
pnpm install --frozen-lockfile && pnpm run build:lib:host && pnpm run build:lib:client
# 注意：postinstall 会向仓库根写 lefthook.yml 与 .git/hooks/prepare-commit-msg，需删除

# backend/.env
HARNESS_V3_ENABLED=true
HARNESS_V3_ROOT=/abs/path/to/deepseek-harness-0.1.2-alpha.2   # 引擎 checkout（dev stack 示例见 .codex-tmp/harness-v3-engine/deepseek-harness-0.1.2-alpha.2）
HARNESS_V3_HOME=/abs/path/to/staffdeck-harness-v3-home        # 部署自有 home，绝不使用 ~/.dsh
HARNESS_V3_STAFF_ALLOWLIST=agent_xxx                    # 可选：单 Staff 灰度
SECURITY_PROFILE=OSS_LOCAL                       # 或 BUSINESS_BASE（需 BASE_AUTHZ_URL/BASE_AUTHZ_DECISION_TOKEN）

# 测试
cd backend
.venv/bin/python -m pytest tests_harness -q                         # 单测（无外部依赖）
set -a; source ../.codex-tmp/server-models.env; set +a
HARNESS_V3_E2E=1 .venv/bin/python -m pytest tests_harness/test_e2e_harness_v3_turn.py tests_harness/test_golden_v2_vs_v3.py -q -s
```
集成测试调用真实模型网关，默认跳过；显式设 `HARNESS_V3_E2E=1` 才运行（因为该二题模型会因检索措辞/动作预算产生非确定性，仅结构不变量被断言）。

`staffdeck_harness` 通过 `backend/.venv/lib/python3.12/site-packages/staffdeck_harness.pth` 加入 sys.path（等价于把 `backend/src` 加进 `pyproject` 的 packages；正式化时在 `pyproject.toml` 加 `[tool.setuptools.packages.find] where=["src", "."]`）。

## 5. 验收标准对照（设计第七节）

| 验收项 | 状态 | 证据 |
|---|---|---|
| A 模块只依赖 Module SDK | ✅ | `contracts/` 无 ORM import；Facade 是唯一触库层 |
| Required Slot 缺失/契约不兼容/依赖环/Hook 环拒绝发布 | ✅ | `test_composition.py` |
| 同一 SOP 挂两个 Staff 绑不同 Knowledge/Tool | ✅ | `test_same_sop_two_staffs_different_bindings` |
| 缺 PEP Binding 无法启动 | ✅ | `Guard(...)` 抛 `PepBindingMissing` |
| OSS/Business 同一套测试只换 SecurityProfile | ✅ | `test_security_profiles.py` 两个 PEP 同接口 |
| Business Base 故障 fail closed | ✅ | `test_business_base_fails_closed_no_local_fallback` |
| Tool 对模型可见但权限被撤销时仍拒绝执行 | ✅ | 宿主侧 PEP 用 `live_resource_ref` 复核；`test_harness_v3_turn_denies_unbound_tool` |
| Bridge 不保存 Token/Secret、不拥有 SOP/Knowledge/Channel/Handoff 状态 | ✅ | 密钥经 env 传给子进程；patch 文件用 `!!js process.env.*` |
| SOP 前处理/in-loop Hook/后处理同一 Snapshot | ✅ | `ActivationSlot.snapshot` 贯穿 |
| Handoff Reply 必须开新 Turn | ✅ | `HandoffCore.reply` → legacy `_apply_handoff_reply` → 异步 resume |
| Channel 不得直接发送未监管文本 | ✅ | `ChannelHost.stage_send(supervised=False)` 拒绝 |
| Harness v2 vs Harness v3 黄金链路对比 | 🟡 | 普通对话+Knowledge+Tool 已对比（`test_golden_v2_vs_v3.py`）；SOP/子SOP/Team/Scheduler/Handoff/Channel/Memory/Artifact/Sandbox/取消/恢复待补 |

## 6. 本轮新增（2026-09-03）

- **Module Registry**（`modules/`）：所有可插拔物（能力、Hook、Handoff 槽、渠道适配器、Observer、引擎、安全配置）以 `ModuleManifest` 登记，`seal()` 时校验 PEP-bound 插槽、契约版本、单提供者插槽、未满足的 `requires_operations`；内置 24 个模块，可在 `seal` 前用 `harness_disabled_modules` 禁用，第三方经 entry point `staffdeck_harness.modules` 接入。`CapabilityHost`/`EngineHost`/`InteractionPipelineHost`/`HandoffCore` 均从注册表解析，而非硬编码 import。
- **管理 API**（`api/admin.py`，`/api/enterprise/harness/*`）：`status`、`modules`、`snapshot`（编译一份 Staff 组成快照，含逻辑槽与引擎代理工具）、`ledger/unknown|recent` + `ledger/{id}/reconcile`、`staff/{id}/engine`（按员工持久化 `metadata_json.execution_engine` 选择引擎，配合部署默认与灰度名单，下一 Turn 生效）、`events/recent`。`/ledger/recent`、`/events/recent`、`/log` 对租户管理员或该会话拥有者（`ChatSession.user_id`）可读；非管理员 `arguments` 做 `<redacted>` 脱敏（保留键名）；`/sessions/recent`、`/ledger/unknown`、`/audit` 仅管理员。
- **前端**（`frontend-enterprise`）：新增「运行时与插件」管理页（总览/模块装配/组成快照/调用台账），`HarnessRuntimePage`；侧边栏 + 路由 + API client；聊天 trace 渲染新增 Harness v3/PEP 事件行（`composition_snapshot_compiled`、`harness_v3_process_started`、`capability_provider_selected`、`capability_denied`、`harness_v3_task_finished`）。部署到 `http://127.0.0.1:5173`（`scripts/dev_up.sh` 单端口，`frontend-enterprise/dist` 已构建）。
- **真机验证**：服务器网关 `llm-center.modelbest.co`（GLM-5.2 key 可服务 deepseek-v4-flash）经 Harness v3 完成知识检索 Turn，回复带 `[2]`/`[3]` 引用；Ledger 记录 `knowledge:knowledge.search/v1 completed engine=harness_v3`；事件流 `composition_snapshot_compiled → harness_v3_process_started → … → capability_invoked`。

### 6.1 大模块 / 小模块（2026-09-03 晚）

参考架构图的八个业务模块落成 `modules/taxonomy.py`（数据，不是代码路径）：`BigModule → SubModule → 注册表插槽/模块 id`。`GET /api/enterprise/harness/modules/tree` 把扁平注册表合并进这棵树。新增 `modules/kernel.py`：把 K/T 类内核件（Persona 投影、模型路由、组成投影/编译器、SOP 定义/槽解析/运行态、Handoff Core、取消恢复、团队委派、Web/开放接口/定时入口、Channel Host、Runtime Coordinator、Harness v3 内核、Invocation Ledger）也登记为模块——**不可替换但可禁用、可校验、可见**。注册表由 24 → 41 个模块，8 大模块 / 26 子模块，每个子模块至少一个实现（`test_taxonomy_tree_covers_every_registered_module_once` 保证每个模块恰好出现一次且无空叶子）。新增 `runtime.kernel` 插槽承载信息性内核件，避免污染单提供者的 `runtime.engine`。

前端「运行时与插件」页重做为应用原生风格（AppHeader + StatCard + 白色圆角面板 + DataTable，Tab 用员工档案页的上浮样式），`ModuleTree` 组件渲染 大模块（编号/PEP/计数）→ 子模块（A/C/T/K 徽标/描述）→ 插件（状态点/id/版本/PEP/展开看插槽、契约、能力、策略动作、Hook），并展示模块间调用关系边。

### 6.2 管理后台 `/admin`、动态装配与执行日志（2026-09-03）

- **页面搬到独立链接** `http://127.0.0.1:5173/admin`（`frontend-enterprise/src/pages/admin/AdminPage.tsx`，无侧边栏，仅管理员）；原 `/enterprise/harness-runtime` 删除，侧边栏「管理后台」指向新链接。`single_port_app.py` 增加 `/admin` 的 SPA 路由。
- **面向使用者的文案**：不再出现 PEP/插槽/契约/Hook/A-C-T-K 等开发词。执行引擎统一称 **Harness v3 引擎**（可插拔运行时桥接）与 **Harness v2 引擎**（内置）；权限模式称「开源版 · 本地权限」/「企业版 · 统一权限中心」；模块类型标为 可替换 / 可配置 / 平台服务 / 核心；操作名、Hook、插槽、工具名都有中文映射（`src/lib/harnessLabels.ts`）。技术标识只在「显示技术信息」开关打开时补充展示。后端 `manifest(..., summary=)` 为每个模块提供一句话说明，`taxonomy.py` 描述改为业务语言。
- **动态装配**（`backend/src/staffdeck_harness/modules/config.py` + `runtime/assembly.py`）：管理员在页面上开关模块、选择引擎/权限模式、接入外部模块（`pkg.mod:register`），保存到 `<harness_v3_home>/staffdeck-runtime.json`（`harness_runtime_config_path` 可指定）；`GET/PUT /api/enterprise/harness/config` 报告 saved / applied / pending；`POST /restart` 在进程内重建注册表、安全配置和引擎子进程（`restart_harness_runtime(..., drain_timeout_seconds=20)`，先做有界等待在途 Turn（`ActivationRegistry` 长度），返回 `interrupted_turns`），失败自动回滚到上一套装配并返回 409。核心（K）模块与引擎/PEP 槽不能通过停用列表关闭，只能二选一。启动时 `start_harness_runtime` 先把保存的装配投影到 `Settings`，所以 `AgentLoop._open_engine` 无需改动即可跟随切换。
- **执行日志**（替代「调用台账」）：`GET /sessions/recent` + `GET /log?session_id=` 把 `agent_events` 与 `harness_invocations` 合并成引擎风格的时间线（`user/message`、`snapshot/compiled`、`engine/start`、`tool/call`、`tool/result`、`tool/denied`、`llm/call`、`assistant/message`…），前端 `SessionLog` 一行一事、可筛选（能力调用/引擎/对话/模型调用/仅问题）、可展开原始数据、可自动刷新；`outcome_unknown` 的人工确认收进折叠横幅。MCP 工作线程上缓冲的 trace 事件现在带 `occurred_at`，日志按真实发生时间排序。

### 6.3 管理后台第二轮：一致性测试、企业权限切换、外部模块归类、接口方案（2026-09-03）

- **接口方案**：`design-harness-v3-module-interface-spec.md`（同目录）是模块接入的约束性规范：接口总览表、Manifest JSON Schema、三种接入方式（进程内 Python SPI【已实现】 / MCP Streamable HTTP 远程能力提供者【待实现】 / HMAC Webhook【待实现】）、逐插槽接入矩阵、PEP 约定、幂等与 Ledger 状态机、事件、生命周期、错误码表、示例。
- **开关一致性**：两轮 Playwright 全量回归（`.codex-tmp/shots/t1-modules.mjs`、`t2-admin.mjs`，94/95 通过；剩余为测试期望问题）。修复：全部展开下单卡可收起；引擎核心行"随 Harness v3 启用/停用中"取代矛盾标签；引擎行统一"二选一"标签；平台服务（T）默认不可停用（`sandbox.local` 例外 `metadata.switchable`），核心（K）在注册表层面就忽略停用；`PUT /config` 拒绝未知 / 不可停用模块；开关乐观更新 + 串行保存队列（连点不丢）；计数徽标显示"→ 重启后数量"；重启进度横幅；Harness v2 时 `runtime_ok=true`。
- **企业权限切换真正可用**：管理台可填权限中心地址 / 决策令牌 / 控制令牌 / 身份中心凭证（密钥 `encrypt_secret` 加密落盘，读接口只回掩码与来源）；`POST /base/test` 只读连接测试（health / ready / `agents/public/list` 验令牌 / oauth/token / 本租户同步状态，全部中文结论）；`PUT /config` 切企业版前要求地址 + 令牌；重启前 `preflight_assembly` 实时测连接并在一次性注册表上 seal，不通过则**不碰运行时**；启动时保存的装配无法构建则回退部署默认值而不是拒绝启动。地址策略（`BASE_URL_ALLOWLIST`，默认仅内网 / 本机，公网需 https）与密钥配对规则（新地址必须带自己的密钥）防止密钥外泄。按企业服务器真实契约修正 `business_base.py`：pending 由 `reason=authorization_pending` 而非 409 表达；Base 不认识的运行时内部类型本地评估、未知类型拒绝；`general_skill → skill`、`knowledge_base use → retrieve`；id 单射编码；401/403 判为配置错误；5xx / 传输错误重试一次；工作负载凭证改为 oauth/token → workload-delegation-check → workload-delegations → workload-sessions。
- **外部模块归类**：`metadata.category`（模块作者意图）+ 管理员覆盖 `placements`（`staffdeck-runtime.json`，纯展示，立即生效，不触发 pending）；精确优先级 override > 平台预设 > 模块自带分类 > 按接入点 > 未归类（`taxonomy.resolve_placement`）。`POST /modules/inspect` 在一次性注册表里试装规格串，返回会安装的模块、归类、错误与警告（冲突 / 被遮蔽 / 被停用），前端"预检"卡片里就能选类目；外部模块行带"外部"标签，移除后显示"重启后移除"。`Installed.source/spec` 由 `installing_from()` 自动记录。
- **运行时健壮性**（评审发现）：`get_registry()` 无 settings 不再懒构建默认装配（`peek_registry()` 供宿主 / Relay / Hook 使用），重启改为"局部构建 → 原子切换"，重启窗口内能力调用得到 `ENGINE_UNAVAILABLE` 而不是落到默认引擎；`snapshot_env` 使"留空回落到环境"不再被已应用的管理员值污染；`_state_lock` 不再跨网络 I/O，`/status` 在重启中可用并带 `restarting`；`build_handoff_core` 按能力挑选槽实现；`ledger.invocation` 实现 `on_event`；钉钉 / 微信通知器如实标注"暂不支持主动私聊"并默认停用；SOP 绑定按 Skill 行 id 查询；审计事件（`assembly_saved` / `base_connection_tested` / `module_placed` / `module_inspected` / `runtime_restart_*`）+ `GET /audit`，执行日志页新增「管理操作记录」。
- **每模块测试**：`backend/tests_harness/modules/`，41 个模块各一个文件（manifest / 归类 / 停用语义 / 离线 provider 行为 / PEP），共享 fixtures 在 `conftest.py`；全套 `tests_harness` 426 passed。

### 6.4 模型调用走 StaffDeck 模型模块；知识库引用回流（2026-09-03）

- **模型网关**（`bridge/model_gateway.py`）：Harness v3 子进程的 `baseURL` 指向桥接层自己的 `/v1/chat/completions`（与能力 MCP 同一个 127.0.0.1 端口），API key 就是本 Turn 的激活令牌。请求由 `app.llm.client.LLMClient` 按该 Turn 解析出的 ModelConfig 执行：协议驱动、提供方密钥、思考策略（`extra_body.thinking` / `reasoning_effort`）、温度、输出上限、`llm_call_*` 观测都与 Harness v2 完全一致；提供方凭证不再进入 Node 进程（`HarnessV3WorkerConfig.model_api_key` 现在只是激活令牌，`DEEPSEEK_*` 透传已删）。引擎自带的 `reasoningEffort` 默认值因此不再起作用。目前只有 `openai_chat_completions` 协议的模型能承接工具调用，其他协议返回 400（`UNSUPPORTED_PROTOCOL`）并提示改用 Harness v2。
- **附件（v3 路径）**：附件描述（filename/kind/size/sandbox 路径，绝不内联 data URL）渲染进 step prompt；校验过的图片 data URL 转成 SDK 图片内容块 `{type:"image", data, mimeType}`，进 `session_prompt`。
- **引用回流**：知识检索的完整响应约 30 KB，超过引擎的 50 KB 内联预算时会被 spill 成文件（模型只拿到路径提示，我们从会话日志也解析不到）。现在 `KnowledgeFacade` 给模型的是**精简视图**（带 `[N]` 标签的证据摘录，≤8 条 × 1200 字），完整响应放在 `ModuleResult.extensions.evidence`；`CapabilityHost` 在工具返回时直接累积 `citations` / `evidence` 并发出 `knowledge_result`，`HarnessV3TaskAgent._result` 优先使用宿主收集的引用，不再依赖引擎转录。助手消息的 `metadata.knowledge_citations` 与 `execution_engine`（现取自本轮实际引擎）随之正确，前端引用卡片恢复。
- 验证：人事「年假怎么申请」→ Harness v3 检索人事知识库，回答带 `[1]`，消息元数据 1 条引用；法务「违约金条款风险」→ 4 条引用；同一员工切到 Harness v2 → 4 条引用；两者事件 `execution_engine` 一致。

## 7. 已知差异与后续

- **边界**：`HarnessV3Engine` 继承 `HarnessV2Engine` —— Harness v3 只替换 *step 执行*（AgentLoop），turn claim / planner / TaskFrame / SOP CAS / handoff / memory 仍在 StaffDeck；这是计划中的边界。
- **Business Base PEP 只覆盖 Harness v3 能力路径**：legacy `backend/app` 业务端点不经过它（不在可插拔运行时范围内，`backend/app` 只在三处开关被触碰）；`BasePep` 把 session/handoff/channel/model_config/runtime/capability/tenant 路由到本地规则（Base 天然拒绝这些类型）。
- **取消**：上游 deepseek-harness 0.1.2-alpha.2 协议无 cancel；`HarnessV3TaskAgent` 在事件循环里以 0.25 s 轮询 `is_chat_turn_cancelled` 与 SDK 通知队列，命中后抛 `HarnessExecutionCancelled` 并关闭子进程（进程级中断），legacy 的取消回执路径原样生效。
- **进程模型**：每个 TaskFrame 一个引擎子进程（~1s 启动），因为 MCP 激活 token 是插件加载时从 env 读的。后续改为进程池 + 每帧换绑（进程内串行，避免改引擎）。
- **Business 端**：`BUSINESS_BASE` 的 Identity/Workload 与企业版 `app/trust/` 线协议对齐但未在企业库上跑；接入时把 `BaseTrustClient` 适配到 `BaseAuthzClient` 接口即可。
- **黄金链路**：目前已覆盖普通对话 + Knowledge + Tool；SOP/子SOP/Team/Scheduler/Handoff/Channel/Memory/Artifact/Sandbox/取消/恢复的对比仍待补。观察到 legacy 同请求因 action budget 未一轮完成而 Harness v3 一轮完成（已记为 divergence）。
- **生产化**：`staffdeck_harness` 正式纳入 `pyproject`；`HarnessV3Runtime` 挂到 app lifespan（`runtime/assembly.py` 已提供 `start_harness_runtime/stop_harness_runtime/harness_health`）；`sandbox_execute` 目前跟随 `UIConfig.sandbox_*`，Business 远程 sandbox 需接 `sandbox_manager_url`。
# 当前版本说明

本文件为原始实现记录；当前九项审查修复及模块接入方式见 [模块化修订](design-harness-v3-modularity-repair.md)。下文的继承、内存替换与待实现描述不代表修复后的状态。
