# StaffDeck 可插拔运行时 · 模块接口协议规范（v1）

> 历史版本说明：本文件保留原 v1 设计与当时的实现状态。当前 SDK、分层绑定、权限及生命周期契约见 [模块化修订](design-harness-v3-modularity-repair.md)；两者冲突时以后者和代码测试为准。

> 适用范围：`backend/src/staffdeck_harness`（Harness v3 引擎桥接 + 模块注册表 + 管理后台 `/admin`）。本文是**约束性契约**：任何模块（内置或第三方）要接入 StaffDeck 运行时，必须遵守这里写死的接入方式、报文形状与约定。
>
> - 每条条款标注 **【已实现】** 或 **【待实现】**。【已实现】条款以当前代码为准，出处写成 `文件路径 · 符号名`（函数/类/常量），不写行号；【待实现】条款给出精确设计，供后续按此实现。
> - 关键词 MUST / MUST NOT / SHOULD / MAY 按 RFC 2119 理解。
> - 术语沿用管理后台文案：Harness v3 引擎 = `engine.harness_v3`（可插拔运行时桥接），Harness v2 引擎 = `engine.harness_v2`（内置）；权限模式：开源版 · 本地权限 = `OSS_LOCAL`，企业版 · 统一权限中心 = `BUSINESS_BASE`。
> - 配套文档：`design-harness-v3-pluggable-runtime.md`（实现说明）。

---

## 0. 接口总览

| # | 接口 | 谁调用谁 | 协议 | 鉴权 | 幂等 / 重试 | 状态 |
|---|---|---|---|---|---|---|
| 1 | 模块注册 `register(registry, ctx)` | 装配（`modules/registry.py · discover_and_install`）→ 模块包 | 进程内 Python：entry point 组 `staffdeck_harness.modules` 或规格串 `pkg.mod:register` | 进程信任；只有租户管理员能通过 `/admin` 添加规格串 | 每次 restart 重跑，MUST 幂等；失败即整次装配失败 | 【已实现】 |
| 2 | 能力提供者 `invoke(host, inv) -> ModuleResult` | `capabilities/host.py · CapabilityHost._dispatch` → provider | 进程内 Python，MCP 工作线程 | Guard/PEP（宿主通用 PEP，`CapabilityHost._pep`，未经策略映射 fail closed；Guarded Facade 复核） | Ledger `side_effect_key` 去重；副作用不重试 | 【已实现】 |
| 3 | 交互 Hook `handlers[name](ctx, state) -> HookDecision` | `interactions/pipeline_host.py · InteractionPipelineHost.run` → provider | 进程内 Python，引擎线程同步 | 无（只收窄，不授权） | 无重试；异常记 `hook_failed` 后跳过 | 【已实现】 |
| 4 | 转人工三槽 `propose / notify / resolve` | `handoff/core.py · HandoffCore` → provider | 进程内 Python | Core 先 `Guard.require` 再调槽 | notify 异常被吞并记事件；无重试 | 【已实现】（`build_handoff_core` 按能力挑选：只把实现了 `propose` / `notify` / `resolve` 的提供者接进对应槽） |
| 5 | 事件观察者 `on_event(tenant_id, session_id, event_type, payload)` | `events/relay.py · _fanout` → provider | 进程内 Python，同步 | 无 | 无重试；异常只记日志 | 【已实现】 |
| 6 | 渠道适配器 `ChannelAdapter`（normalize/send/start_ingress/stop_ingress） | `channels/host.py · ChannelHost` / legacy inbox·outbox → adapter | 进程内 Python | Receive PEP / Send PEP 在 `ChannelHost` | outbox 幂等键 | 【已实现】（第三方适配器注册接线待实现） |
| 7 | 能力回调 MCP（引擎 → StaffDeck） | 上游 deepseek-harness 0.1.2-alpha.2 子进程 `@deepseek-ai/dsh-mcp-client` → `bridge/capability_mcp.py · CapabilityMcpServer` | MCP Streamable HTTP（JSON-RPC 2.0，`json_response=True, stateless_http=True`），仅 127.0.0.1 | 请求头 `x-staffdeck-activation: <token>`（每 Turn 一枚） | Ledger | 【已实现】（内部接口，不对第三方开放） |
| 8 | 引擎驱动（StaffDeck → 引擎） | `bridge/worker.py · HarnessV3Process` → `dsh --profile sdk` 子进程 | JSON-RPC 2.0 NDJSON over stdio（官方 Python SDK `HarnessClient`） | 进程环境变量；子进程**不持有任何模型提供方凭证** | 无；Turn 取消 = 关闭子进程 | 【已实现】（内部接口） |
| 8a | 模型调用（引擎 → StaffDeck 模型模块） | 引擎 `llm-deepseek` 适配器 → `bridge/model_gateway.py · ModelGateway`（与能力 MCP 同端口 `/v1/chat/completions`）→ `app.llm.client.LLMClient` + 协议驱动 → 提供方 | OpenAI Chat Completions（SSE 流式透传，含 `reasoning_content` / `tool_calls` 增量） | `Authorization: Bearer <activation token>`（每 Turn 一枚；提供方密钥、思考策略、温度、输出上限全部由 ModelConfig 决定） | 由 `LLMClient` 的重试策略处理；每次调用记 `llm_call_started/finished/failed`（operation=`harness_v3.step`） | 【已实现】 |
| 9 | 远程能力提供者（StaffDeck → 第三方） | `capability.mcp_remote` 适配模块 → 第三方 MCP 服务 | MCP Streamable HTTP（JSON-RPC 2.0） | `Authorization: Bearer <workload token>` + `X-StaffDeck-*` | 只读调用最多重试 2 次；副作用调用**永不**自动重试 | 【待实现】 |
| 10 | Webhook 出站（StaffDeck → 第三方） | `events/webhook.py` 投递器 → 第三方 HTTPS 端点 | HTTPS POST JSON | `X-StaffDeck-Signature: sha256=HMAC-SHA256` | `delivery_id` 去重；5 次指数退避 | 【待实现】 |
| 11 | Webhook 入站（第三方 → StaffDeck） | 第三方 → `POST /api/enterprise/harness/hooks/{module_id}/inbound` | HTTPS POST JSON | 同一 HMAC 方案或 Bearer workload token | `delivery_id` 去重；重复返回首次应答 | 【待实现】 |
| 12 | 企业权限中心判定 | `security/business_base.py · BaseAuthzClient` → Base authz | HTTP JSON `POST /internal/v1/authz/check` `/batch-check` | `X-Base-Service-Token: <decision_token>` | 传输错误/502·503·504 重试 1 次；`authorization_pending` 退避轮询至超时 | 【已实现】 |
| 13 | 工作负载凭证 | `security/business_base.py · BaseWorkload` → Base Identity | HTTP JSON（oauth/token 为 form） | client_credentials → Bearer 服务令牌 + `Idempotency-Key` | 无自动重试 | 【已实现】（线协议对齐企业库，未在企业库实跑） |
| 14 | 权限中心连接测试 | `security/base_preflight.py · preflight_base` → Base | HTTP | `X-Base-Service-Token` | 只读，无重试 | 【已实现】 |
| 15 | 管理 API | 前端 `/admin` → `api/admin.py · router`（`/api/enterprise/harness/*`） | HTTP JSON | 登录 JWT + `ensure_tenant_admin`（读接口 `ensure_current_user_tenant`；`/status` 对非管理员隐去路径 / 错误详情）。`/ledger/recent`、`/events/recent`、`/log` 允许该会话拥有者（`ChatSession.user_id`）读取，非管理员 `arguments` 脱敏为 `<redacted>`（保留键名）；`/sessions/recent`、`/ledger/unknown`、`/audit` 仅管理员；进程级变更（`PUT /config`、`POST /restart`、`PUT /modules/{id}/placement`、`POST /modules/inspect`、`POST /base/test`）还要该租户在 `harness_operator_tenants` 之内（空 = 单操作员部署，操作员自己的租户可变运行时），`PUT /staff/{id}/engine`、`POST /ledger/{id}/reconcile` 保持在租户管理员 | `/restart` 先预检、再在局部构建、最后原子切换（先 `drain_timeout_seconds` 等待在途 Turn，返回 `interrupted_turns`）；每次保存 / 测试 / 预检 / 重启写审计事件（`GET /audit`） | 【已实现】 |
| 16 | 装配文件 `staffdeck-runtime.json` | 管理 API ↔ 磁盘（`modules/config.py · save_overrides / load_overrides`） | JSON 文件 | 文件权限；Base 密钥字段以 `app.security.encryption` 加密 | 临时文件 + `os.replace` 原子写 | 【已实现】 |

---

## 1. 术语与模块类型

### 1.1 术语

| 术语 | 定义 | 代码 |
|---|---|---|
| **模块 (Module)** | 一份 `ModuleManifest` + 一个实现对象（provider），安装进 `ModuleRegistry` 得到 `Installed{manifest, provider, slot, enabled, config, source}` | `contracts/manifest.py · ModuleManifest`；`modules/registry.py · Installed` |
| **插槽 (Slot)** | 模块唯一合法的挂载点，枚举 `SlotName`（20 个值） | `contracts/manifest.py · SlotName` |
| **宿主 (Host)** | 持有 `Guard`、从注册表解析 provider 并调用它的内核/受信组件 | `capabilities/host.py · CapabilityHost`；`interactions/pipeline_host.py · InteractionPipelineHost`；`handoff/core.py · HandoffCore`；`channels/host.py · ChannelHost`；`bridge/engine_host.py · EngineHost`；`events/relay.py · SessionEventRelay / fanout_event` |
| **操作 (Operation)** | 逻辑能力名，格式 `family.verb/vN`，如 `knowledge.search/v1`；全集在 `SUPPORTED_CONTRACTS` | `modules/registry.py · SUPPORTED_CONTRACTS` |
| **策略动作 (policy_action)** | 模块声明"我的操作需要 PEP 检查"的操作名；宿主据此 `Guard.require` | `contracts/manifest.py · ModuleManifest.policy_actions`；`modules/registry.py · ModuleRegistry.seal` |
| **激活 (Activation)** | 一个 TaskFrame 内冻结的 `ActivationSlot`（快照 + generation + SOP 位置 + 截止时间 + `session_id`） | `capabilities/host.py · ActivationSlot` |
| **装配 (Assembly)** | 管理员保存的运行时组合 `RuntimeOverrides`：引擎 / 权限模式 / 停用模块 / 外部模块 / 显示归类 / Base 连接 | `modules/config.py · RuntimeOverrides` |
| **类目 (Category / Placement)** | 管理后台"大模块 › 子模块"树上的位置，只影响显示 | `modules/taxonomy.py · TAXONOMY / resolve_placement` |

### 1.2 模块类型 A / C / T / K

`contracts/manifest.py · ModuleKind`，前端标签见 `frontend-enterprise/src/lib/harnessLabels.ts`。

| 类型 | 含义 | 前端标签 | 第三方可否替换 | 可否在 `/admin` 停用（`switchable`） |
|---|---|---|---|---|
| **A** `CODE` | 实现稳定 SPI 的 provider / adapter | 可替换 | 可以：安装同 `provides` 的 A 模块并停用内置实现（§2.8） | 可以（A 类默认 `switchable=true`） |
| **C** `CONTENT` | 员工 / SOP / 技能等内容包 | 可配置 | 不通过代码替换；通过数据（绑定、发布）改变行为 | 仅当 manifest `metadata.switchable=true` |
| **T** `TRUSTED` | 独立可部署、平台唯一语义的服务 | 平台服务 | 不可替换 | 仅当 `metadata.switchable=true`（内置只有 `sandbox.local` 声明了） |
| **K** `KERNEL` | 契约、编排、幂等、安全边界 | 核心 | 不可替换 | 不可以 |

【已实现】`switchable = kind is CODE or bool(metadata.switchable)`（`modules/registry.py · ModuleRegistry.describe`）。`PUT /config` 对 `disabled_modules` 逐项检查：未知 id → 400；`slot ∈ {runtime.engine, security.pep}` → 400（"通过选择引擎 / 权限模式切换"）；`switchable=false` → 400（`api/admin.py · harness_set_config`）。

【已实现·注意】注册表层本身**不**做上述检查：`modules/registry.py · discover_and_install` 对 `ctx["disabled"]` 中的任何 id 一律 `set_enabled(False)`。因此通过环境变量 `STAFFDECK_HARNESS_DISABLED_MODULES` / `Settings.harness_disabled_modules` 或手工编辑 `staffdeck-runtime.json` 可以停用任何模块（含 K），后果自负（可能 `seal()` 失败或运行期 `AttributeError`）。管理 API 是唯一受保护的入口。

### 1.3 插槽与可替换性（按代码）

单提供者插槽 `SINGLE_PROVIDER_SLOTS = {runtime.engine, security.pep}`（`modules/registry.py`）：`seal()` 时启用者多于一个 → `SLOT_CONFLICT`。这两个槽只能通过装配的 `engine` / `security_profile` 二选一。

| 插槽 | 内置模块（kind） | 宿主选择规则 | 第三方可做什么 |
|---|---|---|---|
| `runtime.engine` | `engine.harness_v2`(T) / `engine.harness_v3`(T) | `EngineHost.open` 取第一个启用者的 `open()` | 不可新增；二选一 |
| `runtime.kernel` | `runtime.coordinator`(K) / `harness_v3.core`(K) | 信息性 | 不可挂载 |
| `security.pep` | `security.oss_local`(K) / `security.business_base`(K) | `security/profile.py · _profile_from_registry` 取第一个启用者的 `build(settings)` | 不可新增；二选一 |
| `staff.capability` | `knowledge.local`(A) `general_skill.local`(A) `tool.local`(A) `sandbox.local`(T, switchable) | `CapabilityHost._dispatch` 按 `for_operation(op)`（第一个启用且 `provides` 含该操作者） | 新增 A 提供者，或替换 A 类内置提供者（必须停用内置） |
| `sop.slot.knowledge` / `sop.slot.skill` / `sop.slot.action` | 复用上面的能力提供者（manifest `attaches_to` 同时声明） | 编译器按操作名解析，不按槽 | 同 `staff.capability` |
| `sop.slot.control` | `sop.definition`(C) `sop.slots`(K) `sop.runtime`(T) | — | 不可替换 |
| `staff.interaction` | `interaction.default`(A) | 所有启用者的 `hooks` 叠加、`handlers` 合并 | 新增 Hook 贡献者（可叠加；也可停用默认） |
| `staff.model_route` | `staff.persona`(K) `staff.model_route`(K) | — | 不可替换 |
| `staff.sop` | `composition.projection`(K) `composition.compiler`(K) | — | 不可替换 |
| `staff.team` | `team.provider`(A) | — | 可替换（需停用内置） |
| `staff.ingress` | `ingress.web` / `ingress.public_api` / `ingress.scheduler`(A) | 信息性（路由由内置宿主挂载） | 可新增入口条目（需宿主支持） |
| `staff.channel` | `channel.{feishu,dingtalk,wecom,wechat,wechat_kf}`(A) `channel.host`(T) | `ChannelHost.adapter(channel)` 走 legacy `get_channel_adapter` | 新增渠道适配器（注册接线【待实现】） |
| `handoff.assignment` | `handoff.assignment.default`(A) `handoff.core`(T) `runtime.cancellation`(T) | `build_handoff_core` 取 `provider(slot)` = 第一个启用者 | 替换指派策略（现有选择规则有缺陷，§3.A.5） |
| `handoff.notifier` | `handoff.notifier.{web,feishu,dingtalk,wecom,wechat}`(A) | 按 `provider.name`（缺省 `module_id`）建字典；`notify` 顺序 = 首选渠道 → `web` | 新增 / 替换通知器 |
| `handoff.reply_endpoint` | `handoff.reply.web` / `handoff.reply.channel_command`(A) | 按 `provider.name` 建字典；调用方指定 resolver | 新增回复解析器 |
| `knowledge.import.source` | （无内置） | 无宿主 | 【待实现】宿主 |
| `event.observer` | `observer.trace`(T) `observer.feedback`(A) `ledger.invocation`(T) | 全部扇出 | 新增观察者 |
| `tenant.staff` | （无内置） | 无宿主 | 保留 |

