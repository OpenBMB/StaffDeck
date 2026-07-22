# StaffDeck 模型 API 协议接入落地方案

## 1. 背景与问题

StaffDeck 当前的模型配置包含 `provider` 字段，但该字段仅被保存和展示，模型运行时并不读取它。所有模型请求都会无条件使用 OpenAI SDK 的 Chat Completions 接口：

```python
client.chat.completions.create(...)
```

这导致以下问题：

- 配置 `provider=anthropic` 不会切换为 Anthropic Messages 协议；
- `openai_compatible` 同时混淆了模型品牌、服务供应商和 API 协议；
- OpenAI Chat Completions 与 OpenAI Responses 是两套不同协议，不能共用一个模糊类型；
- 通用技能等路径通过 `SimpleNamespace` 手工复制模型配置，可能遗漏协议或请求参数；
- 前端允许自由填写 Provider，后端也不校验，错误配置只能到运行阶段才暴露。

StaffDeck 真正需要识别的是服务端暴露的 API 协议，而不是模型品牌或供应商。

### 1.1 评审状态与实施决策

本方案初稿经独立架构评审和 QA 评审后结论均为 **NO-GO**。评审认可“按线协议选择 Driver、业务层保持统一调用接口”的核心方向，但指出旧版本回滚、验证状态、默认模型并发一致性、流式资源清理和发布门禁尚不足以支撑生产发布。

本文已将阻断项纳入设计，但文档修订不代表代码已经满足要求。当前决策为：**允许按阶段进入开发，不允许直接发布生产**。只有第 18 节门禁全部实现、由 CI 强制执行并完成真实回滚演练后，评审状态才可转为 **Conditional GO（分阶段灰度）**；任何阻断项未关闭仍为 **NO-GO**。

## 2. 目标

建立明确、可扩展、全平台一致的模型 API 协议层，并安全接入 Anthropic Messages 与 Gemini Generate Content 协议。

本方案目标：

1. 明确区分模型 API 协议；
2. 保持现有 OpenAI Chat Completions 配置零行为回归；
3. 支持 Anthropic Messages 与 Gemini Generate Content 的文本、流式文本、图片输入和 JSON 生成；
4. 确保聊天、Router、Step Agent、知识、技能、通用技能、记忆和后台任务统一使用协议配置；
5. 为 OpenAI Responses 预留架构位置，但本期不开放未实现协议；
6. 覆盖桌面端打包、观测、错误处理、迁移和回滚。

## 3. 非目标

本期不实现：

- 模型供应商注册中心；
- 模型价格、上下文窗口或模型目录管理；
- OpenAI Responses 协议；
- OpenAI 内置 Web Search、File Search、Code Interpreter；
- Anthropic Tool Use；
- Anthropic Extended Thinking；
- OpenAI Responses 的 `previous_response_id` 或服务端 Conversation；
- 协议自动探测或失败后自动切换；
- 不同协议之间自动转换 `extra_body`；
- 新的模型能力注册系统。

StaffDeck 继续使用自身 Agent Loop、技能状态机和 ToolExecutor，不引入第二套供应商原生工具编排。

## 4. API 协议模型

### 4.1 协议定义

本期生产环境只允许三种已实现协议：

```python
class ModelApiProtocol(StrEnum):
    OPENAI_CHAT_COMPLETIONS = "openai_chat_completions"
    ANTHROPIC_MESSAGES = "anthropic_messages"
    GEMINI_GENERATE_CONTENT = "gemini_generate_content"
```

后续完成 OpenAI Responses Driver 后，再增加：

```python
OPENAI_RESPONSES = "openai_responses"
```

未实现的协议不得出现在后端可保存枚举或前端选项中。

### 4.2 协议含义

协议只描述线协议，不描述实际供应商：

| 配置场景 | `api_protocol` |
| --- | --- |
| OpenAI 官方 Chat Completions | `openai_chat_completions` |
| 自部署 Qwen 的 `/chat/completions` | `openai_chat_completions` |
| 第三方 Claude OpenAI 兼容网关 | `openai_chat_completions` |
| Anthropic 官方 Messages API | `anthropic_messages` |
| 企业 Anthropic Messages 代理 | `anthropic_messages` |
| Google Gemini Generate Content API | `gemini_generate_content` |
| 企业 Gemini Generate Content 代理 | `gemini_generate_content` |

StaffDeck 不根据模型名或 Base URL 猜测协议。

## 5. 数据模型与迁移

### 5.1 新权威字段

在 `model_configs` 中增加：

```text
api_protocol VARCHAR NOT NULL DEFAULT 'openai_chat_completions'
trust_status VARCHAR NOT NULL DEFAULT 'unverified'
verified_at DATETIME NULL
verified_fingerprint VARCHAR NULL
verification_attempt_id VARCHAR NULL
verification_started_at DATETIME NULL
verification_attempt_status VARCHAR NOT NULL DEFAULT 'idle'
verification_attempt_error_code VARCHAR NULL
config_revision INTEGER NOT NULL DEFAULT 1
security_revision INTEGER NOT NULL DEFAULT 1
key_revision INTEGER NOT NULL DEFAULT 1
protocol_options_json JSON NOT NULL DEFAULT '{}'
```

`api_protocol` 是运行时唯一权威字段。

`trust_status` 允许 `legacy_trusted | unverified | verified`，表示当前配置是否可用于生产运行。`verification_attempt_status` 允许 `idle | verifying | succeeded | failed`，只描述最近一次测试，不直接撤销已经建立的信任。`legacy_trusted` 仅用于迁移前已经 enabled 的 Chat 配置，不是一次真实能力认证，也绝不能用于 Anthropic 或 Gemini。

### 5.2 旧 `provider` 字段

旧 `provider` 字段进入兼容期：

- 数据库迁移时，所有现有记录写入 `api_protocol=openai_chat_completions`；
- 新运行时不得读取 `provider` 决定协议；
- 旧 API 请求若只提交 `provider=openai_compatible`，API 边界转换为 `api_protocol=openai_chat_completions`；
- 旧 API 请求中的其他 `provider` 值返回 422，不做猜测；
- 请求同时包含 `provider` 和 `api_protocol` 时，两者映射不一致返回 422；
- 新 API 响应同时返回 `api_protocol`；兼容期可继续返回旧 `provider`，但标记为 deprecated；
- 新前端只发送和展示 `api_protocol`；
- 后续单独版本删除 `provider` 字段，不在本期直接删列。

