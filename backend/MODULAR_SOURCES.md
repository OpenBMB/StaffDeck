# 运行时来源与能力模块接口

本文描述公共框架的来源、能力和运行服务边界。默认本地装配不依赖私有插件；外部实现通过同一组契约接入。

## 公共代码范围

公共框架包含 `backend`、公共管理/对话界面 `frontend-enterprise`、`scripts` 和 `packaging`。
目录名 `frontend-enterprise` 是既有公共控制台名称，不代表私有企业插件实现。
企业插件包、企业服务覆盖补丁、现场数据、凭证、部署配置和本机构建产物不随公共分支发布。

不安装私有插件时，公共源码可独立导入、构建和测试。仅直接调用可选插件的集成测试会跳过；公共权限、来源、生命周期和引用契约测试仍执行。
真实 AgentLoop 测试需要另外准备 Harness 引擎并设置 `HARNESS_V3_ROOT`，测试使用本机模拟模型/工具，不需要企业账号。

除来源/能力接口外，共用的运行服务容器负责会话、模型映射和后台事务；渠道、团队、异步工具不得另建执行器。
`channel_scope_service` 是可选的内部范围验证入口，只应监听私有接口，并复用当前装配与存储。

## 层次与职责

- `source.staff`：部署级唯一员工来源；`reference` 返回授权目标，授权通过后 `resolve` 返回 `StaffComposition`；`model` 复用模型解析器。
- `source.sop`：部署级唯一 SOP 定义来源；返回已绑定的 `SopView`，不执行模型或推进流程。
- `source.identity`：部署级唯一可信身份来源；不得信任客户端自报的角色、委托信息。
- `resource.catalog`：可安装多个目录；执行模块的 manifest `metadata.catalog_module_id` 指定所属目录，默认 `resource.catalog.local`。
- `staff.capability`：现有执行 Provider，继续实现 `invoke(ProviderContext, ModuleInvocation)`。
- `runtime.sop`：部署级唯一 SOP 生命周期模块，现在使用 `sop.lifecycle/v2`。

来源工厂的 `build(services)` 是可信部署装配点；默认工厂在此绑定现有数据库。公开来源方法只接收 `SourceContext`、引用和纯数据，不接收 ORM。第三方能力调用只接收既有 ProviderContext，不获得数据库、宿主或模型客户端。

## 调用顺序

可信身份 → 员工使用权限 → 员工/SOP 来源 → 共用编译器 → 固定执行快照。

模型发现、说明、调用共用同一个资源目录。Host 先做绑定范围限制，再确认目录身份、状态、版本并调用 PEP；参数合法才进入原有调用记录和 Provider。即使 Provider 不主动检查权限，也不能跳过 Host 的检查。

资源 ID、安装绑定 ID、执行模块 ID、模块版本和资源摘要分别保存，不能把展示名称当作资源 ID。替代资源不必在本地 Tool/GeneralSkill/KnowledgeBase 表中建立影子行。

## SOP 与状态

生命周期插件只能修改隔离的 `SopState`，并提出事件/人工介入意图。Host 检查身份和权限后，在原有事务/租约管理下应用状态；插件不获得 Session，也不能提交事务或直接写执行记录。

SOP 内容与能力绑定按 TaskFrame 实例固定，记录在现有 `ChatSession.context_state_json.sop_module_pins`。新实例使用新版本；旧实例恢复固定版本。绑定撤销或资源/模块版本不可用时明确失败，不清空执行历史或重新调用旧 Runner。

`sop.lifecycle/v1` 不再是可注册版本；升级自定义插件时使用新的 SopDependencies（events/create_handoff），移除对 ports.db 的依赖。

## 安全和复用

安全上下文有 actor_user_id、agent_id、run_id、run_attempt、sop_authorization_ref 等相关标识，不携带长期授权。真实凭证不能放入来源 DTO、模块 provider_config、快照或模型上下文。

本轮没有实现企业委托适配。现有 BUSINESS_BASE 对不能表达的代理执行明确拒绝，不能把 workload 主体假装成 user 发送普通授权检查。

调度、会话、Memory、租约、调用记录及公共结果协议继续共用原实现。来源读取和 SOP/资源逻辑是新增插入点，不是第二套基础设施。

## 验收

### 管理与后台任务的来源边界

独立运行 `channel_scope_service` 的部署必须将其纳入同一次源码发布及进程重启，不能只更新主 API。systemd 可由主 Runtime `Wants=` 校验服务，校验服务 `PartOf=` 主 Runtime；这样 stop/start 与 restart 都会同步。具体单元名由部署指定。发布后需检查校验服务及依赖方的就绪契约，不能只以主 API 的 `/health` 成功作为验收。装配加载失败返回 `CHANNEL_SCOPE_ASSEMBLY_UNAVAILABLE` 和关联错误 ID，日志仅记录异常类型/代码与栈位置，不输出任意异常文本或配置值。

- 外部控制身份适配器实现 `MemberDirectoryPort`：声明 `member_identity_source`，批量返回 `MemberRecord`（含明确的启停状态）。渠道绑定、协作者、人工处理人及 SOP 人工节点统一使用该目录，不要求成员先登录以生成本地投影。
- 本地身份投影仅用于关联渠道和运行记录。不能从投影推断成员仍有效；绑定码兑换和后台执行重新校验。目录不可用不回退本地账号，也不冒用管理员身份。
- 定时任务、记忆、会话和反馈的员工访问通过 `staff_directory` 与当前 PEP；后台任务沿用接纳时的运行服务和装配租约。
- 员工来源可提供 `profiles(context, staff_ids)` 批量展示资料。批量名称不是授权缓存，执行和写入仍实时检查权限。
- Runtime 在 `composition_snapshot_compiled`、`capability_manifest_resolved` 事件中提供白名单式 `trace_names`；渠道不再扫描自己的资源表拼装名称，不输出 Provider 私有配置。
- 成员错误分别为 `MEMBER_NOT_FOUND`、`MEMBER_DISABLED`、`MEMBER_IDENTITY_CONFLICT`、`MEMBER_DIRECTORY_UNAVAILABLE` 和 `MEMBER_DIRECTORY_INVALID`。
- 回归：`tests/test_modular_member_directory.py`、`tests/test_modular_management_sources.py`，覆盖无本地投影的外部成员、普通成员权限、跨租户、停用、身份冲突、批量读取、运行服务保留和无本地资源的卡片名称。

- `tests_harness/test_source_boundaries.py`：公开契约无 ORM 导入；无本地资源行调用；错租户、撤权、版本变化和 schema 错误拒绝；SOP 固定版本及隔离状态。
- `tests_harness/test_sealed_e2e_engine.py`：注册替代员工、SOP 来源及资源目录/Provider，经真实 Node 引擎、模型网关和 MCP 完成执行；默认 SOP 和替代 SOP 生命周期的等待/恢复/完成。
- 代码模块不做运行中热换。注册表启动后冻结；更换来源/执行实现需要重启工作进程。