内置模块共 41 个（A 22 / T 9 / K 9 / C 1），见 `modules/builtin.py · register` 与 `modules/kernel.py · register`。

---

## 2. 模块清单（Manifest）

### 2.1 数据类与三级校验【已实现】

`contracts/manifest.py · ModuleManifest` 是冻结 dataclass。校验分三级：

| 时机 | 校验 | 失败 |
|---|---|---|
| 构造期 `ModuleManifest.__post_init__` | `module_id / version / contract_version` 非空；每个操作名含 `/`；`kind` 与 `attaches_to` 可从字符串转枚举 | `ValueError` |
| 安装期 `ModuleRegistry.install` | 注册表未 seal；`module_id` 匹配 `MODULE_ID_RE`；`version` 匹配 `SEMVER_RE`；`contract_version ∈ SUPPORTED_CONTRACT_VERSIONS`；`module_id` 唯一；`slot ∈ attaches_to`；`provides ∪ requires` 每项 `name/vN` 在 `SUPPORTED_CONTRACTS[name]` | `RegistrySealed` / `ContractIncompatible` / `DuplicateModule` / `SlotConflict` |
| 封印期 `ModuleRegistry.seal` | 仅看**启用**模块：声明 `policy_actions` 的模块所在槽已被 `mark_guarded`；`requires` 被某启用模块 `provides`；单提供者槽启用者 ≤ 1 | `PepBindingMissing` / `UnsatisfiedRequirement` / `SlotConflict` |

正则（`modules/registry.py`）：

```
MODULE_ID_RE = ^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$          # 至少两段；后续段可数字开头
SEMVER_RE    = ^\d+(\.\d+){0,2}([-+][0-9A-Za-z.-]+)?$     # "1" / "1.0" / "1.2.3-rc.1" / "1.2.3+build.5" 皆合法；"v1"、"1.2.3.4"、"latest" 非法
SUPPORTED_CONTRACT_VERSIONS = {"v1"}
```

`seal()` 后注册表不可变：`install` / `set_enabled` 抛 `RegistrySealed`；每次 `seal()` 使 `generation += 1`。

### 2.2 JSON Schema（draft 2020-12）