### 5.3 修改协议的安全规则

修改 `api_protocol` 时必须：

1. 将配置设为 `enabled=false`；
2. 如果它是默认模型，清除 `is_default`；
3. 使现有验证状态失效，不静默删除原配置；
4. 重新完成连接测试；
5. 由管理员重新启用并设为默认。

协议参数按协议分区保存，避免切换协议时不可逆丢失：

```json
{
  "openai_chat_completions": {},
  "anthropic_messages": {}
}
```

当前协议的 Driver 只能读取自己的参数分区。平台控制字段（`model`、`messages`、`system`、`max_tokens`、`temperature`、`stream`、`tools`）不得通过扩展参数覆盖。本期 Anthropic 参数分区使用空 allowlist，即不允许自定义扩展参数。

`protocol_options_json` 是上述分区的持久化字段。兼容期内现有 `extra_body_json` 只映射到 `openai_chat_completions` 分区；新代码不得再把它作为所有协议共享的请求体。API Key 实际变化时 `key_revision += 1`，编辑请求中空 Key 表示不修改，因此不能递增。

Chat 的现有 thinking 行为提升为平台受控兼容字段，而不是任意请求体覆盖。新建/新编辑配置的 Chat allowlist 首版只允许顶层 `thinking`，且精确 schema 为：`type` 必填，只能是 `enabled | disabled`；`clear_thinking` 可选且只能是 boolean；禁止其他键、嵌套对象及额外顶层字段，未知键返回 `422 MODEL_PROTOCOL_OPTIONS_INVALID`。Driver 从该类型化配置生成最终 `extra_body.thinking`，新 API 用户不能通过任意 JSON 覆盖最终请求。迁移历史 `extra_body_json` 时：符合该 schema 的 thinking 原样进入 Chat 分区；空对象正常迁移；不符合新 schema或 thinking 之外的既有字段完整保存在 `legacy_unmapped_options`。为保证阶段 A 线行为不变，`legacy_trusted` Chat 仍由隔离的 legacy adapter 按旧逻辑执行原始历史 `extra_body_json`，同时在 UI/日志标记“未类型化兼容参数”；管理员一旦编辑关键配置，必须先删除或转换未知字段，之后转入新 allowlist 和验证流程。Anthropic 永不读取它们。阶段 A 必须对仓库已有 `type/clear_thinking` 用例以及未知键、错误类型和历史未知字段建立 golden migration；兼容数据归零前不得删除 legacy adapter。

以下任一字段变化都必须在同一事务内执行 `config_revision += 1`、`security_revision += 1`、`trust_status=unverified`、清空 `verified_at/verified_fingerprint`，并强制 `enabled=false`、`is_default=false`：

- `api_protocol`；
- Base URL；
- 模型名；
- API Key；
- 当前协议参数分区。

`temperature` 和 `max_output_tokens` 只做协议范围校验，不参与连接身份认证：修改它们只增加 `config_revision`，不增加 `security_revision`、不清除验证、不强制禁用。这样管理员调参不会被迫重新执行能力测试；它们也不得出现在验证指纹中。

更新请求即使同时携带 `enabled=true` 或 `is_default=true`，后端也必须以强制失效规则为准，不得被客户端绕过。

### 5.4 验证状态与配置指纹

生产信任与连接测试是两组相关但分离的状态机：

```text
unverified --测试成功--> verified
verified --配置变化--> unverified
legacy_trusted --关键配置变化--> unverified
legacy_trusted --真实验证成功--> verified

idle/failed/succeeded -> verifying -> succeeded | failed
```

指纹定义：

```text
SHA-256(api_protocol, normalized_base_url, model, key_revision,
        current_protocol_options, security_revision)
```

哈希输入使用版本化 canonical JSON：固定包含 `fingerprint_version=1` 和上述具名字段，UTF-8、Unicode NFC、JSON key 递归排序、紧凑分隔符、禁止 NaN/Infinity，`null` 与空字符串不等价。Base URL 仅规范化 scheme/host 小写、移除默认端口、空 path 变 `/`、移除末尾 `/`（根路径除外），禁止 fragment/userinfo，保留大小写敏感 path 和规范化后的 query；不得把 API Key 或 URL query 写日志。实现必须提供跨进程 golden vectors。

指纹不包含 API Key 明文，只包含单调递增的 `key_revision`。开始连接测试时生成随机 `verification_attempt_id`，短事务写入 `verification_attempt_status=verifying/verification_started_at` 后立即提交，网络调用期间不得持有数据库事务。测试结果只能通过以下条件更新写回：

```sql
WHERE security_revision = :started_security_revision
  AND verification_attempt_id = :attempt_id
  AND verification_attempt_status = 'verifying'
```

同一配置版本的新 attempt 会使旧 attempt 失效，旧结果不能覆盖新结果。`verifying` 超过 15 分钟视为过期，下一次测试可原子接管并记录上一次超时。重测期间及重测失败后，只要关键配置未变化，旧 `trust_status/verified_fingerprint` 继续有效，不中断正在使用的模型；测试成功才原子替换可信指纹。启用或设为默认必须同时满足：

- `trust_status=verified`；
- 当前计算指纹等于 `verified_fingerprint`；
- 当前 `security_revision` 与已验证指纹一致。

兼容栅栏迁移时，迁移前已 enabled 的 Chat 配置保持 enabled/default 并写为 `legacy_trusted`；其他历史配置写为 `unverified`。运行时仅允许 `legacy_trusted` 继续执行 Chat，禁止把它切换到 Anthropic 或用于新配置。首次真实能力验证后转为 `verified`；关键字段一经修改立即转为 `unverified + disabled + non-default`。旧前端只能继续运行或修改既有配置的名称、温度、token 上限；尝试创建新配置、修改关键字段、启用或设默认时返回 `409 MODEL_CONFIG_VERIFICATION_REQUIRED` 并提示升级客户端，不留永久绕过入口。

所有新配置（包括租户第一条）固定创建为 `unverified + disabled + non-default`，忽略客户端同时提交的 enabled/default。首次配置流程必须为“保存 → 能力验证 → 首次激活”，任何步骤失败都不得进入模型选择器。管理端可在测试请求中显式申请首次激活；后端仅当测试开始时配置仍为 unverified、租户不存在任何 enabled/default 模型，并且验证成功提交时该条件仍成立，才在同一次提交中将本配置设为 enabled/default。若已有可用模型则只记录验证结果，禁止覆盖现有默认。普通重测只更新验证状态，不得改变 enabled/default。

### 5.5 默认模型一致性

模型状态始终满足 `enabled=false => is_default=false`；任何禁用操作必须在同一事务清除默认。反向设置默认要求 `enabled=true` 且 trust 合法。设置默认模型必须在单一事务中完成：

1. 用 revision/CAS 验证目标配置未被并发修改；
2. 验证目标配置已验证且 enabled；
3. 清除同租户原默认；
4. 设置新默认；
5. 检查受影响行数并提交。

SQLite 必须创建部分唯一索引，保证每租户至多一个默认模型；“默认必须 enabled”由上述事务不变量和集中服务保证：

```sql
CREATE UNIQUE INDEX IF NOT EXISTS uq_model_configs_tenant_default
ON model_configs(tenant_id) WHERE is_default = 1;
```

建索引前只在 enabled 默认中以 `updated_at DESC, id ASC` 确定保留一条，其余及所有 disabled default 一律取消并记录迁移日志；不存在可用默认时不擅自选择。SQLite 路径同时使用 `BEGIN IMMEDIATE`、条件更新与受影响行数检查。唯一约束冲突返回 409，不能静默覆盖。后台任务启动时获取完整不可变快照，任务执行中不混用新旧 revision。

### 5.6 迁移与跨版本兼容

- 新增列，不删除旧列；
- 迁移必须使用项目现有 `app_data_migrations` 幂等标记，并先检查列、部分唯一索引和关键 schema invariant 是否存在；marker 存在但 schema 不完整时，必须进入只修复 schema/invariant 的路径，不能静默跳过，也不能重置已验证数据；
- SQLite 先 `ALTER TABLE` 增加可空/带默认列，再回填旧数据，最后执行完整性检查；
- 重复启动和迁移中断后重启必须安全；
- 当前产品若仅正式支持 SQLite，需要在发布说明中明确；若支持其他数据库，必须先提供正式迁移脚本，不能依赖 SQLite 启动迁移；
- 发布迁移前备份数据库；
- 记录迁移耗时并评估大库写锁时间。

SQLite 启动迁移按单个 `BEGIN IMMEDIATE` 事务执行，迁移 ID 建议为 `model_api_protocols_v1`：检查 `PRAGMA table_info(model_configs)`，按缺失情况分别执行 `ALTER TABLE model_configs ADD COLUMN ...`；回填 `api_protocol`、revision、验证状态和协议参数分区；清理重复默认并创建部分唯一索引；校验不存在 NULL/未知协议；最后写入 `app_data_migrations` 并提交。若 marker 已存在但列或唯一索引缺失，只执行 schema/invariant 修复并保留现有 trust、fingerprint、revision 和验证结果。任一步失败必须整体回滚，下一次启动可安全重试。测试必须覆盖两个进程并发启动以及每个 DDL、回填、索引和迁移标记写入点失败。SQLite 无法通过后续 `ALTER COLUMN` 收紧约束，因此运行时校验与新建库 schema 必须同时覆盖非空和枚举约束；若决定重建表收紧约束，需要另立迁移并评估锁表时间。

直接回滚到当前旧二进制并不安全：旧运行时会忽略新协议字段，把 enabled Anthropic 配置按 Chat 协议调用。因此采用 expand/contract 发布：

1. **兼容栅栏版本**：先增加 `api_protocol`、验证状态、统一快照和运行时拒绝非 Chat 协议；此版本不能创建 Anthropic 配置；
2. **Anthropic 功能版本**：兼容栅栏版本覆盖到位后，再发布 Driver 和创建入口；
3. **回滚策略**：优先切回已验证的 Chat 默认模型并回滚到兼容栅栏版本，而不是回滚到不识别协议的更老版本；
4. 若必须回滚到更老版本，必须先执行经过演练的数据库降级脚本：取消所有 Anthropic 默认、禁用所有 Anthropic 配置，并验证至少一个 Chat 默认配置可用；
5. 自动更新回退不得跨过兼容栅栏版本。

跨版本矩阵必须覆盖：旧前端/兼容后端、新前端/兼容后端、新前端/Anthropic 后端，以及带 enabled/default Anthropic 数据的二进制回退。

| 前端 / 后端组合 | 允许行为 | 禁止行为 | 预期用途 |
| --- | --- | --- | --- |
| 旧前端 + 兼容栅栏后端 | 读写并运行既有 Chat 配置 | 创建或运行 Anthropic | 兼容期过渡 |
| 新前端 + 兼容栅栏后端 | 展示 Chat 协议并运行 Chat | 展示、保存或运行 Anthropic | 栅栏版本验证 |
| 新前端 + Anthropic 后端 | 按验证状态机管理两种协议 | 未验证配置启用或设默认 | 正式功能版本 |
| 任意前端 + 新协议配置停用 | Chat 正常运行 | Anthropic 配置保留但不参与运行 | 故障前向回滚 |
| 兼容栅栏后端 + 含 Anthropic 数据库 | 忽略并拒绝 Anthropic，Chat 默认仍可用 | 将 Anthropic 当 Chat 调用 | 二进制回退演练 |

## 6. 统一运行时配置

引入不可变内部配置，替代手写 `SimpleNamespace`：

```python
@dataclass(frozen=True)
class ResolvedModelConfig:
    id: str | None
    api_protocol: ModelApiProtocol
    base_url: str | None
    api_key_encrypted: str
    model: str
    temperature: float
    max_output_tokens: int
    protocol_options: Mapping[str, Any]
    config_revision: int
    security_revision: int
```

该 DTO 只保存加密密钥。`frozen=True` 不提供深度不可变保证，因此 `protocol_options` 必须先深拷贝并转换为只读 mapping，内部嵌套集合也转换为不可变结构。API Key 明文只在 Driver 构造边界短暂解密，不进入 dataclass repr、异常、span、后台任务载荷或持久化快照。

统一入口：