同一 Schema 用于：进程内模块的 `manifest(...)` 参数（Python 形式）、【待实现】远程 MCP 提供者 `capability.describe.v1` 的返回、【待实现】Webhook 模块注册体。正则与代码保持一致。

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://staffdeck.dev/schemas/module-manifest.v1.json",
  "title": "StaffDeck ModuleManifest v1",
  "type": "object",
  "required": ["module_id", "name", "version", "kind", "contract_version", "attaches_to"],
  "additionalProperties": false,
  "properties": {
    "module_id": {
      "type": "string",
      "pattern": "^[a-z][a-z0-9_]*(\\.[a-z0-9_]+)+$",
      "description": "点分层级 id，至少两段。第三方 MUST 以厂商命名空间开头（如 acme.knowledge）；首段不得为保留前缀（§2.3）。"
    },
    "name": { "type": "string", "minLength": 1, "maxLength": 64, "description": "面向管理员的显示名（中文优先）" },
    "version": {
      "type": "string",
      "pattern": "^\\d+(\\.\\d+){0,2}([-+][0-9A-Za-z.-]+)?$",
      "description": "1~3 段数字版本 + 可选预发布/构建后缀（SEMVER_RE）；第三方 SHOULD 用完整的 SemVer 2.0.0 三段式"
    },
    "kind": { "type": "string", "enum": ["A", "C", "T", "K"], "description": "第三方 MUST 为 A（C/T/K 由平台保留）" },
    "contract_version": { "type": "string", "enum": ["v1"] },
    "attaches_to": {
      "type": "array", "minItems": 1, "uniqueItems": true,
      "items": { "type": "string", "enum": [
        "runtime.engine", "runtime.kernel", "tenant.staff", "staff.sop", "staff.capability", "staff.model_route",
        "staff.team", "staff.interaction", "staff.channel", "staff.ingress",
        "sop.slot.knowledge", "sop.slot.skill", "sop.slot.action", "sop.slot.control",
        "handoff.notifier", "handoff.reply_endpoint", "handoff.assignment",
        "knowledge.import.source", "event.observer", "security.pep" ] }
    },
    "provides_operations": { "type": "array", "uniqueItems": true, "items": { "$ref": "#/$defs/operation" } },
    "requires_operations": { "type": "array", "uniqueItems": true, "items": { "$ref": "#/$defs/operation" } },
    "policy_actions": {
      "type": "array", "uniqueItems": true, "items": { "$ref": "#/$defs/operation" },
      "description": "宿主在调用本模块前 MUST Guard.require 的操作；每一项必须能被 DEFAULT_ACTION_MAP 映射（§4.2）"
    },
    "hooks": {
      "type": "array",
      "items": {
        "type": "object", "required": ["point", "handler"], "additionalProperties": false,
        "properties": {
          "point": { "type": "string", "enum": ["pre_step", "pre_tool", "post_tool", "turn_stopping"] },
          "handler": { "type": "string", "pattern": "^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)*$", "description": "由 provider.handlers[handler] 解析；第三方 MUST 以厂商前缀开头" },
          "order": { "type": "integer", "minimum": 0, "default": 100 },
          "depends_on": { "type": "array", "items": { "type": "string" }, "default": [], "description": "同一 point 内必须先于本 handler 运行的 handler 名" }
        }
      }
    },
    "metadata": {
      "type": "object",
      "properties": {
        "summary":    { "type": "string", "maxLength": 200, "description": "一句话说明；describe() 提升为顶层 summary【已实现】" },
        "category":   { "type": "string", "pattern": "^[a-z]+\\.[a-z_]+$", "description": "taxonomy SubModule.id，如 capability.knowledge；describe() 提升为顶层 category，参与归类【已实现】" },
        "switchable": { "type": "boolean", "default": false, "description": "非 A 类模块允许在 /admin 停用【已实现】" },
        "vendor":     { "type": "string" },
        "homepage":   { "type": "string", "format": "uri" },
        "docs":       { "type": "string", "format": "uri" },
        "channel":    { "type": "string", "description": "staff.channel 模块的渠道名（缺省 module_id 末段）【待实现】" },
        "access":     { "type": "string", "enum": ["inprocess", "mcp", "webhook"], "default": "inprocess", "description": "【待实现】" },
        "endpoint":   { "$ref": "#/$defs/endpoint", "description": "【待实现】access=mcp|webhook 时必填" },
        "priority":   { "type": "integer", "default": 100, "description": "同一 provides 冲突时越小越优先【待实现】" },
        "policy_map": { "type": "object", "additionalProperties": { "type": "array", "prefixItems": [{"type": "string"}, {"type": "string"}], "minItems": 2, "maxItems": 2 }, "description": "新增操作家族的 (action, resource_type) 映射【待实现】" },
        "log_events": { "type": "object", "additionalProperties": { "type": "string" }, "description": "模块自定义事件 → 执行日志标签【待实现】" }
      },
      "additionalProperties": true
    }
  },
  "$defs": {
    "operation": { "type": "string", "pattern": "^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+/v[1-9]\\d*$", "description": "且 name 必须在 SUPPORTED_CONTRACTS 内" },
    "endpoint": {
      "type": "object", "required": ["url"],
      "properties": {
        "url": { "type": "string", "format": "uri" },
        "auth": { "type": "string", "enum": ["workload", "bearer_env", "none"], "default": "workload" },
        "bearer_env": { "type": "string", "description": "auth=bearer_env 时读取的环境变量名；值不入库" },
        "timeout_ms": { "type": "integer", "minimum": 1000, "default": 60000 },
        "fail_on_startup_error": { "type": "boolean", "default": true },
        "signing_secret_env": { "type": "string", "description": "webhook：HMAC 密钥所在环境变量名" }
      }
    }
  }
}
```

### 2.3 命名规则

- **`module_id`**：见 `MODULE_ID_RE`。内置保留首段：`knowledge, general_skill, tool, sandbox, interaction, handoff, channel, observer, engine, security, staff, composition, sop, runtime, team, ingress, harness_v3, ledger`（来源 `modules/builtin.py` 与 `modules/kernel.py`）。第三方 MUST 使用自己的厂商前缀（如 `acme.*`）。【已实现】代码只校验格式，不校验保留前缀；【待实现】`install()` 对 `source != "builtin"` 的模块拒绝保留首段（`CONTRACT_INCOMPATIBLE`）。
- **操作名**：`family.verb/vN`。新增家族 MUST 先加入 `modules/registry.py · SUPPORTED_CONTRACTS`，否则安装即 `CONTRACT_INCOMPATIBLE`。【待实现】`composition/compiler.py · SUPPORTED_CONTRACTS` 是一份更小的副本（只含 SOP 可声明的 9 个操作）；应改为引用注册表单一来源（`CompositionCompiler.__init__` 的 `supported_contracts` 参数目前会 `update` 这份副本）。
- **Hook handler 名**：全局命名空间。`ModuleRegistry.hook_handlers()` 用 `dict.update` 合并所有启用的 `staff.interaction` 模块的 `handlers`，后装者覆盖；`InteractionPipelineHost.__init__` 再以 `{**DEFAULT_HANDLERS, **handlers}` 合并，注册表条目覆盖默认名。第三方 MUST 用 `<vendor>.<name>`，MUST NOT 覆盖默认 handler 名（`persona, memory.recall, sop.execution_slice, activation.allowlist, capability.pep, ledger.record, citations.collect, sop.output_supervisor, handoff.detect`，见 `interactions/pipeline_host.py · DEFAULT_HANDLERS`）。
- **事件名**：小写下划线，见 §6。

### 2.4 `attaches_to` 与安装插槽【已实现】

`install(..., slot=)` 指定的槽 MUST ∈ `attaches_to`（`SLOT_CONFLICT`）。一个模块一次安装只挂一个槽（`Installed.slot`）。能力提供者声明 `[staff.capability, sop.slot.*]` 但只安装到 `staff.capability`；SOP 槽通过编译器按操作名复用（`CapabilityHost._dispatch` 按 `for_operation` 解析，不按槽）。

### 2.5 `provides` / `requires` / `policy_actions`

- 【已实现】`requires_operations` 在 `seal()` 时必须被某个**启用**模块的 `provides_operations` 满足，否则 `UNSATISFIED_REQUIREMENT`。
- 【已实现】声明 `policy_actions` 的**启用**模块所在插槽 MUST 已被宿主 `mark_guarded()`，否则 `PEP_BINDING_MISSING`。真实装配路径（`get_registry` → `discover_and_install` → `seal`）里被标记的槽只来自内置注册函数：`staff.capability, handoff.assignment, handoff.notifier, handoff.reply_endpoint, staff.channel, runtime.engine, staff.model_route, sop.slot.control, staff.team, staff.ingress`。`event.observer`、`staff.interaction`、`staff.sop`、`runtime.kernel`、`security.pep`、`knowledge.import.source`、`tenant.staff` **未标记**，这些槽上的模块 MUST NOT 声明 `policy_actions`。
- 【已实现·注意】`runtime/assembly.py · preflight_assembly` 与 `modules/inspect.py · inspect_spec` 会把**全部**槽 `mark_guarded`，因此预检 / dry-run **检测不到** `PEP_BINDING_MISSING`；这类错误只会在真正 `_build` 时出现并触发回滚。【待实现】预检应复用真实的受护槽集合（把 `mark_guarded` 调用集中到一个 `GUARDED_SLOTS` 常量）。
- 【待实现】`seal()` 校验 `policy_actions ⊆ DEFAULT_ACTION_MAP ∪ 合并后的 metadata.policy_map`，否则宿主 `Guard.require` 时才会 `KeyError`（`contracts/security.py · PolicyActionMapper.map`）。
- 【待实现】`seal()` 校验所有启用 `staff.interaction` 模块 `hooks[*].handler` 都能在合并后的 `hook_handlers()` 中解析；当前只在 `InteractionPipelineHost.__init__` 抛 `LookupError`，即**每个 Turn** 失败而不是启动失败。

### 2.6 `metadata` 保留键与 `describe()` 输出【已实现】

`modules/registry.py · ModuleRegistry.describe()` 每个条目：

```json
{
  "module_id": "acme.sink", "name": "ACME 审计", "summary": "…", "version": "0.1.0", "kind": "A", "contract_version": "v1",
  "slot": "event.observer", "enabled": true, "source": "acme_pkg:register",
  "category": "governance.trace", "switchable": true,
  "metadata": {"vendor": "ACME"},
  "provides": ["event.observe/v1"], "requires": [], "hooks": ["pre_tool:acme.pii_guard"], "policy_actions": [],
  "guarded": false
}
```

- `summary` / `category` / `switchable` 从 `metadata` 提升为顶层字段并从 `metadata` 中剔除。
- `metadata` 只透传**标量**值（`str / int / float / bool`）；对象（如 `endpoint`、`policy_map`）不出现在 `describe()` / `ModuleRead`（`api/admin.py · ModuleRead`）。
- `source`：显式传入，否则取 `installing_from()` 上下文：`"builtin"`、`"entry_point:<ep.name>"`、`"<spec>"`（规格串原文）。
- `guarded`：该模块所在槽是否已 `mark_guarded`。

### 2.7 归类（Placement）精确规则【已实现】

`modules/taxonomy.py · resolve_placement(module_id, slot, category=, override=)`，优先级从高到低：

1. **override**：装配 `placements[module_id]` 且是合法 `SubModule.id`（只对 `movable` 模块生效：`kind != "K"` 且 `slot ∉ {runtime.engine, security.pep}`，`taxonomy.py · movable`）；
2. **taxonomy**：`module_id` 出现在某 `SubModule.module_ids`（内置模块几乎全在此，故内置模块**不能**用 `metadata.category` 改位置，只能用 override）；
3. **manifest**：`metadata.category` 是合法 `SubModule.id`；
4. **slot**：安装槽出现在某 `SubModule.slots`。注意 `staff.capability`、`event.observer`、`security.pep`、`knowledge.import.source`、`tenant.staff` **不属于任何子模块**，因此第三方能力提供者 / 观察者若不声明 `category`，会落入"未归类"（`unplaced / unplaced.all`）；管理员可在树上直接选类目（写入 `placements`，立即生效、不触发 pending）。

### 2.8 兼容性与冲突

- **契约版本**：`SUPPORTED_CONTRACTS[name]` 是版本集合；升级操作契约 = 新增 `vN+1`，旧版保留至少一个小版本周期。
- **同一 `provides` 冲突**【已实现】：`for_operation()` 遍历 `_by_id` 插入序，返回**第一个启用**的提供者；内置最先安装（`discover_and_install` 顺序：builtin → entry points → 规格串）。第三方要接管 `knowledge.search/v1` 必须把 `knowledge.local` 放进 `disabled_modules`。`POST /modules/inspect` 会对此给出 `OPERATION_SHADOWED` 警告。
- 【待实现】`metadata.priority`：`for_operation()` 按 `(priority, 安装序)` 排序；`seal()` 对同一操作多个启用提供者写入 `describe()["shadowed_by"]`。
- **多提供者插槽**（`handoff.notifier`、`handoff.reply_endpoint`、`event.observer`、`staff.channel`、`staff.interaction`）：并存，按各自宿主规则（§1.3）。

---

## 3. 三种接入方式及协议（写死）

### 3.0 接入矩阵：每个插槽允许的接入方式

| SlotName | (A) 进程内 Python SPI | (B) 进程外 MCP 提供者 | (C) HTTP Webhook | 说明 |
|---|---|---|---|---|
| `runtime.engine` | 仅内置 | ✗ | ✗ | 引擎内部用 JSON-RPC/stdio 驱动上游 deepseek-harness 0.1.2-alpha.2 子进程 |
| `runtime.kernel` | 仅内置 | ✗ | ✗ | 信息性 |
| `security.pep` | 仅内置 | ✗ | ✗ | BUSINESS_BASE 内部经 HTTP 调 Base（§4.4），PEP 模块本身在进程内 |
| `staff.capability` / `sop.slot.knowledge` / `sop.slot.skill` / `sop.slot.action` | A ✓【已实现】 | A ✓【待实现】 | ✗ | 能力必须同步返回 `ModuleResult` |
| `sop.slot.control` | 仅内置 | ✗ | ✗ | |
| `staff.interaction` | A ✓【已实现】 | ✗ | ✗ | Hook 在引擎线程同步执行，必须低延迟 |
| `staff.model_route` / `staff.sop` | 仅内置 | ✗ | ✗ | |
| `staff.team` | A ✓【已实现】 | ✗ | ✗ | |
| `staff.ingress` | A ✓（路由由内置宿主挂载） | ✗ | 入站 Webhook【待实现】 | |
| `staff.channel` | A ✓（`ChannelAdapter`；注册接线【待实现】） | ✗ | 双向 Webhook【待实现】 | |
| `handoff.assignment` | A ✓【已实现】 | ✗ | ✗ | 需要 DB 读 |
| `handoff.notifier` | A ✓【已实现】 | ✗ | ✓【待实现】 | 同步返回 `notify_message_id` |
| `handoff.reply_endpoint` | A ✓【已实现】 | ✗ | 入站 Webhook【待实现】 | |
| `knowledge.import.source` | A【待实现】宿主 | ✗ | ✗ | |
| `event.observer` | A ✓【已实现】 | ✗ | ✓【待实现】 | 只消费，不影响 Turn |

---

### 3.A 进程内 Python SPI【已实现】

#### 3.A.1 发现与加载

`modules/registry.py · discover_and_install(registry, settings, include_builtin=True, extra_specs=())`，顺序固定：

1. 内置 `staffdeck_harness.modules.builtin.register`（source=`builtin`）；
2. Python entry point 组 **`staffdeck_harness.modules`**（`ENTRY_POINT_GROUP`；source=`entry_point:<name>`）；加载异常记日志后**重新抛出**，整次装配失败；
3. 规格串 `pkg.module:register`：来自 `Settings.harness_modules` / 环境变量 `STAFFDECK_HARNESS_MODULES`（逗号分隔）+ `extra_specs`；规格串格式 `SPEC_RE = ^[A-Za-z_][\w]*(\.[A-Za-z_]\w*)*(:[A-Za-z_]\w*)?$`，`:attr` 省略时默认 `register`（`_load_callable`）；source=规格串原文。管理后台 `extra_modules` 通过 `modules/config.py · apply_overrides` 投影到 `Settings.harness_modules`，所以走的也是第 3 条。
4. 最后对 `ctx["disabled"]` 中已安装的 id 调用 `set_enabled(False)`。

`pyproject.toml` 写法（第三方包）：

```toml
[project.entry-points."staffdeck_harness.modules"]
acme = "acme_staffdeck.plugin:register"
```

`get_registry(settings)` 是进程级单例：首次调用 `discover_and_install` + `seal()`；`reset_registry()` 清空（重启路径每次重建）。

#### 3.A.2 `register(registry, ctx)` 契约

签名 `Registrar = Callable[[ModuleRegistry, Mapping[str, Any]], None]`。

`ctx` 键：

| 键 | 类型 | 含义 |
|---|---|---|
| `settings` | 应用 `Settings`（真实装配）或其 `model_copy` 视图（预检 / dry-run，`modules/config.py · settings_view`） | 只读 |
| `disabled` | `set[str]` | 本次装配将停用的 `module_id` 集合（模块 MAY 用它跳过昂贵构造，但注册表仍会统一执行 `set_enabled`） |
| `dry_run` | `True` | **仅** `modules/inspect.py · inspect_spec` 传入；真实装配与 `preflight_assembly` **不**带此键。模块 SHOULD 用 `ctx.get("dry_run")` 判断，MUST NOT 在 dry-run 时产生任何外部副作用 |

模块 MUST：
- 对每个模块调用一次 `registry.install(manifest, provider, slot=..., enabled=True, config=None, source=None)`；`source` 省略时自动取 `installing_from` 上下文（推荐省略）。
- 用 `staffdeck_harness.modules.registry.manifest(module_id, name, *, kind, slots, summary="", provides=(), requires=(), hooks=(), policy_actions=(), version="1.0.0", contract_version="v1", metadata=None)` 构造清单，或直接构造 `ModuleManifest`。
- 幂等：同一进程多次 restart 会重新调用 `register`（`runtime/assembly.py · restart_harness_runtime` 每次 `reset_registry()` 后重建）；MUST NOT 依赖模块级全局状态或"只注册一次"假设。
- 快速返回：`register` 只构造对象。

模块 MUST NOT：
- 调用 `registry.mark_guarded()`（只有持 `Guard` 的宿主可以；dry-run 会尝试给出 `GUARD_SELF_DECLARED` 警告——但由于 dry-run 已预先标记全部槽，此警告当前永远不会触发，见 §7.3）、`registry.seal()`、`registry.set_enabled()`。
- 在 `register` 内做网络 I/O、启动线程或子进程、读写数据库；远程连接放到宿主首次 `invoke` 或装配预检（§3.B.4）。
- 在**模块导入时**导入 `app.*`（dry-run 会在管理进程内 `import` 你的包；`sys.modules` 保留该导入，改包后需重启进程才能重新读取）。
- 依赖：A 模块 MUST 只依赖 `staffdeck_harness.contracts.*`、`staffdeck_harness.modules.registry.manifest`、`staffdeck_harness.composition.projection`（授权投影，唯一允许触库的辅助层）以及 `invoke(host, inv)` 时宿主传入的句柄；MUST NOT 直接 import `app.api.*` / `app.core.*` / 各业务 service。

#### 3.A.3 各插槽 provider 鸭子类型（`modules/builtin.py` 模块 docstring 与各宿主）

| 插槽 | provider 必须提供 | 调用方 / 线程 | 返回与异常 |
|---|---|---|---|
| `staff.capability` 等 | `invoke(host: CapabilityHost, inv: ModuleInvocation) -> ModuleResult` | `capabilities/host.py · CapabilityHost._dispatch`；**MCP 工作线程**（`bridge/capability_mcp.py · CapabilityMcpServer._call_tool` 用 `run_in_executor`） | 返回 `ModuleResult`；抛 `ModuleSdkError` 子类 → 宿主映射为 `fail(code, message, extensions.details)`；抛 `PermissionDenied` → ledger `denied` + 事件 `capability_denied`；抛 `AuthorizationUnavailable` → ledger `denied`；抛 `ActivationFenced` → ledger `cancelled`；其他异常 → `fail("HARNESS_TOOL_ERROR")`，副作用调用记 `outcome_unknown` |
| `staff.interaction` | 属性 `handlers: Mapping[str, Handler]`，`Handler = Callable[[HookContext, PipelineState], HookDecision]` | `interactions/pipeline_host.py · InteractionPipelineHost.run`；**引擎线程同步** | `HookDecision`；异常被吞并记 `hook_failed`；`deny` 后短路；`merge_decisions` 最严优先（deny > steer > modify > pass） |
| `handoff.assignment` | `propose(db, handoff, *, agent) -> str \| None` | `handoff/core.py · HandoffCore.assign` | 候选 user_id；Core 再校验租户成员 |
| `handoff.notifier` | 属性 `name: str`；`notify(db, handoff, *, pending_question, context_summary) -> str \| None` | `HandoffCore.notify` | 渠道消息 id 或 None（写回 `notify_message_id`）；异常被捕获记 `human_handoff_notify_failed` |
| `handoff.reply_endpoint` | 属性 `name`；`resolve(db, tenant_id, inbound) -> tuple[handoff_id, text] \| None` | `HandoffCore.reply` | Core 之后 `Guard.require("handoff.reply/v1")` |
| `staff.channel` | legacy `ChannelAdapter` Protocol：`normalize / send / start_ingress / stop_ingress`（`app/channels/adapters/base.py · ChannelAdapter`） | `channels/host.py · ChannelHost.adapter()` → legacy `get_channel_adapter` | 见 §3.A.5 差距 |
| `event.observer` | 属性 `name`；`on_event(tenant_id, session_id, event_type, payload) -> None` | `events/relay.py · _fanout`；引擎线程或 MCP 工作线程同步 | 无；异常记日志 |
| `runtime.engine` | `open(loop, request, agent_id) -> HarnessV2Engine` | `bridge/engine_host.py · EngineHost.open`（按模块 id 解析 `engine.harness_v3`，使按 Staff 的 Harness v3 选择可在部署默认为 v2 时作 canary） | 仅内置 |
| `security.pep` | `build(settings) -> SecurityProfile` | `security/profile.py · _profile_from_registry` | 仅内置 |

#### 3.A.4 能力提供者与 PEP 的分工（精确）

【已实现】`CapabilityHost.invoke` 的固定顺序：① `LifecycleFence.check` + `_fence_resource`（围栏只收窄）→ ② **pre_tool hooks**（`_hooks("pre_tool", inv)`；`deny` 返回 `PRE_TOOL_DENIED`，不写 ledger）→ ③ `InvocationLedger.replay_or_block` + `start`（占位）→ ④ **宿主 PEP**（`CapabilityHost._pep`，在下发提供方前执行）→ ⑤ `_dispatch`（`resolve_operation_provider` 按 pin → `capability_provider_selected` 事件 → `provider.invoke(host, inv)`）→ ⑥ `ledger.finish` + `capability_invoked` 事件 → ⑦ **post_tool hooks**（`ledger.record` 收集回执、`citations.collect` 收集引用；`modify` 决策带 `replacement` 时换掉给模型的 `result`）。

**PEP 是宿主义务：** `CapabilityHost._pep` 在 `_dispatch` 之前运行——tool/mcp/a2a 对活行 `Tool` 行（`live_resource_ref`）+ 其 `MCPServer`、`general_skill` 对活行、`knowledge` 对选中的每个 Base、`sandbox` 对应资源各 `guard.require` 一次；**没有策略映射的操作一律拒绝（fail closed，`PermissionDenied`）**。因此一个从不调用 `guard.require` 的外部/远程提供者也无法绕过 PEP。Guarded Facade 仍以活行复核作为第二道防线（defence in depth）。

- 【已实现·约定】第三方 `staff.capability` 提供者在触碰任何资源前仍 SHOULD 自行调用 `host.guard.require(host.security_context, inv.operation, ref)`（资源类型见 §4.2，`knowledge.search/v1` 对每个选中的知识库各 require 一次）；现在这只是复核，不再是必需的豁免缺口。宿主提供 `host.guard`、`host.security_context`、`host.db`、`host.slot`、`host.trace`、`host._deps()`、`host._workspace_root(ctx)`、`host.sandbox(ctx)`。
- 【已实现】提供者 pin：`CapabilityGrant.provider_module_id` / `provider_version`（来自绑定 `metadata_json.provider_module_id`，否则编译期注册表默认提供者）冻结进 `CompositionSnapshot`（改变 `snapshot_id`）；`_dispatch` 经 `ModuleRegistry.resolve_operation_provider` 遵守 pin，被 pin 的模块停用/缺失时 fail closed 返回 `PROVIDER_UNAVAILABLE`（不会在运行中的 Turn 内静默换提供者）。`ModuleRegistry.providers_for_operation` 列出候选。

#### 3.A.5 线程安全与资源约束（MUST）

1. 能力提供者在 MCP 工作线程执行，一个 Turn 内可能并发多次调用；provider 对象是进程级单例（安装一次）：MUST 无可变共享状态，或用锁保护。
2. `host.db` 是宿主为本 TaskFrame 单独开的 `Session`（`bridge/task_agent.py · HarnessV3TaskAgent.run` 的 `host_db = Session(bind)`）：provider MAY 在调用期间使用，MUST NOT 在返回后保留引用，MUST NOT 跨线程共享，MUST NOT `commit()`（ledger 负责事务）。
3. 时间预算：MUST 尊重 `host.slot.remaining_seconds()`（`None` = 无截止）；超时前自行放弃并返回 `fail("TIMEOUT", ...)`。
4. Hook handler 与 observer 同步执行：MUST 在 ≤ 50 ms 内返回；需要 I/O 的 observer MUST 入队异步处理。
5. 禁止：`os.fork`、修改 `sys.path`、修改环境变量、写 `harness_v3_home`、直接调用 `app.api.*`、捕获并吞掉 `PermissionDenied` / `AuthorizationUnavailable` / `ActivationFenced`（这三类必须向上抛，宿主据此记账）。
6. 日志：`logging.getLogger("staffdeck_harness.plugins.<module_id>")`；MUST NOT 打印 `SecurityContext.token_id / workload`、模型 API key、工作负载 token。

#### 3.A.6 现有差距【待实现】

- **`handoff.assignment` 选择规则**：`handoff/core.py · build_handoff_core` 用 `reg.provider(SlotName.HANDOFF_ASSIGNMENT)`（第一个启用者）。该槽的安装序是 `handoff.assignment.default`(A) → `handoff.core`(T) → `runtime.cancellation`(T) → 第三方。一旦 `handoff.assignment.default` 被停用（它是 A 类，`/admin` 允许停用），第一个启用者变成 `handoff.core`，其 provider 没有 `propose`，`HandoffCore.assign` 会 `AttributeError`；第三方指派策略也因此永远选不中。修复：`build_handoff_core` 取第一个**启用且具备 `propose`** 的 provider；同理 notifier / resolver 只收 `callable(notify)` / `callable(resolve)` 的条目。
- **`staff.channel` 第三方适配器**：注册表安装 ≠ legacy 适配器注册。需在 `runtime/assembly.py · _build` 于 `get_registry()` 后遍历 `providers(STAFF_CHANNEL)`，对非内置条目调用 `ChannelHost.register_adapter(channel, adapter)`，渠道名取 `metadata.channel` 或 `module_id` 末段。
- **`knowledge.import.source`**：尚无宿主。设计：provider `list_sources(tenant_id) -> list[{id,name}]` / `pull(db, tenant_id, source_id, since) -> Iterable[Document]`，由知识库导入服务在 `app.knowledge` 侧按 `for_operation("knowledge.import/v1")` 解析（操作已在 `SUPPORTED_CONTRACTS`）。
- 预检 / dry-run 的受护槽保真（§2.5）、hook handler 可解析性校验（§2.5）、`policy_actions` 映射校验（§2.5）。

---

### 3.B 进程外能力提供者 —— MCP（Streamable HTTP，JSON-RPC 2.0）【待实现】

> 目标：第三方以独立服务形态提供 `staff.capability` 家族的操作，StaffDeck 作为 **MCP 客户端** 调用它。选型理由：StaffDeck 已以 MCP 服务端形态把能力暴露给 Harness v3 引擎（`bridge/capability_mcp.py · CapabilityMcpServer`，`mcp` Python SDK，`streamable_http_app(json_response=True, stateless_http=True)`），两端使用同一套报文形状；上游 deepseek-harness 0.1.2-alpha.2 自身的 MCP 客户端也只支持 `stdio` 与 `streamable-http`（附录 B）。**不支持** SSE-only 旧传输，**不支持** stdio（StaffDeck 不为第三方拉子进程）。

#### 3.B.1 接入形态

- 进程内安装一个通用适配模块 **`capability.mcp_remote`**（kind A，建议文件 `modules/remote_mcp.py`）。每个远程服务对应一个 manifest（`metadata.access="mcp"`，`metadata.endpoint.url`）；它实现 `invoke(host, inv)`，把 `ModuleInvocation` 翻译成 `tools/call`。
- 远程服务的声明来源：`staffdeck-runtime.json` 新增 `remote_modules: [ {manifest JSON …} ]`（`RuntimeOverrides.remote_modules`；`PUT /config` 接受并按 §2.2 Schema 校验；`same_assembly` 纳入比较；`_build` / `preflight_assembly` / `inspect_spec` 安装）。
- 与引擎无关：引擎仍只看到 `mcp__staffdeck__*` 代理工具（`capabilities/host.py · PROXY_TOOLS`）；远程提供者不直接出现在模型工具表里。
- PEP 在 StaffDeck 侧完成（§3.A.4 的宿主通用 PEP，已在 `CapabilityHost._pep` 落地）；远程服务 MUST NOT 自行做业务授权判定。

#### 3.B.2 传输

- HTTP/1.1 或 HTTP/2，`POST <endpoint.url>`（推荐路径 `/mcp`）；请求头 `Content-Type: application/json`、`Accept: application/json, text/event-stream`、`MCP-Protocol-Version: 2025-06-18`。
- 服务端 MUST 支持**无状态 JSON 响应模式**（不要求 `Mcp-Session-Id`，不要求 SSE）；MAY 以 SSE 流回应长任务，客户端两者都接受。
- TLS：除 `127.0.0.1` / `localhost` 外强制 `https`。
- 连接复用：每个远程模块一个 `httpx.Client` 连接池，进程级单例，`restart` 时关闭重建。

#### 3.B.3 鉴权

按 `metadata.endpoint.auth`：

| auth | 头 | 值 |
|---|---|---|
| `workload`（默认） | `Authorization: Bearer <token>` | `profile.workload.mint(security_context, audience=module_id, ttl_seconds=300)["access_token"]`。BUSINESS_BASE：`security/business_base.py · BaseWorkload.mint` 返回 `{"kind":"base_workload","access_token":…}`。OSS_LOCAL：`security/oss_local.py · LocalWorkload.mint` 目前只返回不可验证的 `{"kind":"local","token_id":…}`，**没有** `access_token`；【待实现】`LocalWorkload` 增加 `access_token` = HS256 JWT（签名密钥 = 应用 `APP_SECRET`，`aud=module_id`，`sub=principal_id`，`tenant_id`，`exp=now+ttl`），远程服务用部署共享的 `STAFFDECK_WORKLOAD_JWT_SECRET` 验签 |
| `bearer_env` | `Authorization: Bearer <env>` | 读环境变量 `endpoint.bearer_env`；值 MUST NOT 入 manifest / JSON / 日志 |
| `none` | 无 | 仅允许 `127.0.0.1` / `localhost` |

固定附加头：`X-StaffDeck-Tenant: <tenant_id>`、`X-StaffDeck-Module: <module_id>`、`X-StaffDeck-Contract: v1`、`X-StaffDeck-Invocation-Id: <invocation_id>`、`X-StaffDeck-Attempt: <attempt>`、`X-StaffDeck-Idempotency-Key: <side_effect_key>`（仅副作用调用）。

远程服务 MUST 校验 Bearer；MAY 校验 `X-StaffDeck-Tenant` 做租户隔离；MUST NOT 基于用户身份做"允许/拒绝"业务判定（§4.1）。

#### 3.B.4 能力协商（装配预检 / 重启时）

客户端执行：`initialize`（`clientInfo = {name:"staffdeck", version:<staffdeck_harness 版本>}`）→ `tools/list` → `tools/call capability.describe.v1`。

- `initialize.result.serverInfo.name` MUST 等于 `module_id`。
- `capability.describe.v1` MUST 返回 §2.2 Schema 的 manifest JSON（`structuredContent`）。客户端与 `remote_modules` 里的本地声明比对：`module_id`、`contract_version`、`provides_operations` 三者不一致 → `CONTRACT_INCOMPATIBLE`，装配失败并回滚。
- `tools/list` 中每个工具 `name` 与 `provides_operations` 一一对应（命名见 3.B.5）；缺一个 → `UNSATISFIED_REQUIREMENT`。
- `endpoint.fail_on_startup_error=true`（默认）时协商失败即装配失败；`false` 时安装为 `enabled=false` 并在 `describe()` 增加 `startup_error`。
- `POST /modules/inspect` 对 `remote_modules` 候选执行同样三步（网络 I/O 只发生在 dry-run 与预检，不发生在 `register`）。

#### 3.B.5 工具命名与 Schema

- 工具名 = 操作名把 `/` 换成 `.`：`knowledge.search/v1` → **`knowledge.search.v1`**（MCP 工具名不允许 `/`）。反向映射：末段 `v\d+` 为版本。
- `inputSchema` MUST 为：

```json
{
  "type": "object",
  "required": ["arguments", "context", "invocation"],
  "properties": {
    "arguments": { "type": "object", "description": "= ModuleInvocation.arguments，形状由操作定义（如 PROXY_TOOLS[*].parameters；tool.invoke/v1 为 {tool_id, **inner}）" },
    "context": {
      "type": "object",
      "required": ["tenant_id", "agent_id", "user_id", "session_id", "turn_id", "channel"],
      "properties": {
        "tenant_id": {"type": "string"}, "agent_id": {"type": "string"}, "user_id": {"type": "string"},
        "session_id": {"type": "string"}, "turn_id": {"type": "string"}, "channel": {"type": "string"},
        "task_frame_id": {"type": ["string", "null"]}, "step_id": {"type": ["string", "null"]}, "run_id": {"type": ["string", "null"]},
        "snapshot_id": {"type": ["string", "null"]}, "trace_id": {"type": ["string", "null"]},
        "deadline_at": {"type": ["string", "null"], "format": "date-time"}, "attempt": {"type": "integer", "minimum": 1}
      }
    },
    "invocation": {
      "type": "object", "required": ["invocation_id", "operation", "side_effecting", "attempt"],
      "properties": {
        "invocation_id": {"type": "string"}, "operation": {"type": "string"},
        "binding_id": {"type": ["string", "null"]}, "side_effecting": {"type": "boolean"},
        "side_effect_key": {"type": ["string", "null"]}, "request_digest": {"type": "string"},
        "attempt": {"type": "integer", "minimum": 1}, "deadline_at": {"type": ["string", "null"], "format": "date-time"}
      }
    }
  }
}
```

`context` 即 `contracts/invocation.py · InvocationContext` 的全部公开字段。**不传** `SecurityContext`（§4.3）。

- `outputSchema` MUST 为 `ModuleResult` 的 JSON 形状（§5.3）。
- `_meta.staffdeck` MAY 附加 `{"operation": "knowledge.search/v1", "side_effecting": false, "idempotency_key_fields": [...]}`；客户端读取 `side_effecting` 覆盖本地推断（本地推断在 `capabilities/host.py · CapabilityHost.invoke_proxy`）。

#### 3.B.6 调用与结果

- 请求：`tools/call`，`params.name` = 工具名，`params.arguments` = 上述三段，`params._meta.progressToken` = `invocation_id`。
- 成功：`result.structuredContent` = `ModuleResult` JSON；`result.content[0] = {type:"text", text: <同一 JSON 序列化>}`（与 StaffDeck 服务端输出对称，`CapabilityMcpServer._call_tool`）；`result.isError = !success`。
- 客户端 MUST 只信 `structuredContent`；缺失时解析 `content[0].text` 为 JSON；两者皆无 → `fail("PROVIDER_INVALID")`。
- `data` 序列化后 ≤ 256 KiB；`citations` / `artifacts` 元素为 JSON 对象。`artifacts` 只允许引用（`{"name","uri","mime","size","sha256"}`），二进制不走 MCP 报文。

#### 3.B.7 错误对象

两层：

1. **JSON-RPC 错误**（`error.code/message/data`）表示协议/传输层失败，客户端映射：`-32601` → `UNSUPPORTED_CAPABILITY`，`-32602` → `INVALID_ARGUMENTS`，`-32603` / `-32000..-32099` → `PROVIDER_ERROR`；HTTP `401/403` → `PROVIDER_UNAUTHORIZED`，`404` → `UNSUPPORTED_CAPABILITY`，`429` → `PROVIDER_THROTTLED`，`5xx` / 连接失败 → `PROVIDER_UNAVAILABLE`，超时 → `TIMEOUT`。
2. **工具级错误**（`isError=true`）表示业务失败，`structuredContent.error = {"code","message","details?"}`。`code` MUST 取自 §8 或以 `<VENDOR>_` 前缀自定义。远程服务若**确定请求未触达外部系统**，MUST 返回 `capabilities/ledger.py · NOT_SENT_CODES` 内的码（如 `INVALID_ARGUMENTS`、`NOT_FOUND`、`NOT_ALLOWED`、`TOOL_NOT_AVAILABLE`），这样 ledger 记 `failed` 并释放幂等占位；否则副作用调用一律记 `outcome_unknown`。

#### 3.B.8 幂等键传递

- 副作用调用带 `X-StaffDeck-Idempotency-Key` 与 `invocation.side_effect_key`（计算见 §5.2）。远程服务 SHOULD 以该键去重并返回首次结果；StaffDeck 侧 ledger 本就在调用前 `replay_or_block`，远程去重是第二道防线。
- 同键重放的响应 SHOULD 在 `extensions.replay = {"idempotent_replay": true, "replayed_from_invocation_id": "..."}`（与本地形状一致，`InvocationLedger.replay_or_block`）。

#### 3.B.9 超时与重试

- 单次超时 = `min(endpoint.timeout_ms, host.slot.remaining_seconds()*1000)`；默认 60 000 ms（对齐上游 deepseek-harness 0.1.2-alpha.2 的 `toolCallTimeoutMs`）。
- 只读调用（`side_effecting=false`）：对 `PROVIDER_UNAVAILABLE` / `PROVIDER_THROTTLED` / `TIMEOUT` 最多重试 2 次，退避 500 ms、2 s，`X-StaffDeck-Attempt` 与 `invocation.attempt` 递增。
- 副作用调用：**永不自动重试**；超时 → `fail("TIMEOUT")`（非 NOT_SENT → ledger `outcome_unknown`，人工在 `POST /ledger/{id}/reconcile` 结算）。
- 取消：宿主 `LifecycleFence` 命中后客户端中止 HTTP 请求；若副作用请求已发出则结果同上。

#### 3.B.10 健康检查

- 预检：§3.B.4 三步。
- 运行期：每 30 s `tools/list`（或 `ping`）一次；连续失败 3 次标记 `unhealthy`，在 `GET /status` 的 `modules_unhealthy[]` 报告；调用时 `unhealthy` 不拒绝（让真实调用决定），但发出 `capability_provider_unhealthy` 事件。
- 无连接态，无重连拓扑；按请求重试。

---

### 3.C 事件 / 通知型 —— HTTP Webhook【待实现】

适用：`event.observer`（单向出站）、`handoff.notifier`（出站 + 同步应答）、`staff.channel`（双向）、`handoff.reply_endpoint` / `staff.ingress`（入站）。进程内适配模块 **`webhook.remote`**（建议文件 `events/webhook.py`）按 manifest `metadata.access="webhook"` 安装到对应槽，实现该槽的鸭子类型（§3.A.3）并把调用翻译成 HTTP。

#### 3.C.1 出站投递（StaffDeck → 模块）

- `POST <endpoint.url>`，`Content-Type: application/json; charset=utf-8`。
- 头：`X-StaffDeck-Event: <event_type>`、`X-StaffDeck-Delivery: <uuid4>`（投递 id，重试保持不变）、`X-StaffDeck-Timestamp: <unix seconds>`、`X-StaffDeck-Module: <module_id>`、`X-StaffDeck-Contract: v1`、`X-StaffDeck-Signature: sha256=<hex HMAC-SHA256(secret, timestamp + "." + raw_body)>`，`secret` 来自 `endpoint.signing_secret_env`。
- 信封（所有事件型 Webhook 共用）：

```json
{
  "spec_version": "v1",
  "delivery_id": "9c7e…",
  "event_type": "capability_invoked",
  "occurred_at": "2026-09-03T08:12:45.120Z",
  "tenant_id": "tenant_demo",
  "session_id": "sess_…",
  "module_id": "acme.audit_sink",
  "payload": { }
}
```

- 应答：`2xx` = 确认；`410 Gone` = 模块要求停用（下次装配标记 `enabled=false` 并记事件 `module_unsubscribed`）；其他 `4xx` = 丢弃不重试并记 `webhook_rejected`；`5xx` / 超时（5 s）/ 连接失败 = 重试。
- 重试：指数退避 1 s、5 s、30 s、2 min、10 min，共 5 次；仍失败记 `webhook_delivery_failed` 并丢弃。投递队列进程内内存 + 落库 `agent_events`（`event_type="webhook_delivery"`）便于审计；**MUST 异步**（observer 不得阻塞 Turn）。
- 去重：接收方按 `delivery_id` 去重；同一 `delivery_id` 的重放 MUST 返回首次应答。
- 接收方 MUST 验签并拒绝 `|now - timestamp| > 300 s`。

#### 3.C.2 `handoff.notifier` Webhook（同步应答）

同 3.C.1 信封，`event_type="human_handoff_notify"`，`payload = {"handoff_id","assignee_user_id","pending_question","context_summary","session_id","agent_id"}`。应答体 `{"notify_message_id": "<string|null>"}`；宿主把它写回 `HumanHandoffRequest.notify_message_id`（`HandoffCore.notify`）。超时 5 s 视为失败（Core 捕获并记 `human_handoff_notify_failed`）。

#### 3.C.3 入站（模块 → StaffDeck）

- 端点 `POST /api/enterprise/harness/hooks/{module_id}/inbound`。
- 鉴权：同一 HMAC 方案（模块用同一 secret 签，头 `X-StaffDeck-Signature`）；另接受 `Authorization: Bearer <workload token>`。
- 体：`{"spec_version":"v1","delivery_id":"…","kind":"handoff_reply"|"channel_inbound","tenant_id":"…","payload":{…}}`。
  - `handoff_reply`：`payload` 即 `ReplyResolver.resolve()` 的 `inbound`；宿主用 `SecurityContext = profile.identity.from_service("webhook:"+module_id, tenant_id)` 跑 `HandoffCore.reply(ctx, resolver=module_id, inbound, source="webhook")`，PEP `handoff.reply/v1` 照常。
  - `channel_inbound`：`payload` 为 `app/channels/adapters/base.py · ChannelInbound` 的 JSON 投影；经 `ChannelHost.authorize_receive` 后进入 durable inbox。
- 应答：`202 {"accepted": true, "delivery_id": "…"}`；重复 `delivery_id` 返回 `200 {"accepted": true, "duplicate": true}`；验签失败 `401`；租户不匹配 `403`；信封不合规 `422`；同 `delivery_id` 不同体 `409`。

#### 3.C.4 `staff.channel` 双向

出站 `channel.send`：信封 `event_type="channel_send"`，`payload={"binding_id","target":{…},"text","idempotency_key"}`（对齐 `ChannelAdapter.send` 签名）；应答 `{"message_id": "<string|null>"}`。入站按 3.C.3 `channel_inbound`。Send PEP 与 supervised 检查仍在 `ChannelHost.stage_send`。

---

## 4. 统一权限（PEP）约定

### 4.1 原则

1. **模块声明，宿主执行**：模块只在 manifest `policy_actions` 里声明它的操作需要哪些策略动作；宿主在调用前 `Guard.require(ctx, operation, resource)`（`security/profile.py · Guard.require`）。【已实现】落点：`CapabilityHost._pep`（下发给能力提供者之前，对 tool/mcp/a2a 的活行 `Tool` + 其 `MCPServer`、`general_skill`、`knowledge` 选中 Base、`sandbox` 各 require 一次，无策略映射的操作 fail closed）、`KnowledgeFacade.search`、`GeneralSkillFacade.consume`、`ToolFacade.invoke`（工具 + MCP server 两次）、`SandboxFacade.execute`、`HandoffCore.create / assign / reply / close / cancel`、`ChannelHost.authorize_receive`（`channel.receive/v1` + `staff.use/v1`）/ `stage_send`（`channel.send/v1`）、`HarnessV3Engine._ensure_turn_context`（`staff.use/v1`）。Guarded Facade 仍按活行复核（defence in depth）。
2. **拒绝为默认；失败即关闭**：`contracts/security.py · PepPort` 注释；BUSINESS_BASE 任何传输/契约错误都是 deny（§4.4）。
3. **快照围栏不是 PEP**：`ActivationSlot` / `LifecycleFence` / `_activation_allowlist` hook 只收窄不授权（`capabilities/host.py` 模块 docstring；`pipeline_host.py · _activation_allowlist`）。
4. **远程模块不得自行判权**：MCP / Webhook 模块收到的是已通过 PEP 的调用；MAY 做自身的租户隔离校验，MUST NOT 基于用户身份做业务判定，MUST NOT 期待宿主转发用户 token。
5. **缺少 PEP 绑定即无法启动**：`Guard(profile=None)` → `PEP_BINDING_MISSING`（`Guard.__init__`）；注册表 `seal()` 同样检查（§2.5）。

### 4.2 操作 → (action, resource_type) 映射【已实现】

`contracts/security.py · PolicyActionMapper` + `DEFAULT_ACTION_MAP`（23 项）：

| 操作 | action | resource_type |
|---|---|---|
| `knowledge.search/v1` | use | knowledge_base |
| `general_skill.consume/v1` | use | skill |
| `tool.invoke/v1` / `mcp.invoke/v1` / `a2a.invoke/v1` | use | tool |
| `sandbox.execute/v1` | execute | capability |
| `artifact.publish/v1` | write | session |
| `handoff.request/v1` | create | handoff |
| `handoff.assign/v1` | manage | handoff |
| `handoff.reply/v1` | edit | handoff |
| `sop.execute/v1` | execute | sop |
| `sop.read/v1` / `sop.advance/v1` / `sop.resume/v1` | view / advance / resume | sop |
| `staff.use/v1` / `staff.manage/v1` | use / manage | agent |
| `channel.receive/v1` / `channel.send/v1` | receive / send | channel |
| `memory.read/v1` / `memory.write/v1` | read / write | session |
| `team.delegate/v1` | delegate | team |
| `model.use/v1` | use | model_config |
| `runtime.turn/v1` | execute | runtime |

宿主 MAY 用 `security/profile.py · guard_for(module_id, mapping=...)` 覆盖。未映射的操作在 `Guard.decide` 时 `KeyError`。【待实现】第三方新增操作家族通过 manifest `metadata.policy_map` 声明，`seal()` 合并到进程级映射，与 `DEFAULT_ACTION_MAP` 冲突则拒绝（`CONTRACT_INCOMPATIBLE`）。

### 4.3 `SecurityContext` 传递

`contracts/security.py · SecurityContext` 由 `IdentityPort` 构造（`security/oss_local.py · LocalIdentity`，`security/business_base.py · BaseIdentity`），模块永不构造。

- 进程内：宿主持有 `host.security_context`；provider MAY 读取 `principal_id / tenant_id / principal_type / tenant_role / channel` 用于日志与路由，MUST NOT 依据 `token_id / workload / attributes` 自行推导权限。
- 进程外：**不传 `SecurityContext`**。远程模块只收到 `InvocationContext`（含 `user_id`，仅用于审计）与 `Authorization` 工作负载 token（§3.B.3）。需要"以用户身份"访问外部系统的模块 MUST 通过自己的凭据映射，不经 StaffDeck 转发用户 token。
- 服务主体：`from_service(service_id, tenant_id)` 在 OSS_LOCAL 得到 `principal_type="service"`（`LocalPep` 只允许 use 类动作 + receive/send/write，且资源 `enabled/binding_status` 仍约束），在 BUSINESS_BASE 得到 `principal_type="workload"`。

### 4.4 BUSINESS_BASE 线协议【已实现】

`security/business_base.py`。Base 只治理**租户级共享资源**：`BASE_GOVERNED_TYPES = {agent, sop, skill, general_skill, tool, mcp_server, knowledge_base, team}`（送到 Base 时 `mcp_server → tool`、`general_skill → skill`，`_BASE_RESOURCE_TYPE`）。运行时内部类型 `LOCAL_TYPES = {session, handoff, channel, model_config, runtime, capability, tenant}` 在 `BasePep.authorize` 中**本地评估**（`BasePep._local_decision` 委托 `LocalPep` 的租户 / 归属 / 绑定规则，决策 `reason` 前缀 `local policy: `，`source="BUSINESS_BASE"`）。**其他任何资源类型一律拒绝**（`unknown resource type … under BUSINESS_BASE`，fail closed）。租户不一致直接 deny（不发网络请求）。

**动作收敛**（`_BASE_ACTION`，`_TYPE_ACTION`）：`execute / advance / resume / receive / send → use`；`read → view`；`write → edit`；`delegate → view`；特例 `(knowledge_base, use) → retrieve`（Base 区分 can_use 与 can_retrieve；运行时检索是 retrieve）。其余动作原样。

**id 规范化**：`canonical_id()` 是**单射**编码：非 `[A-Za-z0-9._-]` 的字符按 `_xx`（十六进制码点）编码（`kb:1 → kb_3a1`），首字符不是字母数字时前置 `x`，截断 255；空 id 保持为空并由 Base 拒绝（不再映射为共享的 `unknown`）。principal / tenant / resource id 均经此处理。

**请求** `POST {base_authz_url}/internal/v1/authz/check`（`BaseAuthzClient.check`），头 `X-Base-Service-Token: <decision_token>`、`X-Request-ID: <uuid4 hex>`，超时 `timeout_seconds`（默认 3 s），`follow_redirects=False`：

```json
{
  "principal": {"type": "user", "id": "<canonical principal_id>", "tenant_id": "<canonical tenant_id>"},
  "action": "retrieve",
  "resource": {"type": "knowledge_base", "id": "<canonical id>", "tenant_id": "<canonical tenant_id>"},
  "request_id": "<uuid4 hex>",
  "consistency": "minimize_latency"
}
```

**传输层语义**（`BaseAuthzClient._post`）：`httpx.HTTPError` 或 `502/503/504` → 重试 **1 次**（共 2 次尝试）后 `_Unavailable`；`401` → `_Misconfigured("Base rejected the decision token (401)")`；`403` → `_Misconfigured("credential lacks decision scope (403) …")`（填了控制令牌）；其他非 200 / 非 JSON / 非对象 → `_Contract`。

**响应** `200 {"request_id","allowed","reason","decision_id","revision"}`（`BasePep._decision`）：

| 情况 | 决策 |
|---|---|
| `request_id` 不匹配或 `allowed` 非布尔 | `deny("authorization unavailable: contract error")` |
| `allowed=false, reason="authorization_pending"`（`PENDING_REASON`；Base **不用** 409） | `deny(pending=True)`；`BasePep.authorize` 以 `pending_poll_seconds`=0.05 s 起、每次加倍、上限 `pending_poll_max_seconds`=0.25 s 退避重试（每次换新 `request_id`），直到 `pending_timeout_seconds`（默认 3 s）后返回 pending deny |
| `allowed=false, reason ∈ {openfga_unavailable, authz_unavailable}` | `deny("authorization unavailable: <reason>")` |
| 其他 | `Decision(allowed, reason, decision_id, revision, source="BUSINESS_BASE")` |
| `_Misconfigured` | `deny("authorization unavailable: misconfigured — …")` |
| `_Unavailable` / `_Contract` | `deny("authorization unavailable: …")` |

`Guard.require`：`reason.startswith("authorization unavailable")` 或 `pending` → 抛 `AuthorizationUnavailable`（`CapabilityHost` 记 ledger `denied`，不发 `capability_denied` 事件）；否则 `PermissionDenied`。

**批量** `POST /internal/v1/authz/batch-check`，体 `{"requests": [<check 体>…]}`，1..100 条（`BaseAuthzClient.batch_check`；`BasePep.filter` 分块）；响应 `{"decisions": [{"request_id","allowed",…}]}`；非显式 `allowed:true` 一律剔除，整块异常则整块不授予；返回保持调用方顺序。非治理类型走 `LocalPep.filter`。

**工作负载凭证**（`BaseWorkload`）：
1. `POST {base_identity_internal_url}/internal/v1/identity/oauth/token`（form：`grant_type=client_credentials, client_id, client_secret`）→ `access_token`，按 `expires_in`（默认 300 s）减 30 s 缓存（`service_token`）；401 → `RuntimeError`。
2. 若配置了 authz 客户端：`POST {base_authz_url}/internal/v1/authz/workload-delegation-check`（经 `BaseAuthzClient._post`，同令牌头与重试），体 `{principal, agent_id, request_id}`；`allowed` 非 true → `RuntimeError("workload delegation refused: …")`。
3. `POST {identity}/internal/v1/identity/workload-delegations`（`Authorization: Bearer <服务令牌>`、`X-Request-ID`、`Idempotency-Key = run_idempotency_key`），体含 `runtime_client_id, tenant_id, actor_user_id, agent_id, authorization_{decision_id,request_id,revision,proof}, run_idempotency_key, run_attempt, request_digest, requested_scopes`（默认 `DEFAULT_SCOPES = knowledge:retrieve, sop:execute, skill:package.read, tool:config.read`）→ `delegation_token`。
4. `POST {identity}/internal/v1/identity/workload-sessions`，体 `{delegation_token, run_id, run_idempotency_key, run_attempt, request_digest}` → `{session_id, access_token, token_type, expires_in, scope}`。
`mint()` 返回 `{"kind":"base_workload","audience","delegation_id","session_id","access_token","token_type","expires_in","scope"}`；非 200/201 或非对象响应 → `RuntimeError`。未配置身份中心时 `build_business_base_profile` 装入 `_Unconfigured`，`mint()` 抛 `RuntimeError`（不会静默造本地 token）。

**身份**：`BaseIdentity.from_user` 取 `subject_id or id` 作 principal，`provider` 默认 `base_identity`；`from_service` → `principal_type="workload", tenant_role="service"`。

### 4.5 连接测试与重启预检【已实现】

`security/base_preflight.py · preflight_base(conn, tenant_id=, principal_id=)` 全为只读：

| # | 检查 | 端点 | fatal |
|---|---|---|---|
| 1 | 权限中心健康检查 | `GET {authz}/health`（200 且 `status ∈ {null, "ok"}`） | 是 |
| 2 | 权限中心就绪检查 | `GET {authz}/ready`（404 视为"未提供，跳过"） | 否 |
| 3 | 决策令牌 | `POST {authz}/internal/v1/authz/agents/public/list`，体 `{tenant_id, request_id}`，头 `X-Base-Service-Token`；200 取 `revision`；401 令牌被拒；403 填了控制令牌；502/503/504 暂不可用 | 是 |
| 4 | 身份中心（工作负载凭证） | `POST {identity}/internal/v1/identity/oauth/token`（仅当 url + client_id + secret 齐全） | 否 |
| 5 | 本租户同步状态 | `POST {authz}/internal/v1/authz/check`（`view tenant`）；`reason` 映射 `unknown_principal / inactive_principal / unknown_resource / authorization_pending` 为中文提示 | 否 |

报告 `PreflightReport{ok, checks[{name, ok, message, status, latency_ms, fatal, detail}], authz_revision, tested_at}`。

- `POST /base/test`（`api/admin.py · harness_base_test`）：可带未保存的 `base` 补丁；只有当测试值 == 已保存值时才把 `last_test_ok / last_test_at` 写回装配（防止"测通未保存的值"解锁重启）。
- **地址策略**（`modules/config.py · check_url_policy`）：权限中心 / 身份中心地址默认只允许本机、内网、`*.internal` / `*.local` 主机；公网主机必须 `https://` 且在 `Settings.base_url_allowlist`（逗号分隔的 fnmatch 主机模式，`*` 放开）之内。`PUT /config` 与 `POST /base/test` 均执行。
- **密钥配对规则**：测试或切换到一个**新的**地址时，必须在同一请求里给出该地址对应的密钥（决策令牌 / 运行时客户端密钥）；否则 400。已保存或来自环境的密钥永不会被发往管理员刚输入的地址（防止用 `/base/test` 把部署密钥外泄到任意主机）。
- **环境快照**（`modules/config.py · snapshot_env`）：启动时记录 `.env` 的 Base / 引擎 / 模块字段；此后所有"留空回落到环境"的判断都读快照，而不是被 `apply_overrides` 改写过的 `Settings`——所以清空一个管理员填写的字段，真正回到环境值，`masked()` 的 `*_source` 也如实报告。
- `PUT /config` 切到 `BUSINESS_BASE` 时要求生效值有 `authz_url` + `decision_token`（否则 400）；`base` 签名变化会清空 `last_test_*`。
- `runtime/assembly.py · preflight_assembly` 在 `restart` 时对 `BUSINESS_BASE` 实时跑一遍 `preflight_base`（不依赖 `last_test_ok`）；不通过 → `AssemblyFailed("无法切换到企业版权限：…")`，运行时**未被触碰**。