```python
def resolve_model_config_for_runtime(
    db: Session, tenant_id: str, config_id: str
) -> ResolvedModelConfig:
    ...

def resolve_model_config_for_verification(
    db: Session, tenant_id: str, config_id: str, attempt_id: str
) -> ResolvedModelConfig:
    ...
```

这两个函数属于同一个集中解析模块，是仅有的模型配置出口，且必须在内部按 tenant/config ID 读取数据库当前行，不能接受可能陈旧的 ORM row。runtime 入口检查 `enabled`、`trust_status/fingerprint`、协议枚举、当前 `config_revision/security_revision` 和当前协议参数分区；只允许 `verified`，以及兼容期只读运行的 Chat `legacy_trusted`。verification 入口允许 unverified/disabled，但仍检查租户权限、协议枚举、attempt ID、security revision、options allowlist 和密钥状态。安全门成功后产生的不可变 DTO 是该次任务的授权快照；已开始任务按该快照完成，新的任务必须重新解析。按模型 ID、默认模型、AgentModelBinding、后台任务等所有路径都必须经过对应入口。`LLMClient` 只接受 `ResolvedModelConfig`，禁止直接接收 ORM row、`SimpleNamespace` 或前端请求对象。

以下路径必须全部使用该类型：

- 普通聊天；
- Router、Step Agent、Reflection、Response Generator；
- 知识路由、知识发现；
- 技能生成、改写、反思；
- 通用技能选择、运行、修复、审查和回复；
- 记忆捕获；
- 反馈分析；
- 定时任务；
- 会话标题；
- 后台线程和异步任务。

禁止继续手写模型配置快照。需要调整 token 上限时使用：

```python
dataclasses.replace(config, max_output_tokens=...)
```

## 7. 协议 Driver 架构

### 7.1 内部标准请求

```python
@dataclass(frozen=True)
class ModelRequest:
    system_prompt: str
    messages: tuple[ModelMessage, ...]
    temperature: float
    max_output_tokens: int
    json_mode: bool = False
    protocol_options: Mapping[str, Any] = field(default_factory=MappingProxyType)
    cancellation: CancellationToken | None = None
```

```python
@dataclass(frozen=True)
class ModelMessage:
    role: Literal["user", "assistant"]
    parts: tuple[TextPart | ImagePart, ...]
```

System Prompt 独立于 messages：

- Chat Driver 转换为 `role=system`；
- Anthropic Driver 转换为顶层 `system`；
- 未来 Responses Driver 转换为 `instructions`。

### 7.2 标准响应

```python
@dataclass(frozen=True)
class ModelResponse:
    text: str
    response_id: str | None
    stop_reason: str | None
    usage: ModelUsage
```

```python
@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
```

流式 Driver 产生统一内部事件；`LLMClient.generate_text_stream()` 继续只向业务层暴露 `Iterator[str]`，其余事件用于观测。

流式生命周期要求：

- Driver 必须用 context manager/finally 关闭 SDK 上游流；
- SSE relay 断开默认不取消独立后台 worker，保留当前断线重连后继续读取结果的语义；
- 用户显式取消必须把 cancellation token 传播到 worker 和 Driver，主动关闭 SDK stream；应用退出做 best-effort 关闭；业务 generator 收到 `GeneratorExit` 时在 `finally` 关闭；
- Driver 必须读取 `ModelRequest.cancellation`，流式路径还须持有可主动关闭的 stream handle，不能只在收到下一个 chunk 后轮询取消标记；非流式 SDK 调用必须使用可关闭 transport/任务封装响应取消；
- 首个用户可见 token 之前允许按现有空流策略重试；
- 一旦输出首个用户可见 token，任何中断都不得自动重试，避免重复文本；
- 取消记录为 `cancelled`，不是 provider failure；
- 即使结束事件缺少 usage，也必须正常完成并以空指标记录；
- SDK 自带重试与 StaffDeck 重试只能启用一层，避免请求倍增。

### 7.3 Driver 接口

```python
class ProtocolDriver(Protocol):
    def complete(self, request: ModelRequest) -> ModelResponse: ...
    def stream(self, request: ModelRequest) -> Iterator[ModelStreamEvent]: ...
```

Driver 使用 typed error 表达能力和上游失败：

```python
class UnsupportedRequestFeature(LLMError):
    feature: Literal["native_json"]

class ModelCallError(LLMError):
    def __init__(self, *, category, safe_user_message, status_code,
                 request_id, retryable, api_protocol):
        super().__init__(safe_user_message)
        ...
```

`ModelCallError` 必须显式初始化 `Exception.args` 并提供稳定 `__str__`；结构化元数据作为只读属性，避免 dataclass 继承 Exception 后日志文本为空或不稳定。

只有 Driver 明确识别出原生 JSON 参数不受支持时，才能抛出 `UnsupportedRequestFeature("native_json")` 让平台去掉该参数重试。401、403、404、429、超时和普通 400 不得触发能力降级或协议切换。

Driver 仅负责协议转换，不负责：

- JSON 解析与修复；
- Agent 路由；
- ToolExecutor；
- 技能状态推进；
- 会话持久化。

## 8. OpenAI Chat Completions Driver

第一阶段从现有 `LLMClient` 原样抽取，保证行为不变：

- 使用 `OpenAI(...).chat.completions.create()`；
- System Prompt 转为第一条 system message；
- 图片保留 `image_url` 格式；
- JSON 模式尝试 `response_format={"type": "json_object"}`；
- 不支持 JSON Mode 时通知上层降级为普通文本；
- 流式读取 `choices[].delta.content`；
- 保留现有空输出重试、错误脱敏、thinking 扩展和观测指标。

现有 OpenAI Compatible 配置和测试必须零行为变化。

## 9. Anthropic Messages Driver

### 9.1 请求

- 使用 Anthropic Messages 协议；
- System Prompt 使用顶层 `system`；
- messages 只包含 `user` 与 `assistant`；
- 必要时合并连续同角色消息；
- 使用 `max_tokens`、`temperature`；
- 只读取 Anthropic 协议参数分区；本期 allowlist 为空；
- 第一版不发送 thinking 或 tools 参数。

消息规范化发生在内部标准消息层、token 裁剪之后、协议映射之前。合并连续角色时按 content block 顺序拼接，不能压成字符串；不得丢失图片或同轮阶段消息。空 assistant block 被删除；图片只能进入 user block。若裁剪后首条为 assistant，则继续向前保留与它对应的最近 user 消息；仍无法恢复合法上下文时删除孤立 assistant 并记录低基数原因指标，绝不伪造用户内容。无法满足 Anthropic 角色约束时在发送前返回稳定错误。

### 9.2 文本响应

- 拼接响应中所有 `type=text` 的 content block；
- thinking block 不作为用户可见文本；
- 无文本时进入统一空输出重试；
- 映射 response ID、stop reason 和 usage。

### 9.3 流式响应

- 只将文本 delta 转为 `text_delta`；
- 记录首文本时间、事件数量、结束原因、usage 和 response ID；
- 只有 thinking、没有文本时视为空流；
- 供应商错误转换为统一 `LLMError`。

### 9.4 图片

现有 StaffDeck 图片为 OpenAI 风格 Data URL：

```json
{"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}
```

Anthropic Driver 转为 base64 image source。

本期支持 MIME：

- `image/jpeg`；
- `image/png`；
- `image/gif`；
- `image/webp`。

不自动下载远程 URL，避免 SSRF。远程 URL 或非法 Data URL 在发送前返回可读错误。

第一版采用确定的本地安全上限：按 Base64 严格解码后的字节计量，单图 `<= 5 MiB`、单请求最多 6 张、所有图片合计 `<= 18 MiB`、标准请求序列化后总正文 `<= 25 MiB`，边界值允许。校验顺序固定为格式/MIME → 单图 → 数量 → 解码图片总量 → 序列化正文，因此同时超限时返回最先命中的稳定错误码。超限分别返回 `MODEL_IMAGE_TOO_LARGE`、`MODEL_TOO_MANY_IMAGES` 或 `MODEL_REQUEST_TOO_LARGE`。同时校验 Data URL MIME、Base64 完整性、声明 MIME 与 magic bytes 一致及允许类型，不把 Base64 内容写入日志。这些是 StaffDeck 首版保护值，不代表上游模型极限；后续只能通过版本化配置调整并补契约测试。

### 9.5 JSON

Anthropic Driver 不伪造 OpenAI `response_format`。

统一 `generate_json()`：

1. 对 Anthropic 请求增加“仅返回 JSON object”的平台提示；
2. 获取普通文本；
3. 复用现有 `_loads_llm_json()`；
4. 解析失败继续使用现有 JSON repair payload 重试；
5. 不在 Driver 内复制 JSON 修复逻辑。

`generate_json()` 增加可选的 `validator: Callable[[dict], T]`。JSON 请求共享一次 wall-clock deadline 和总尝试预算：首版默认 deadline 为 120 秒、最多 3 次上游请求（首次生成 + 最多 2 次 repair）；每次请求超时取 `min(配置的单次超时, 剩余 deadline)`。解析失败及 validator 抛出的受控业务 schema 错误消耗 repair 次数，repair payload 包含脱敏后的校验摘要；429、网络错误和 timeout 消耗上游请求次数但不伪装成 repair，只有错误标记 retryable 且 deadline 足够时才可重试。指数退避上限 2 秒且计入 120 秒总 deadline。用户取消通过 `ModelRequest.cancellation` 主动关闭非流式 SDK 请求并立即终止，不再 repair。现有 Chat 如需保留不同预算，必须通过协议合同测试证明线行为兼容，并在兼容栅栏阶段单独记录例外，不能沿用 600 秒乘多次 repair 的无界总耗时。未传 validator 时只保证可解析 JSON；Router、Step Agent、Skill 等结构化业务调用必须迁移为传 validator，“可解析”不等于业务合法。

## 10. Gemini Generate Content Driver

Gemini 首版使用 HTTPX 直接实现，不引入供应商 SDK：

- 普通请求：`POST {base_url}/v1beta/models/{model}:generateContent`；
- 流式请求：`POST {base_url}/v1beta/models/{model}:streamGenerateContent?alt=sse`；
- LLM Center 使用 `Authorization: Bearer`，同时保留标准 Gemini `x-goog-api-key` header；
- `system` 消息映射为顶层 `systemInstruction`；
- user/assistant 映射为 Gemini `user`/`model` contents，并合并连续角色；
- `temperature`、`max_tokens` 映射到 `generationConfig.temperature`、`maxOutputTokens`；
- JSON 生成使用 `generationConfig.responseMimeType=application/json`；
- Gemini SSE 的 `data:` JSON chunk 转换为统一 Chat 风格 chunk；
- `thought=true` parts 不作为用户可见文本；
- 图片 Data URL 转为 `inlineData`，沿用统一图片数量、大小和正文上限；
- 不读取 Chat `extra_body` 或 thinking 参数。

LLM Center 的 Anthropic/Gemini Base URL 可以填写为 `https://llm-center.modelbest.cn/llm` 或内网地址 `https://llm-center.modelbest.co/llm`。OpenAI SDK 的 Base URL 必须按中转站定义原样保存和使用，平台不猜测也不自动补 `/v1`；LLM Center 的 OpenAI 配置应填写 `https://llm-center.modelbest.cn/llm/v1`，而将 `/llm` 自身定义为版本根的自定义中转站仍可直接填写 `/llm`。

## 11. `LLMClient` 对外兼容

业务层接口保持不变：

```python
generate_text(..., cancellation=None) -> str
generate_text_stream(..., cancellation=None) -> Iterator[str]
generate_json(..., validator=None, cancellation=None) -> T | dict[str, Any]
```

内部变化：

1. 将现有消息投影为标准 `ModelRequest`；
2. 根据 `ResolvedModelConfig.api_protocol` 选择 Driver；
3. 统一处理空输出重试、JSON 修复、观测和错误；
4. 不允许协议失败后自动切换 Driver。

## 12. 前端落地

### 12.1 模型管理页

将自由文本 Provider 改为“API 协议”下拉框：

- OpenAI Chat Completions 兼容；
- Anthropic Messages 兼容。
- Gemini Generate Content 兼容。

协议切换动态更新：