---

## 5. 调用与幂等

### 5.1 `ModuleInvocation`（`contracts/invocation.py`）【已实现】

| 字段 | 类型 | 说明 |
|---|---|---|
| `invocation_id` | str | 宿主生成；引擎路径 = `ctx.trace_id` = `hcall_<n>_<hex8>`（`CapabilityMcpServer._call_tool`），缺省 `hcall_<ms>` |
| `module_id` | str | **注意**：宿主传入的是操作家族名（`op.split(".",1)[0]`，如 `knowledge`、`tool`），不是 provider 的 `module_id`（`CapabilityHost.invoke_proxy`）。因此更换提供者不改变 `side_effect_key` / `request_digest`，幂等历史跨实现有效。ledger 的 `tool_name` = `f"{module_id}:{operation}"` |
| `operation` | str | `name/vN` |
| `arguments` | JSON 对象 | `tool.invoke/v1` 为 `{"tool_id", **inner}`；`sandbox.execute/v1` 为 `{"tool", "arguments"}` |
| `context` | `InvocationContext` | 宿主盖章；6 个必填非空：`tenant_id, agent_id, user_id, session_id, turn_id, channel`；`attempt ≥ 1`；`user_id` 匿名时为 `"anonymous"` |
| `binding_id` | str? | 资源 id（`tool_id` / `skill_id`） |
| `side_effecting` | bool | 推断规则（`invoke_proxy`）：HTTP 工具 `method ∈ {POST, PUT, PATCH, DELETE}`；`config_json.idempotency.enabled=false` 强制 false；`tool_type == "a2a"` 总为 false（A2A 客户端按 `invocation_id` 自去重）；sandbox 写类工具 `{write_file, exec_command, run_skill_script, apply_patch, delete_file}` 为 true；其余 false |
| `idempotency_key_fields` | tuple[str] | 来自工具 `config_json.idempotency.key_fields` |
| `replayable` | bool（默认 true） | `config_json.idempotency.enabled=false` 时仍 `side_effecting=True`（模糊失败 → `outcome_unknown`），但 `side_effect_key()` 返回 `None`（不重放、不去重） |
| `metadata` | JSON | 保留 |