| 字段 | Chat Completions | Anthropic Messages | Gemini Generate Content |
| --- | --- | --- | --- |
| Base URL 示例 | `https://host/v1` | `https://api.anthropic.com` | `https://llm-center.modelbest.cn/llm` |
| API Key 示例 | `sk-...` | `sk-ant-...` | `sk-...` |
| Model 示例 | 服务端模型 ID | Claude 或代理模型 ID | Gemini 或代理模型 ID |
| 协议扩展参数 | 仅允许 Chat allowlist 参数 | 本期不开放 | 本期不开放 |

如果用户已填写 Base URL，切换协议时不自动覆盖；保存时按第 5.3 节禁用配置并使验证失效。各协议参数分区保留，但不会跨协议传递或执行。

### 11.2 首次配置弹窗

与模型管理页共享同一协议选项、默认值和校验逻辑，禁止两套表单各自维护协议规则。

### 11.3 模型显示

列表、模型下拉和聊天模型提示显示协议名称，不再显示误导性的 Provider 文本。

### 11.4 国际化

同步更新中英文文案，包括：

- API 协议；
- 协议名称；
- 配置错误；
- Anthropic 连接提示；
- 不支持远程图片；
- 协议变更后需重新测试。

## 13. 配置 API

创建/更新接口要求：

- `api_protocol` 必须是已实现枚举；
- 所有新配置一律保存为 unverified、disabled、non-default，不能自动把租户第一条配置设为默认；
- `model`、API Key 和 token 上限合法；
- Temperature 按协议校验：Chat Completions 保持现有 `0..2` 兼容范围，Anthropic Messages 使用 `0..1`；迁移不改写已有 Chat 值；
- 新建时若 API Key 为空则拒绝；编辑时空 API Key 表示不修改；
- 协议变化执行禁用、取消默认并使验证失效；协议参数按分区保留且不得跨协议读取；
- 未知旧 `provider` 返回 422。

连接测试：

- 使用保存配置的实际协议 Driver；
- 将“连接成功”和“能力认证”分开：基础连接测试验证非流式文本；启用为全平台模型前还需验证流式文本和最小业务 JSON；图片标记为未验证，或由管理员显式执行图片能力测试；
- 不将失败配置自动设为 enabled/default；
- 返回 `api_protocol`、可读错误和脱敏 request ID；
- 不返回 API Key、请求头或完整上游响应体。

能力验证总 deadline 为 90 秒并关闭 SDK 自动重试。文本探针 `max_tokens=32`、connect/read/total timeout 为 `5/20/25` 秒；流式探针 `max_tokens=32`、`5/20/25` 秒；JSON 探针 `max_tokens=128`、`5/30/35` 秒。每项最多一次上游请求，按文本 → 流式 → JSON 顺序执行；某项失败仍继续其余项，只要剩余 deadline 足够，否则剩余项返回 `MODEL_VERIFICATION_DEADLINE_EXCEEDED`。只有全部必需能力通过，才原子写入新的 verified 指纹；失败只更新 attempt 状态，不撤销关键配置未变化时的旧信任。

能力测试 HTTP 200 响应固定包含：`attempt_id`、`trust_status`、`attempt_status` 和 `capabilities[]`；能力 ID 固定为 `text`、`stream`、`json`、可选 `image`，每项包含 `id/success/error_code/latency_ms/request_id`，不得返回上游原文。

## 14. 错误处理与安全

统一错误分类：

| 内部分类 | StaffDeck HTTP | 稳定业务码 | 用户提示 |
| --- | --- | --- | --- |
| authentication | 422 | `MODEL_AUTHENTICATION_FAILED` | API Key 无效 |
| permission | 422 | `MODEL_PERMISSION_DENIED` | 当前凭据无模型权限 |
| not_found | 422 | `MODEL_ENDPOINT_NOT_FOUND` | Base URL、端点或模型不存在 |
| rate_limit | 429 | `MODEL_RATE_LIMITED` | 被限流或额度不足 |
| timeout | 504 | `MODEL_TIMEOUT` | 模型服务连接超时 |
| invalid_request | 422 | `MODEL_INVALID_REQUEST` | 请求参数与所选协议不兼容 |
| empty_output | 502 | `MODEL_EMPTY_OUTPUT` | 模型未返回可用文本 |
| upstream | 502 | `MODEL_UPSTREAM_ERROR` | 模型服务暂时不可用 |
| unsupported_protocol | 422 | `MODEL_PROTOCOL_UNSUPPORTED` | API 协议未实现 |

API 返回稳定错误结构：

```json
{
  "code": "MODEL_RATE_LIMITED",
  "category": "rate_limit",
  "message": "模型服务被限流或额度不足",
  "retryable": true,
  "api_protocol": "anthropic_messages",
  "request_id": "req_..."
}
```

上游原始错误不直接返回 API。原始异常只进入受控 debug 日志，并经过统一 scrubber。

连接/能力测试是一个被正常处理的检测操作：只要测试任务本身成功执行，HTTP 返回 200，并在 `capabilities[]` 中逐项返回 `success/error`；配置不存在、权限不足、非法状态或请求 schema 错误仍使用标准 4xx。普通模型业务调用按上表返回 HTTP 状态。前端只能依赖 StaffDeck 状态和稳定业务码，不能依赖上游文案。

安全要求：

- 脱敏 `sk-...`、`sk-ant-...`、Authorization、`x-api-key`；
- 日志不记录完整请求头或供应商响应体；
- Base URL 仅允许 `http/https` 且禁止 userinfo；日志仅记录 host/port。桌面本地部署允许 localhost、内网和企业代理；服务端部署必须启用 SSRF 网络策略；
- Anthropic 图片只接受受支持 Data URL；
- 协议配置在后台任务快照中不可丢失或被覆盖。

## 15. 观测

每次调用统一记录：

```text
api_protocol
model
endpoint_host
request_kind
stream
input_tokens
output_tokens
total_tokens
cache_read_tokens
cache_write_tokens
stop_reason
provider_response_id
latency_ms
ttft_ms
```

`request_kind`：

```text
chat.completions
anthropic.messages
```

保留现有 `chat.completions` 和 `cached_input_tokens` 指标名称，避免破坏既有面板；新增 Anthropic cache 字段时同时写统一 `cached_input_tokens`，原始 cache read/write 仅作为可选扩展。不得将模型品牌推断为协议或供应商。

灰度使用 5/15/60 分钟滚动窗口，每 5 分钟评估一次，并与同业务链路过去 7 天 Chat 基线比较。首版放量规则：样本不足 100 次只告警不自动动作；15 分钟样本达到 100 后，成功率低于 98% 或比 Chat 下降超过 2 个百分点、429 率超过 5%、空输出率超过 1%、JSON 最终失败率超过 2%、latency P95 或 TTFT P95 超过各自 Chat 基线 1.5 倍，任一条件连续两次评估触发暂停新增灰度并禁止 Anthropic 成为新默认，同时报警；不自动中断已经开始的请求或流。Chat 对应指标样本不足 100 或基线为零时，该相对指标只告警，仍应用成功率/错误率绝对阈值。60 分钟恢复窗口全部达标后仅允许管理员人工恢复。阈值应配置化并审计变更，但发布时必须有上述保守默认值。观测标签禁止使用高基数请求正文、完整 URL 或用户输入。

## 16. 桌面端与依赖

若采用 Anthropic 官方 Python SDK：

- 增加 `anthropic` 依赖；
- 更新 `backend/pyproject.toml`；
- 更新 PyInstaller spec hidden imports；
- 更新四平台 runtime 构建；
- 验证最终安装产物中可导入 SDK；
- 验证 macOS Intel/Apple Silicon、Windows 和 Linux 目标（按现有发布矩阵）；
- 验证升级安装不影响数据库迁移。

依赖必须锁定经过验证的版本范围，并检查与现有 `httpx`、`pydantic` 的依赖冲突、CA bundle、系统代理和自定义企业证书。最终产物需要覆盖当前发布矩阵的 macOS ARM、macOS Intel、Windows 和 Linux，而不只验证源码环境。SDK 默认重试关闭，由 StaffDeck 统一控制。

如果使用现有 `httpx` 自行实现，可减少打包依赖，但需要自行维护流式事件、错误类型和协议版本。为降低协议维护风险，本方案推荐官方 SDK，并将打包验证列为发布门禁。

提供仅在测试构建或显式测试环境启用的包内诊断入口：

```text
staffdeck --protocol-smoke anthropic_messages --base-url http://127.0.0.1:<port>
```

四平台 CI 必须从最终 `.app`、Windows 安装产物和 Linux 产物内部执行该入口，经实际锁定版本的官方 SDK 请求本地契约服务器。入口仅存在于测试构建，只接受 loopback Base URL 和编译时固定测试 token，拒绝用户传入 Key、非 loopback 地址及正式构建调用，也不得打印测试 token。

## 17. 实施阶段

### 阶段 A：协议字段与配置快照

目标：模型请求线协议不变；API/UI 存在可观察的兼容升级。

- 新增 `api_protocol` 与迁移；
- 新增后端枚举和 API 兼容；
- 新增 `ResolvedModelConfig`；
- 替换全部手写模型快照；
- 前端 Provider 改为 API 协议；
- 当前唯一可用协议仍为 Chat Completions；
- 全量回归现有模型行为。

该阶段作为可独立发布的兼容栅栏版本：只能保存/执行 Chat Completions，运行时明确拒绝任何其他协议。完成旧前端/新后端与新前端/新后端兼容测试后先发布，不与 Anthropic Driver 同批上线。

建议提交：

```text
refactor(models): introduce explicit model API protocols
```

### 阶段 B：抽取 Chat Completions Driver

目标：纯重构，行为不变。

- 抽取现有请求、流式、usage、错误逻辑；
- `LLMClient` 改为统一运行时；
- 现有 LLM 测试原样通过；
- 增加 Driver 合同测试。

建议提交：

```text
refactor(llm): extract Chat Completions protocol driver
```

### 阶段 C：开放多协议能力

- 加入官方 SDK 和打包依赖；
- 实现文本、流式、图片和 usage；
- 接入统一 JSON 修复并扩展错误脱敏；
- OpenAI Chat、Anthropic Messages、Gemini Generate Content 作为已实现协议直接开放创建、测试、启用和设默认；
- 所有协议统一受验证指纹、enabled/default 状态、租户权限和集中 resolver 约束；
- 协议异常时由管理员停用对应模型并切换到已验证的其他默认模型，不使用环境变量隐藏产品能力。

建议提交：

```text
feat(llm): support Anthropic Messages protocol
```

协议实现仍应按 Driver、测试入口、配置验证和运行启用拆分提交，并保持数据库前后兼容；产品发布后不再通过本地环境变量隐藏已支持协议。

### 阶段 D：文档与发布

- 更新 README、README.zh、教程和 Onboarding；
- 更新配置示例；
- 构建桌面安装包；
- 执行升级、回滚和冒烟测试；
- 分阶段启用 Anthropic 和 Gemini 配置。

建议提交：

```text
docs(models): document supported API protocols
```

### 后续阶段：OpenAI Responses

完成独立 Driver、流式事件、`text.format` 和无状态策略后，再将 `openai_responses` 加入生产枚举和前端选项。本期不创建不可执行的 Responses 配置。

## 18. 测试策略

### 17.1 单元测试

- 旧 `provider=openai_compatible` 迁移正确；
- 未知协议创建/更新被拒绝；
- 协议变化自动禁用、取消默认并使验证失效，各协议参数分区互不泄漏；
- `ResolvedModelConfig` 字段完整且不可变；
- 所有 token 调整使用 `replace()` 并保留协议；
- Chat 消息、图片、JSON 和流式映射不变；
- Anthropic system、消息、图片和 usage 映射正确；
- 连续同角色消息正确合并；
- Anthropic JSON 不发送 OpenAI `response_format`；
- Anthropic JSON repair 保留原始任务上下文；
- API Key 和 request ID 错误信息正确脱敏。
- 验证指纹只认证当前 security revision，测试期间关键配置并发修改会使结果作废；
- 同一安全版本的并发验证只有最新 attempt 可写回，过期 verifying 可安全接管；
- 历史 enabled Chat 迁移为 `legacy_trusted` 后不中断，关键字段首次修改即退出兼容状态；
- 调整温度/token 只增加普通 revision，不破坏已验证安全指纹；
- 协议更新即使携带 `enabled=true/is_default=true` 仍强制失效；
- 设置默认使用 CAS，冲突返回 409，单租户至多一个 enabled default；
- typed error 只有 `UnsupportedRequestFeature(native_json)` 可以触发 JSON Mode 降级。