### 5.2 `side_effect_key` 计算（`ModuleInvocation.canonical_arguments / request_digest / side_effect_key`）【已实现】

```
args'           = arguments                                   若 idempotency_key_fields 为空
                = {k: arguments.get(k) for k in sorted(fields)} 否则
canonical_args  = json.dumps(args', ensure_ascii=True, sort_keys=True, separators=(",",":"), default=str)
request_digest  = sha256(f"{module_id}|{operation}|{canonical_args}")
side_effect_key = None                                        若 not side_effecting 或 not replayable
                = sha256("|".join([tenant_id, task_frame_id or session_id, step_id or "", module_id, operation, canonical_args]))
```

实测：`arguments={"tool_id":"tool_1","b":2,"a":1}`, `key_fields=("tool_id","a")` → `canonical='{"a":1,"tool_id":"tool_1"}'`，`request_digest=7008e10f…4999`。中文按 `\uXXXX` 转义（`ensure_ascii=True`）。远程模块自行计算时 MUST 得到同一值。

### 5.3 `ModuleResult` 与 `Receipt`【已实现】

`ModuleResult` JSON 形状（MCP / Webhook 线上形状同此）：

```json
{
  "success": true,
  "data": {},
  "error": {"code": "…", "message": "…", "details": {}},
  "citations": [ {"…"} ],
  "artifacts": [ {"…"} ],
  "extensions": {}
}
```

`ModuleResult.ok(data, **kw)` / `ModuleResult.fail(code, message, **kw)`。宿主对 SDK 异常统一放 `extensions.details`。

`Receipt{invocation_id, status, request_digest, side_effect_key, replayed_from, ledger_id, started_at, finished_at, error}`；`status ∈ {started, completed, failed, outcome_unknown, cancelled, denied}`。经 MCP 返回给引擎时只带 `receipt: {invocation_id, status, replayed_from}`，且 `data` / `error` / `citations` / `artifacts` 只在非空时出现（`CapabilityMcpServer._call_tool`）。

### 5.4 Ledger 状态机与 `outcome_unknown`（`capabilities/ledger.py · InvocationLedger`）【已实现】

```
started ──success───────────────────────────► completed        （占位保留；同键重放返回缓存）
        ──fail & (code ∈ NOT_SENT_CODES or 非副作用)──► failed  （释放占位 logical_action_key=NULL，可重试）
        ──fail & 副作用 & code ∉ NOT_SENT_CODES──► outcome_unknown（占位保留，阻断重放）
        ──ActivationFenced（provider 抛出）──► cancelled        （释放）
        ──PermissionDenied / AuthorizationUnavailable──► denied（释放）
```

`NOT_SENT_CODES = {NOT_FOUND, DISABLED, NOT_ALLOWED, UNSUPPORTED_TOOL_TYPE, TOOL_NOT_AVAILABLE, CAPABILITY_AUTHORIZATION_REVOKED, CAPABILITY_SNAPSHOT_CHANGED, CAPABILITY_NOT_ACTIVATED, CAPABILITY_NOT_AVAILABLE, INVALID_ARGUMENTS, PERMISSION_DENIED, ACTIVATION_FENCED, REQUIRED_SLOT_MISSING, SLOT_NOT_BOUND, AUTHORIZATION_UNAVAILABLE}`。

- 调用前 `replay_or_block`：同键 `completed` 且缓存 `success=true` → 直接返回缓存结果，`data` 合并 `{"idempotent_replay": true, "replayed_from_invocation_id": <prior.id>}`，`extensions.replay` 同；同键其他状态 → 抛 `OutcomeUnknown`（宿主返回 `fail("OUTCOME_UNKNOWN")`，`extensions.details = {side_effect_key, prior_invocation_id, prior_status}`）。
- 占位是数据库唯一约束（`app/db/models.py · HarnessInvocationRecord.logical_action_key unique=True`）；并发抢占失败者 `IntegrityError` → 回滚 → 重放（`_Replayed`）。
- 审计截断：`arguments_json` / `result_json` 序列化 > 4 000 字符时存 `{"_truncated": true, "preview": …}`（`_audit`）；`response_cache_json` 只在 `completed` 时保留完整结果。
- 人工结算：`POST /api/enterprise/harness/ledger/{id}/reconcile {tenant_id, status: completed|failed, result?}`（`InvocationLedger.reconcile`；非 `outcome_unknown` 行 → 409）；记事件 `invocation_reconciled`。
- **模块义务**：确定未触达外部系统时返回 `NOT_SENT_CODES` 内的码；不确定时不要伪装成 `failed`。
- 宿主在 provider 运行前就返回的码（`UNSUPPORTED_CAPABILITY`、`PROVIDER_INVALID`、`PROVIDER_UNAUTHORIZED`、`ENGINE_UNAVAILABLE`）已在 `NOT_SENT_CODES` 内：副作用调用遇到它们记 `failed` 并释放占位，可直接重试。

### 5.5 激活与围栏（`capabilities/host.py`）【已实现】

- `ActivationSlot{snapshot, generation, turn_id, session_id, active_sop_id, active_node_id, deadline_monotonic, closed, finish, allowed_next_steps}`；`session_id` 由 `HarnessV3TaskAgent.run` 填入，供观察者扇出定位会话（§6.2）。`grants()` / `allowed()` 按当前 SOP 位置取快照授权；`remaining_seconds()` 基于单调时钟。
- `LifecycleFence.check(slot)`：`closed` → "turn is closed"；generation 不匹配 → "stale generation"；`is_cancelled()`（`HarnessV3TaskAgent` 提供线程安全的 id 版取消检查）→ "turn cancelled"；截止已过 → "step deadline expired"；均抛 `ActivationFenced`。
- `_fence_resource`：`knowledge.search/v1` 要求 `allowed()["knowledge_base"]` 非空；`general_skill.consume/v1` / `tool.invoke/v1` / `mcp.invoke/v1` / `a2a.invoke/v1` 要求 `binding_id ∈ allowed()[rtype]`；其他操作不围栏。
- `CapabilityMcpServer._call_tool` 无活跃 activation token → `ACTIVATION_FENCED`；`_list_tools` 返回空表。
- `task.finish/v1`（`CapabilityHost._finish_task`）关闭 `ActivationSlot`（`slot.closed=True`）：本轮后续能力调用返回 `ACTIVATION_FENCED`，引擎 turn 循环在捕获 finish 后立即退出（子进程在关闭 finally 里被关）。

### 5.6 取消

- `CancelCommand{session_id, turn_id?, reason, requested_by?}` 是数据形状；当前由 legacy `is_chat_turn_cancelled` 驱动（`modules/kernel.py · CancellationModule`；`bridge/task_agent.py · HarnessV3TaskAgent.run` 的 `cancelled_threadsafe`）。
- 上游 deepseek-harness 0.1.2-alpha.2 协议无 cancel 方法：Turn 取消 = 抛 `HarnessExecutionCancelled` 并关闭子进程（`HarnessV3TaskAgent.run` finally）。引擎 turn 循环每 0.25 s 轮询 SDK 通知队列，使 `is_chat_turn_cancelled` 在生成中途也能被履行。
- 取消不进入 `arguments`（附录 B Typert 约定）；远程模块通过 HTTP 请求中止 + `deadline_at` 感知。

---

## 6. 事件与日志

### 6.1 宿主发出的事件（模块 MUST NOT 伪造这些名字）【已实现】

| 事件名 | 发出点 | 走向 | payload |
|---|---|---|---|
| `capability_provider_selected` | `CapabilityHost._dispatch` | trace + fanout | `{operation, module_id, module_version}` |
| `capability_invoked` | `CapabilityHost.invoke` | trace + fanout | `{operation, resource, status, invocation_id}` |
| `capability_denied` | `CapabilityHost.invoke`（仅 `PermissionDenied`） | trace + fanout | `{operation, resource, reason}` |
| `harness_v3_task_finished` | `CapabilityHost._finish_task` | trace + fanout | `{status, next_step_id}` |
| `general_skill_trace` / `harness_mcp_app_view` | `capabilities/facade.py · FacadeDeps.emit` | 仅 trace | 见各处 |
| `hook_decision`（仅非 pass） / `hook_failed` | `InteractionPipelineHost.run` | 仅 trace | `{point, handler, kind, reason}` / `{point, handler, error}` |
| `composition_snapshot_compiled` | `HarnessV3Engine._ensure_turn_context` | `events.record`（DB） | `{snapshot_id, staff_id, grants, sops, security_profile, execution_engine}` |
| `harness_v3_process_started` / `harness_v3_turn_steered` / `harness_v3_turn_failed` | `HarnessV3TaskAgent.run` | trace | `{model, base_url, workspace, boot_ms}` / `{reason, message}` / `{error}` |
| `harness_v3_turn_started`, `harness_v3_step_started`, `llm_call_started`, `stream_delta`, `harness_action_created`, `harness_tool_result`, `harness_v3_assistant_message`, `harness_v3_turn_ended`, `harness_v3_hook_event`, `harness_v3_compaction` | `events/relay.py · relay_event`（引擎 `session.event` 翻译） | trace + fanout（`SessionEventRelay.__call__`） | 见 `relay_event` |
| `human_handoff_created` / `assigned` / `notify_failed` / `closed` / `cancelled` | `HandoffCore.create / assign / notify / close / cancel` | DB `agent_events` 直写（**不** fanout） | `{handoff_id, …}` |
| `human_handoff_notified` | `handoff/core.py · WebInboxNotifier.notify` | DB 直写 | `{handoff_id, assignee_user_id, channel:"web"}` |
| `runtime_restarted` / `runtime_restart_failed` / `module_placed` / `module_inspected` / `staff_engine_changed` / `invocation_reconciled` | `api/admin.py` 各端点 | DB 直写（`session_id="runtime"` 或 `agent:<id>`） | 含 `by` |

所有 `CapabilityHost._emit` 事件自动附加 `snapshot_id` 与 `execution_engine:"harness_v3"`。在 MCP 工作线程发出的 trace 事件先缓冲，附加 `occurred_at`（ISO-8601 UTC），由引擎线程回放持久化（`HarnessV3TaskAgent._threadsafe_trace / _flush_trace`）；执行日志按 `occurred_at` 排序（`api/admin.py · _event_ts`）。

### 6.2 观察者扇出（`events/relay.py`）【已实现】

`_fanout(tenant_id, session_id, event_type, payload)` 的接收者 = 显式 `register_observer()` 登记的对象 ∪ 注册表中**启用**的 `event.observer` 模块（`_registry_observers`：`peek_registry()` 返回的活动注册表中 `on_event` 可调用者），按 `id()` 去重；顺序：显式登记者先、注册表条目后。每个观察者的异常单独捕获记日志，不影响其他观察者与 Turn。

`peek_registry()` **只读不建**：注册表尚未构建或正在重建时返回 `None`，观察者只是漏掉这条事件。宿主、Hook、Relay 一律用 `peek_registry()`；`get_registry(settings)` 只允许拥有 settings 的装配代码调用——无 settings 的调用会抛 `REGISTRY_NOT_BUILT`，绝不会在重启窗口里悄悄构建一套默认装配。

- 来源一：引擎 `session.event` 经 `SessionEventRelay`（`session_id` = StaffDeck 会话 id）。
- 来源二：`CapabilityHost._emit` 经 `fanout_event`，`session_id = slot.session_id or snapshot.staff_id`。
- 停用的观察者模块不接收事件；注册表未构建（早期启动、单测）时只有显式登记者接收。
- 观察者 MUST 幂等（重放可能发生），MUST 不阻塞（§3.A.5）。
- 【待实现】Handoff / 管理 API 直写 DB 的事件也经 `fanout_event`。

### 6.3 模块自身事件【已实现】

进程内能力提供者通过宿主 `host.trace(event, payload)` 发出（走 trace sink，不走 fanout）；事件名 MUST 为 `<vendor>_<name>` 小写下划线，payload 为 JSON 对象且 ≤ 4 KiB。要出现在执行日志必须登记到 `api/admin.py · LOG_EVENT_TYPES / _LOG_PICK`；【待实现】manifest `metadata.log_events: {event_type: tag}` 在装配时合并。

### 6.4 执行日志（`GET /log?session_id=`）【已实现】

`api/admin.py · _log_entries` 把 `agent_events`（仅 `LOG_EVENT_TYPES` 中的类型）与 `harness_invocations`（每行拆成 `tool/call` + `tool/result`）合并，按 `(ts, tool/call 先)` 排序，取末 `limit` 条。`capability_invoked` **不在** `LOG_EVENT_TYPES` 中（调用信息来自 ledger 行），`capability_provider_selected` → `tool/route`，`capability_denied` → `tool/denied`。已知小缺陷：`hook/result` 的 `_LOG_PICK` 挑 `decision` 键，而 `hook_decision` payload 用的是 `kind`，日志行看不到决策类型。

---

## 7. 生命周期

### 7.1 装配文件 `staffdeck-runtime.json`（`modules/config.py`）【已实现】

路径：`Settings.harness_runtime_config_path` → 环境变量 `STAFFDECK_HARNESS_RUNTIME_CONFIG` → `<harness_v3_home 或 cwd>/staffdeck-runtime.json`（`config_path`）。原子写：`<path>.tmp` + `os.replace`（`save_overrides`）。磁盘形状（`RuntimeOverrides.to_stored`）：

```json
{
  "engine": "harness_v3",
  "security_profile": "BUSINESS_BASE",
  "disabled_modules": ["handoff.notifier.dingtalk"],
  "extra_modules": ["acme_pkg:register"],
  "placements": {"acme.sink": "governance.monitoring"},
  "base": {
    "authz_url": "https://base.example", "decision_token_enc": "<encrypt_secret>", "control_token_enc": "",
    "timeout_seconds": null, "pending_timeout_seconds": null,
    "identity_internal_url": "", "runtime_client_id": "", "runtime_client_secret_enc": "", "workload_audience": "",
    "last_test_ok": true, "last_test_at": "2026-09-03T08:00:00+00:00"
  },
  "updated_at": "2026-09-03T08:01:02+00:00",
  "updated_by": "admin"
}
```

- `engine ∈ {"harness_v3","harness_v2"}`，`security_profile ∈ {"OSS_LOCAL","BUSINESS_BASE"}`；非法值在 `normalized()` 回落到 `harness_v2` / `OSS_LOCAL`。`disabled_modules` 去重排序；`extra_modules` 保序；`placements` 最多 500 条。
- Base 密钥（`decision_token`、`control_token`、`runtime_client_secret`）以 `app.security.encryption.encrypt_secret`（应用密钥）加密存为 `*_enc`；解密失败（密钥轮换）得到空串而不是崩溃。API 输出用 `BaseConnection.masked`：密钥显示 `••••••••`，附 `has_<field>` 与 `<field>_source ∈ {admin, env, none}`；空字段回落到环境 `Settings.base_*`（`BaseConnection.effective`）。
- 文件缺失或损坏 → `defaults_from_settings(settings)`（环境默认）。
- 投影：`settings_updates` 把装配映射到 `Settings.harness_v3_enabled / security_profile / harness_disabled_modules / harness_modules / base_*`；`apply_overrides` 直接改写缓存的 `Settings`（真实装配），`settings_view` 返回 `model_copy` 视图（预检 / dry-run，不触碰缓存）。
- `saved` vs `applied` vs `pending`：`same_assembly` 比较 `engine, security_profile, disabled_modules, extra_modules`，涉及 `BUSINESS_BASE` 时再比较 `base.signature()`；`placements` 永不计入。

### 7.2 保存 `PUT /api/enterprise/harness/config`（`api/admin.py · harness_set_config`）【已实现】