### 17.2 集成测试

对三个协议分别覆盖：

- 模型连接测试；
- 普通聊天；
- 流式聊天；
- Router JSON；
- Step Agent JSON；
- Response Generator；
- 知识搜索和知识发现；
- 技能生成和改写；
- 通用技能选择、生成、修复和回复；
- 记忆捕获；
- 反馈分析；
- 定时任务；
- 会话标题。

### 17.3 兼容与失败测试

- 现有 Chat 配置升级后可直接使用；
- 带 Anthropic 配置的数据只能回退到兼容栅栏版本，且该版本必须拒绝执行 Anthropic；
- 401、403、404、429、超时、空文本、空流；
- 非法 Base64、未知 MIME、远程图片 URL；
- 协议与 Base URL 不匹配；
- 并发修改默认模型；
- 后台任务在配置修改或删除后的行为；
- 切换协议后另一协议参数不泄漏；
- 旧前端/兼容后端、新前端/兼容后端、新前端/Anthropic 后端组合；
- 带 enabled/default Anthropic 数据直接回滚到兼容栅栏版本；
- SQLite 全新库、上一正式版库、重复启动幂等、迁移中断恢复、未知旧 provider 和大库锁时间；
- 首 token 前空流可重试、首 token 后失败不重试、显式取消关闭上游、SSE relay 断开不取消 worker 和 usage-only 结束；
- JSON 可解析但不符合 Router/Step/Skill 业务 schema；
- 并发测试与编辑、并发设置默认、后台任务使用不可变 revision 快照。
- 图片在单图/数量/总字节/总正文边界及超限 1 byte 时返回稳定错误码；
- JSON 在 120 秒 deadline、三次请求预算、429/timeout 和用户取消下不超预算；
- 停用模型后，集中 resolver 必须阻止绑定、后台任务和所有新调用，且不强杀已有流；
- 所有模型来源均无法绕过集中解析模块的 runtime/verification 安全门。

### 17.4 桌面测试

- 全新安装；
- 从上一版本升级；
- SQLite 迁移；
- Chat、Anthropic 与 Gemini 真实或契约环境连通；
- 最终安装产物内 SDK 导入；
- 签名、公证、安装和启动；
- 无网络和代理网络环境错误提示；
- macOS ARM、macOS Intel、Windows、Linux 最终产物导入 SDK 并走本地协议契约请求。

### 17.5 协议契约与真实服务

建立本地 HTTP 契约服务器，不只 mock SDK 方法。契约测试断言：

- 最终 URL、headers 和协议版本头；
- system、角色合并、文本/图片 content block；
- token、temperature、stream 参数；
- 非流式响应、完整流事件、缺失 usage 和乱序/错误事件；
- 401、403、404、429、超时和非法请求映射；
- 请求与日志不泄漏 Key、Base64 或用户正文。

本地契约测试必须由生产 Driver 通过实际锁定版本的官方 SDK 发起，禁止用平行手写 HTTP fixture 代替 SDK 路径。发布候选最终安装包还必须使用受限 Anthropic 测试 Key 执行真实服务 smoke：非流式文本、流式文本和一个 Router/Step Agent JSON 链路。记录模型 ID、测试日期和结果；真实服务测试失败阻断发布。

### 17.6 前端自动化

前端采用 Vitest + Testing Library 做共享表单逻辑单测，采用 Playwright Chromium 做浏览器 E2E；release CI 启动隔离的临时 SQLite 后端和本地协议契约服务，不连接真实用户数据。覆盖两个配置入口选项一致、未实现 Responses 不出现、协议切换失效验证、旧 URL 不被静默覆盖、未验证配置无法启用/设默认，以及稳定错误码展示。

## 19. 发布门禁

发布前必须满足：

1. 现有 Chat Completions 全量测试通过；
2. Anthropic Driver 单元和契约测试通过；
3. 关键业务链路双协议集成测试通过；
4. 数据库升级和回滚演练通过；
5. macOS ARM、macOS Intel、Windows、Linux 最终产物打包冒烟通过；
6. API Key 与日志脱敏审查通过；
7. 未实现 Responses 不出现在 UI/API 枚举；
8. 协议切换不会让未测试配置成为默认；
9. 文档和中英文文案完成；
10. 配置验证状态机和默认模型并发约束演练通过；
11. 本地协议契约服务器与真实 Anthropic 发布候选 smoke 通过；
12. 四平台最终产物冒烟通过；
13. 从干净 worktree/明确基线构建，`git status --porcelain` 为空。

这些门禁必须进入 `.github/workflows/release.yml`：release/build job 显式依赖后端全量测试、前端 build/i18n/组件测试、协议契约、迁移、四平台 package smoke 和安全扫描。只在文档中声明而未由 CI 强制执行，不视为门禁完成。

## 20. 回滚方案

- 发布前备份数据库；
- 首选将默认模型切回已验证的 Chat 配置，并停用异常协议模型；
- 如需回退二进制，只允许回退到已验证能拒绝非 Chat 配置的兼容栅栏版本；
- 新列和协议分区保留，兼容栅栏版本必须忽略并拒绝执行 Anthropic；
- 禁止直接回退到不识别 `api_protocol` 的旧二进制；确需跨越栅栏时，必须先运行并验证数据库降级脚本；
- 不删除 Anthropic 配置和密钥，避免不可逆数据损失；
- 若 Driver 存在问题，由管理员切换默认模型并停用异常协议配置；桌面端主要依赖兼容栅栏版本、应用更新和经过演练的本地数据库降级。

## 21. 验收标准

只有同时满足以下条件，才视为 StaffDeck 已支持 Anthropic Messages 与 Gemini Generate Content：

- 管理员可明确选择 Anthropic Messages 协议；
- 配置可保存、测试、启用并设为默认；
- 普通与流式聊天正常；
- Router、Step Agent、Reflection 和 Response Generator 正常；
- 知识发现和技能生成正常；
- 通用技能、记忆和后台任务不丢失协议；
- 图片输入正确转换或明确拒绝；
- JSON 任务能够解析和修复；
- 错误、usage 和观测字段正确；
- 桌面打包版本可运行；
- 现有 Chat Completions 配置没有行为回归；
- 未实现的 OpenAI Responses 不可被配置。