请求 `AssemblyUpdate{tenant_id, engine?, security_profile?, disabled_modules?, extra_modules?, placements?, base?}`，缺省键 = 保持现值。校验：`engine` / `security_profile` 枚举（400）；`disabled_modules` 见 §1.2（400）；`extra_modules` 每项 `validate_spec`（400）；`placements` 值必须是合法子模块 id，`null`/`""` 表示删除（400）；`base` 用 `merge_update`（缺键保持，`null` 清空，掩码值保持原密钥）；目标为 `BUSINESS_BASE` 时生效值需有 `authz_url` + `decision_token`（400）。保存后返回 `AssemblyStateRead{saved, applied, pending, started_at, restart_count, last_restart_error, config_path}`。保存不改变运行时（`placements` 除外，立即生效）。

### 7.3 Dry-run `POST /api/enterprise/harness/modules/inspect`（`modules/inspect.py · inspect_spec`）【已实现】

请求 `{tenant_id, spec}`。流程：`validate_spec` → 基线 = 已保存装配（去掉本 spec）的 `settings_view` 上 `discover_and_install` 到**一次性** `ModuleRegistry` → 全部槽 `mark_guarded` → `_load_callable(spec)` 并以 `ctx={"settings": view, "disabled": set(saved.disabled_modules), "dry_run": True}` 调用 → 对新增模块计算 `movable / placement / already_installed` → 无错误则 `seal()`。不触碰活动注册表、缓存 `Settings`、引擎进程；但**会在管理进程内 import 候选包**（与 `/restart` 同信任级别；`sys.modules` 保留）。

响应：

```json
{
  "spec": "acme_pkg:register", "ok": true, "callable": "acme_pkg.register", "file": "/…/acme_pkg/__init__.py",
  "modules": [ { "…describe() 条目…", "movable": true, "placement": {"big_id": "governance", "sub_id": "governance.trace", "source": "manifest"}, "already_installed": false } ],
  "errors":   [ {"code": "…", "message": "…", "phase": "validate|baseline|import|register|seal", "details?": {}} ],
  "warnings": [ {"code": "…", "module_id?": "…", "message": "…"} ],
  "elapsed_ms": 12.3
}
```

`errors.code`：`INVALID_SPEC`（phase validate）、`BASELINE_FAILED`（baseline）、`ModuleSdkError` 子类的 `code`（register / seal）、其他异常类名大写如 `MODULENOTFOUNDERROR`（import / seal）。`warnings.code`：`UNKNOWN_CATEGORY`、`OPERATION_SHADOWED`、`DISABLED_BY_CONFIG`、`GUARD_SELF_DECLARED`（当前不可能触发，§3.A.2）、`NO_MODULES`。每次调用记事件 `module_inspected`。

【待实现】对 `remote_modules` 候选执行 §3.B.4 协商；对 Webhook 候选投递 `event_type="ping"`；`PUT /config` 前端在保存前调用。

### 7.4 归类 `PUT /api/enterprise/harness/modules/{id}/placement`（`api/admin.py · harness_set_placement`）【已实现】

请求 `{tenant_id, sub_id | null}`。`sub_id` 必须合法（400）；已安装的 K 模块或 `runtime.engine` / `security.pep` 槽模块拒绝（400）；**未安装**的 id 允许预先归类（`installed:false`）。写入 `placements`，立即生效、不触发 pending，记事件 `module_placed`，返回 `{module_id, sub_id, installed, placement, tree}`。

### 7.5 预检与重启 `POST /api/enterprise/harness/restart`（`runtime/assembly.py · restart_harness_runtime`）【已实现】

同一时刻只允许一次重启（`_restart_lock`，第二个请求 409 "已有一次重启正在进行"）。`_state_lock` 只保护簿记字段，**从不**跨网络 I/O 持有，所以 `GET /status` / `GET /config` 在重启期间照常返回并带 `restarting: true`。`restart_harness_runtime(..., drain_timeout_seconds=20)` 先做有界等待在途 Turn（`ActivationRegistry` 长度），把没等完的个数作为 `interrupted_turns` 返回。

顺序：
1. `wanted = load_overrides(settings)`；
2. `preflight_assembly(settings, wanted, tenant_id, principal_id)`：若 `BUSINESS_BASE`，要求生效 `authz_url` + `decision_token` 且 `preflight_base` 通过；再用 `build_registry(view)` 在一次性注册表上 `discover_and_install` + `seal()`（**只带注册器自己声明的受护槽**，与真实构建完全一致，所以 `PEP_BINDING_MISSING` 等 seal 错误在预检就会出现），并 `build_profile(view, registry=reg)`。任何失败 → `AssemblyFailed`（"无法切换到企业版权限：…" / "装配无法启动：<Exc>: …"），记入 `_last_restart_error`，**运行时未被触碰**；
3. `_build_components(settings, wanted)`：再次在局部构建新的注册表与安全配置（仍未触碰全局）；
4. `_drain_live_turns(drain_timeout_seconds)` 等待在途 Turn → `reset_runtime()`（关 MCP 服务端与引擎子进程）→ `_activate`：`apply_overrides(settings)` → `install_registry(reg)` → `install_profile(profile)`（原子替换，宿主看到的要么是旧装配要么是新装配）→ 若 `harness_v3_enabled`，`get_runtime`（`EngineUnavailable` 时若 `harness_v3_fallback_to_v2` 则只记 `runtime_error`，否则抛出）；
5. 成功：`_applied = wanted`，`restart_count += 1`，`_last_restart_error = None`，返回 `{security_profile, modules, registry_generation, mcp_url?, harness_v3_root?, harness_v3_home?, runtime_error?, interrupted_turns, restarted_at, restart_count, state}`，记事件 `runtime_restarted`；
6. 被中断的 Turn 会丢失引擎子进程（文档化代价）；它们在重启窗口内的能力调用得到 `ENGINE_UNAVAILABLE`（"运行时正在重启"），而不是落到默认装配上。

### 7.6 回滚与启动回退【已实现】

- **激活失败回滚**：步骤 4 抛异常（只可能是引擎拉起失败且未开启回退）→ `reset_runtime()` → 用重启前 `peek_registry() / peek_profile()` 取到的旧组件 `_activate(previous)` → `_last_restart_error = "<Exc>: …"` → 抛 `AssemblyFailed`；API 返回 **409**，`detail` 为 "重启失败，已恢复原有配置：…"（预检类错误则原文），记事件 `runtime_restart_failed`。已保存文件不动，`pending` 保持 true。
- **启动回退**（`start_harness_runtime`，由 `app/main.py` 启动钩子调用）：先 `snapshot_env(settings)`，再 `_build(settings, saved)`；失败且 saved ≠ 部署默认 → `_last_restart_error = "启动时无法应用已保存的装配，已回退到部署默认值：…"` → 三个 reset → `_build(settings, defaults)`（默认值取自环境快照，不受失败装配已写入 `Settings` 的字段影响），`info["fallback"]` 携带同一消息，`_applied = defaults`；文件不动，`/config` 显示 `pending=true` + `last_restart_error`。saved == 默认仍失败 → 异常向上抛（进程启动失败）。
- `PUT /config` 把已保存装配改回与运行中一致（`pending=false`）时会清掉 `_last_restart_error`（`clear_restart_error`），避免陈旧的拒绝信息一直挂着。

### 7.7 启停

- 停用：`disabled_modules`；生效于重启；`seal()` 后不可切换（`RegistrySealed`）。
- `GET /status` 报告 `config_pending`、`restarting`、`last_restart_failed`、`registry_generation`、`restart_count`、`base_configured`、`base_last_test_ok` 等；`last_restart_error` / `harness_v3_root` / `harness_v3_home` / `mcp_url` 只对管理员返回。Harness v2 为引擎时 `runtime_ok=true`（进程内引擎没有"挂掉"的概念）。
- **审计**：`PUT /config`（`assembly_saved`，只记字段名不记密钥值）、`POST /base/test`（`base_connection_tested`）、`PUT /modules/{id}/placement`（`module_placed`）、`POST /modules/inspect`（`module_inspected`）、`POST /restart`（`runtime_restart_requested` → `runtime_restarted` / `runtime_restart_failed`）都写入 `agent_events`（`session_id="runtime"`）；`GET /audit`（管理员）按时间返回，前端执行日志页的「管理操作记录」即此。

### 7.8 升级与卸载

- 同 `module_id` 新 `version`：在下次重启整体替换（注册表每次重建）。契约：`contract_version` 与 `provides` 的 `/vN` 必须仍受支持，否则 `CONTRACT_INCOMPATIBLE` → 预检失败，运行时不动。幂等历史不受影响（`side_effect_key` 不含 provider id，§5.1）。
- SemVer 约定：主版本变更 MAY 改变 `provides`；次版本 MUST 向后兼容 manifest；补丁版本只修实现。
- 卸载：从 `extra_modules`（【待实现】`remote_modules`）删除并重启。遗留的 `outcome_unknown` 记录仍可在 `GET /ledger/unknown` + `reconcile` 结算。若有其他模块 `requires` 其操作，预检以 `UNSATISFIED_REQUIREMENT` 拒绝。

---

## 8. 错误码表

### 8.1 `ModuleSdkError` 家族（`contracts/errors.py`；`to_dict()` → `{"code","message","details?"}`）

| code | 类 | 何时 | HTTP（管理 / 入站 API） | MCP 工具级（`structuredContent.error.code`） |
|---|---|---|---|---|
| `MODULE_SDK_ERROR` | `ModuleSdkError` | 基类 | 500 | — |
| `CONTRACT_INCOMPATIBLE` | `ContractIncompatible` | `install()` 的 id / 版本 / 契约版本 / 操作名校验（§2.1）；编译器 `_check_contracts` | `/restart` 409；`/modules/inspect` 200 + `errors[]`；`/snapshot` 422 | — |
| `REQUIRED_SLOT_MISSING` | `RequiredSlotMissing` | SOP 必需槽未绑定（`composition/slots.py · resolve_slots`） | `/snapshot` 422 | — |
| `SLOT_NOT_BOUND` | `SlotNotBound` | 槽绑定到不可用资源 | 422 | — |
| `SLOT_KIND_MISMATCH` | `SlotKindMismatch` | 槽操作名未知（`composition/slots.py · sop_slots`） | 422 | — |
| `DEPENDENCY_CYCLE` | `DependencyCycle` | 子 SOP 成环（`CompositionCompiler.compile`） | 422 | — |
| `HOOK_CYCLE` | `HookCycle` | hook `depends_on` 成环（`compile_hooks`） | 422 | — |
| `PEP_BINDING_MISSING` | `PepBindingMissing` | 无 `Guard` / 无守护槽（`Guard.__init__`；`ModuleRegistry.seal`） | `/restart` 409（预检检不到，见 §2.5） | — |
| `PERMISSION_DENIED` | `PermissionDenied` | PEP 拒绝（`Guard.require`） | 403 | 工具级 `isError`（NOT_SENT） |
| `AUTHORIZATION_UNAVAILABLE` | `AuthorizationUnavailable` | 授权服务不可用 / 配置错误 / pending 超时 | 503 | 工具级（NOT_SENT） |
| `INVOCATION_REJECTED` | `InvocationRejected` | 宿主拒绝调用（保留） | 400 | 工具级 |
| `OUTCOME_UNKNOWN` | `OutcomeUnknown` | 同键存在未完成记录（`InvocationLedger.replay_or_block`） | 409 | 工具级 |
| `ACTIVATION_FENCED` | `ActivationFenced` | 围栏：closed / stale / cancelled / deadline / 未激活资源 / 无 activation token | 409 | 工具级（NOT_SENT） |
| `ENGINE_UNAVAILABLE` | `EngineUnavailable` | 引擎未构建 / 未起（`HarnessV3WorkerConfig.validate`；`get_runtime`） | 503 | — |

### 8.2 注册表码（`modules/registry.py`）

`REGISTRY_SEALED`（seal 后 install / set_enabled）、`DUPLICATE_MODULE`、`SLOT_CONFLICT`（未声明槽 / 单提供者槽多于一个）、`UNSATISFIED_REQUIREMENT`。均映射为 `/restart` 409 + `last_restart_error`，或 `/modules/inspect` 的 `errors[]`。

### 8.3 宿主 / 门面返回的 `ModuleResult.fail` 码

| code | 出处 | 是否 NOT_SENT |
|---|---|---|
| `TOOL_NOT_AVAILABLE` | `CapabilityHost.invoke_proxy`（未知代理）；`ToolFacade.invoke` | 是 |
| `UNSUPPORTED_CAPABILITY` | `CapabilityHost._dispatch`（无提供者） | 否 →【待实现】加入 |
| `PROVIDER_INVALID` | `CapabilityHost._dispatch`（provider 无 `invoke`） | 否 →【待实现】加入 |
| `HARNESS_TOOL_ERROR` | `CapabilityHost.invoke`（provider 抛非 SDK 异常） | 否 → 副作用记 `outcome_unknown` |
| `INVALID_ARGUMENTS` | `CapabilityHost._finish_task`；`KnowledgeFacade.search`；`GeneralSkillFacade.consume` | 是 |
| `KNOWLEDGE_NOT_AVAILABLE` / `SKILL_NOT_AVAILABLE` / `SANDBOX_ERROR` / `TOOL_ERROR` / `COMMAND_TIMEOUT` / `COMMAND_EXIT_NONZERO` | `capabilities/facade.py` 各门面 | 否 |
| `CAPABILITY_AUTHORIZATION_REVOKED` / `CAPABILITY_SNAPSHOT_CHANGED` | 门面活行复核 | 是 |
| `CAPABILITY_NOT_ACTIVATED` / `CAPABILITY_NOT_AVAILABLE` / `NOT_FOUND` / `DISABLED` / `NOT_ALLOWED` / `UNSUPPORTED_TOOL_TYPE` | legacy 词表（`NOT_SENT_CODES`） | 是 |
| `PRE_STEP_DENIED` / `HARNESS_V3_ENGINE_ERROR` | `HarnessV3TaskAgent._failed`（TaskExecutionResult.error，非 ModuleResult） | — |

### 8.4 远程提供者新增码【待实现】

`PROVIDER_UNAVAILABLE`（只读可重试）、`PROVIDER_THROTTLED`（只读可重试）、`PROVIDER_UNAUTHORIZED`（NOT_SENT）、`PROVIDER_ERROR`（非 NOT_SENT）、`TIMEOUT`（非副作用 → `failed`；副作用 → `outcome_unknown`）。

### 8.5 Dry-run / 管理 API 码

`INVALID_SPEC`、`BASELINE_FAILED`、`<EXCEPTIONNAME>`（§7.3）；`PUT /config` / `placement` 的 400 为中文 `detail`；`/restart` 409；`/ledger/{id}/reconcile` 404 / 409；`/log` 404。

### 8.6 HTTP 入站（Webhook）码【待实现】

`202` 接受；`200 duplicate`；`401` 验签失败；`403` 租户不符；`409` 同 `delivery_id` 不同体；`422` 信封不合规。

---

## 9. 示例

### 9.1 进程内 Python 模块（最小示例）【已实现路径】

```python
# acme_staffdeck/plugin.py
# pyproject: [project.entry-points."staffdeck_harness.modules"] acme = "acme_staffdeck.plugin:register"
# 或在 /admin → 接入外部模块 填 "acme_staffdeck.plugin:register"
from __future__ import annotations

from typing import Any, Mapping

from staffdeck_harness.composition import projection
from staffdeck_harness.contracts.hooks import HookContext, HookDecision
from staffdeck_harness.contracts.invocation import ModuleInvocation, ModuleResult
from staffdeck_harness.contracts.manifest import HookContribution, ModuleKind, SlotName
from staffdeck_harness.modules.registry import ModuleRegistry, manifest


class AcmeKnowledge:
    """替换内置 knowledge.local（部署需把 knowledge.local 放进 disabled_modules，否则内置者先安装、先被选中）。"""

    module_id = "acme.knowledge"

    def invoke(self, host: Any, inv: ModuleInvocation) -> ModuleResult:
        # 宿主已做：围栏 + ledger。PEP 目前由提供者自行调用（§3.A.4）。
        query = str(inv.arguments.get("query") or "").strip()
        if not query:
            return ModuleResult.fail("INVALID_ARGUMENTS", "query required")          # ∈ NOT_SENT_CODES
        allowed = set(host.slot.allowed().get("knowledge_base", set()))
        requested = {str(x) for x in (inv.arguments.get("knowledge_base_ids") or [])}
        selected = sorted(requested & allowed) if requested else sorted(allowed)
        if not selected:
            return ModuleResult.fail("KNOWLEDGE_NOT_AVAILABLE", "no activated knowledge base")
        ctx = inv.context
        agent_id = None if ctx.agent_id.endswith(":overall") else ctx.agent_id
        refs = {row.id: ref for row, _binding, ref in projection.bound_resources(host.db, ctx.tenant_id, agent_id, "knowledge_base")}
        for kb_id in selected:
            ref = refs.get(kb_id)
            if ref is None:
                return ModuleResult.fail("CAPABILITY_AUTHORIZATION_REVOKED", f"knowledge base {kb_id} is no longer visible")
            host.guard.require(host.security_context, "knowledge.search/v1", ref)   # PermissionDenied 必须向上抛
        budget = host.slot.remaining_seconds()                                       # None = 无截止
        chunks = [{"id": "c1", "text": "…", "knowledge_base_id": selected[0]}]    # 调你的检索服务，尊重 budget
        host.trace("acme_knowledge_searched", {"query": query, "hit_count": len(chunks)})
        return ModuleResult.ok({"query": query, "chunks": chunks}, citations=({"label": 1, "chunk_id": "c1", "knowledge_base_id": selected[0]},))


class AcmePiiHooks:
    module_id = "acme.pii_guard"

    @staticmethod
    def _pre_tool(ctx: HookContext, st: Any) -> HookDecision:
        args = ctx.payload.get("arguments") or {}
        if "id_card" in str(args):
            return HookDecision.deny("PII in tool arguments")
        return HookDecision.passthrough()

    @property
    def handlers(self) -> Mapping[str, Any]:
        return {"acme.pii_guard": self._pre_tool}       # 厂商前缀；不得覆盖默认 handler 名


class AcmeAuditSink:
    name = "acme.audit_sink"                             # EventObserver 需要 name

    def on_event(self, tenant_id: str, session_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
        pass                                             # 入队异步；≤50 ms 返回


def register(registry: ModuleRegistry, ctx: Mapping[str, Any]) -> None:
    dry_run = bool(ctx.get("dry_run"))                   # 仅 /modules/inspect 传入
    registry.install(
        manifest("acme.knowledge", "ACME 知识检索", kind=ModuleKind.CODE,
                 slots=[SlotName.STAFF_CAPABILITY, SlotName.SOP_SLOT_KNOWLEDGE],
                 provides=["knowledge.search/v1"], policy_actions=["knowledge.search/v1"],
                 version="1.2.0", contract_version="v1",
                 summary="用 ACME 向量库检索。",
                 metadata={"category": "capability.knowledge", "vendor": "acme", "homepage": "https://acme.example"}),
        AcmeKnowledge(), slot=SlotName.STAFF_CAPABILITY,
    )
    registry.install(
        manifest("acme.pii_guard", "ACME 敏感信息拦截", kind=ModuleKind.CODE, slots=[SlotName.STAFF_INTERACTION],
                 provides=["hook.contribute/v1"],
                 hooks=[HookContribution(point="pre_tool", handler="acme.pii_guard", order=15, depends_on=("activation.allowlist",))],
                 summary="工具参数含证件号时拒绝调用。", metadata={"category": "sop.supervision", "vendor": "acme"}),
        AcmePiiHooks(), slot=SlotName.STAFF_INTERACTION,
    )
    registry.install(
        manifest("acme.audit_sink", "ACME 审计事件汇入", kind=ModuleKind.CODE, slots=[SlotName.EVENT_OBSERVER],
                 provides=["event.observe/v1"], summary="把运行事件同步到 ACME 审计平台。",
                 metadata={"category": "governance.trace", "vendor": "acme"}),
        AcmeAuditSink(), slot=SlotName.EVENT_OBSERVER,
    )
```

对应 `describe()` 中 `acme.knowledge` 条目：`source="entry_point:acme"`（或规格串原文）、`category="capability.knowledge"`、`switchable=true`、`metadata={"vendor":"acme","homepage":"https://acme.example"}`、`guarded=true`。`POST /modules/inspect` 会对 `knowledge.search/v1` 给出 `OPERATION_SHADOWED` 警告，直到 `knowledge.local` 被停用。

### 9.2 MCP 能力提供者（最小报文）【待实现 客户端侧】

**tools/list**

```http
POST /mcp HTTP/1.1
Host: kb.acme.internal
Content-Type: application/json
Accept: application/json, text/event-stream
MCP-Protocol-Version: 2025-06-18
Authorization: Bearer eyJ…(workload access_token, aud=acme.knowledge)
X-StaffDeck-Tenant: tenant_demo
X-StaffDeck-Module: acme.knowledge
X-StaffDeck-Contract: v1

{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}
```

```json
{"jsonrpc":"2.0","id":1,"result":{"tools":[
  {"name":"knowledge.search.v1",
   "description":"Search ACME vector store",
   "inputSchema":{"type":"object","required":["arguments","context","invocation"],
     "properties":{"arguments":{"type":"object","properties":{"query":{"type":"string"},"knowledge_base_ids":{"type":"array","items":{"type":"string"}},"max_chunks":{"type":"integer"}},"required":["query"]},
                   "context":{"type":"object"},"invocation":{"type":"object"}}},
   "outputSchema":{"type":"object","required":["success"],"properties":{"success":{"type":"boolean"},"data":{},"error":{"type":"object"},"citations":{"type":"array"},"artifacts":{"type":"array"},"extensions":{"type":"object"}}},
   "_meta":{"staffdeck":{"operation":"knowledge.search/v1","side_effecting":false}}},
  {"name":"capability.describe.v1","description":"Return the StaffDeck module manifest","inputSchema":{"type":"object"}}
]}}
```

**tools/call**

```http
POST /mcp HTTP/1.1
… 同上头 …
X-StaffDeck-Invocation-Id: hcall_3_9af1c2d4
X-StaffDeck-Attempt: 1

{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{
  "name":"knowledge.search.v1",
  "arguments":{
    "arguments":{"query":"退款政策","max_chunks":6},
    "context":{"tenant_id":"tenant_demo","agent_id":"agent_01","user_id":"user_07","session_id":"sess_9","turn_id":"msg_42","channel":"web",
               "task_frame_id":"tf_1","step_id":"n2","run_id":"run_5","snapshot_id":"a1b2…","trace_id":"hcall_3_9af1c2d4","deadline_at":"2026-09-03T08:15:00Z","attempt":1},
    "invocation":{"invocation_id":"hcall_3_9af1c2d4","operation":"knowledge.search/v1","binding_id":null,"side_effecting":false,
                  "side_effect_key":null,"request_digest":"4eb5085e5e64e916edcf388f030fbd94a4d1d2b799c0597e72e75a5b3add1224","attempt":1,"deadline_at":"2026-09-03T08:15:00Z"}
  },
  "_meta":{"progressToken":"hcall_3_9af1c2d4"}}}
```

（`request_digest` = `sha256("knowledge|knowledge.search/v1|{\"max_chunks\":6,\"query\":\"\\u9000\\u6b3e\\u653f\\u7b56\"}")`，按 §5.2 实算。）

```json
{"jsonrpc":"2.0","id":2,"result":{
  "content":[{"type":"text","text":"{\"success\":true,\"data\":{\"query\":\"退款政策\",\"chunks\":[{\"id\":\"c1\",\"text\":\"…\",\"knowledge_base_id\":\"kb_1\"}]},\"citations\":[{\"label\":1,\"chunk_id\":\"c1\",\"knowledge_base_id\":\"kb_1\"}]}"}],
  "structuredContent":{"success":true,"data":{"query":"退款政策","chunks":[{"id":"c1","text":"…","knowledge_base_id":"kb_1"}]},"citations":[{"label":1,"chunk_id":"c1","knowledge_base_id":"kb_1"}]},
  "isError":false}}
```

**工具级失败（未触达外部系统）**

```json
{"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"{\"success\":false,\"error\":{\"code\":\"INVALID_ARGUMENTS\",\"message\":\"query required\"}}"}],
  "structuredContent":{"success":false,"error":{"code":"INVALID_ARGUMENTS","message":"query required"}},"isError":true}}
```

（对照：StaffDeck 作为服务端返回给引擎的形状完全相同，只是多一段 `receipt`，`CapabilityMcpServer._call_tool`。）

### 9.3 Webhook 观察者报文【待实现】

```http
POST /staffdeck/events HTTP/1.1
Host: audit.acme.internal
Content-Type: application/json; charset=utf-8
X-StaffDeck-Event: capability_invoked
X-StaffDeck-Delivery: 5f0c1e2a-8c1d-4b62-9a7e-1d2f3c4b5a69
X-StaffDeck-Timestamp: 1756887165
X-StaffDeck-Module: acme.audit_sink
X-StaffDeck-Contract: v1
X-StaffDeck-Signature: sha256=3c9a…e1

{"spec_version":"v1",
 "delivery_id":"5f0c1e2a-8c1d-4b62-9a7e-1d2f3c4b5a69",
 "event_type":"capability_invoked",
 "occurred_at":"2026-09-03T08:12:45.120Z",
 "tenant_id":"tenant_demo",
 "session_id":"sess_9",
 "module_id":"acme.audit_sink",
 "payload":{"operation":"tool.invoke/v1","resource":"tool_ab12","status":"completed","invocation_id":"hcall_4_0c1d2e3f",
            "snapshot_id":"a1b2…","execution_engine":"harness_v3"}}
```

签名输入 = `"1756887165." + raw_body`。应答 `204 No Content`（或 `200 {}`）。重复投递同一 `delivery_id` 时接收方返回相同应答且不重复处理。

---

## 附录 A · 【待实现】清单

（本轮已完成并移出清单：按能力挑选转人工槽实现；预检 / dry-run 使用与真实构建一致的受护槽集合；注册表禁止无 settings 的懒构建，重启改为原子切换；环境快照；地址策略与密钥配对；审计事件；`ledger.invocation` 实现 `on_event`；`general_skill` 进入 Base 治理与本地绑定规则；`BUSINESS_BASE` 未知资源类型拒绝。）

| # | 项 | 涉及文件 | 优先级 |
|---|---|---|---|
| 2 | `CapabilityHost._dispatch` 通用 PEP（对 `policy_actions ∋ operation` 的提供者在调用前 `guard.require`） | `capabilities/host.py` | 高 |
| 4 | `capability.mcp_remote` 适配模块 + `RuntimeOverrides.remote_modules` + 协商 / 超时 / 重试 / 健康检查；`LocalWorkload.mint` 产出可验签的 `access_token` | `modules/remote_mcp.py`(新), `modules/config.py`, `api/admin.py`, `runtime/assembly.py`, `security/oss_local.py` | 高 |
| 5 | `seal()` 校验 `policy_actions ⊆ 映射表`、hook handler 可解析；合并 `metadata.policy_map` | `modules/registry.py`, `contracts/security.py` | 中 |
| 6 | 合并 `compiler.SUPPORTED_CONTRACTS` 到注册表单一来源 | `composition/compiler.py` | 中 |
| 7 | `metadata.priority` 排序 + `describe()["shadowed_by"]` | `modules/registry.py` | 中 |
| 8 | `staff.channel` 第三方适配器 → `ChannelHost.register_adapter` 接线；`metadata.channel` | `runtime/assembly.py` | 中 |
| 9 | Webhook 出站投递器（签名、重试、去重、异步）+ 入站端点 `/hooks/{module_id}/inbound` | `events/webhook.py`(新), `api/admin.py` | 中 |
| 10 | Handoff / 管理 API 直写事件也经 `fanout_event` | `handoff/core.py`, `api/admin.py` | 中 |
| 11 | 远程提供者新增码（`PROVIDER_UNAVAILABLE` / `PROVIDER_THROTTLED` / `TIMEOUT` 语义）随 MCP 远程提供者一起落地 | `capabilities/ledger.py` | 低 |
| 12 | `metadata.log_events` 合并进 `LOG_EVENT_TYPES`；`hook/result` 日志挑 `kind` | `api/admin.py` | 低 |
| 13 | `install()` 对非内置模块拒绝保留首段 | `modules/registry.py` | 低 |
| 14 | `knowledge.import.source` 宿主 | 新 | 低 |
| 15 | `/modules/inspect` 覆盖 `remote_modules` / Webhook 候选；前端保存前调用 | `modules/inspect.py`, 前端 | 低 |

## 附录 B · 与上游 deepseek-harness 0.1.2-alpha.2 对齐的取舍

调研 `.codex-tmp/harness-v3-engine/deepseek-harness-0.1.2-alpha.2` 后，本规范采纳以下约定，其余不采纳（上游约定一行；左侧"约定"列均为上游的导出契约名）：

| 上游约定 | 出处 | 本规范采纳方式 |
|---|---|---|
| SDK 线协议 = JSON-RPC 2.0，每行一条（NDJSON）经 stdio；`initialize` → `session/prompt` → `shutdown`；服务端通知 `session.event` / `session.status` | `packages/sdk/protocol/README.md`（Framing and transport / The SDK methods）；`python/sdk/src/deepseek_harness/client.py · HarnessClient` | 引擎槽内部即用此协议驱动引擎子进程（`bridge/worker.py · HarnessV3Process`；`bridge/task_agent.py · HarnessV3TaskAgent._run_engine_turn`）。**不对外暴露**为模块接入协议。 |
| MCP 客户端仅支持 `stdio` 与 `streamable-http`；HTTP 传输可带自定义 `headers`；`toolCallTimeoutMs`、`failOnStartupError` | `packages/mcp/mcp-client/README.md`；`src/transport.ts` | 进程外能力提供者（§3.B）**统一采用 MCP Streamable HTTP**；StaffDeck 自己挂进引擎的也是 `transport: streamable-http` + `headers.x-staffdeck-activation`（`bridge/worker.py · render_patch`）。 |
| 工具公开名 `mcp__<serverName>__<rawName>`；工具集"整代替换或不替换" | 同上 | StaffDeck 暴露给引擎的工具即 `mcp__staffdeck__<proxy>`（`serverName: staffdeck`，`capabilities/host.py · PROXY_TOOLS`）。远程提供者的工具列表在装配预检时整体校验、整体接受。 |
| stdio 子进程环境变量脱敏 | `src/transport.ts` | 我们的 patch 文件用 `!!js process.env.*` 引用密钥、从不落盘（`render_patch`）。第三方模块 MUST NOT 把密钥写入 manifest 或 `staffdeck-runtime.json`（Base 密钥字段例外，且加密存储）。 |
| Typert 远程调用：取消信号是带外载荷、不进入 `args` | `docs/subsystems/typert.md`（Invocation descriptors） | 采纳：取消不进入 `ModuleInvocation.arguments`（§5.6）。 |
| 上游协议**无版本协商**、无 cancel 方法 | `packages/sdk/protocol/README.md`（Known Limitations） | 不采纳其空缺：本规范强制 `contract_version` + 操作名 `/vN`；取消走进程级中断。 |
| Cordis 配置 patch：`- id: <row>` + `config:` / `disabled: true` / `insert:` | `docs/cordis-primer.md`（Loader Configuration） | 对应我们的装配文件：`disabled_modules` ≈ `disabled: true`，`extra_modules` ≈ `insert:`。 |
| Webhook 家族：签名验证的外部事件 → 规则 → 创建 Session；无重试 / 去重 | `packages/webhook/README.md` | 不采纳其"无重试"：本规范的 Webhook 接入（§3.C）要求签名 + 重试 + 去重。 |
