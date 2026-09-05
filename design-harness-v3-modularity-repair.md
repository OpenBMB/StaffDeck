# StaffDeck / DSH 模块化修订与接入契约

本文件描述 `codex/dsh-pluggable-runtime` 本次九项审查修复后的实现。
旧两份设计文档保留为历史方案；发生冲突时以本文件和可执行测试为准。

## 1. 九项修复对应关系

| 审查问题 | 当前实现与边界 | 主要代码 |
|---|---|---|
| 1. 注册不等于实际接线 | 网页、开放 API、定时任务先经过入口模块；定时 worker 调用实际 dispatch；团队通过 TeamHost；渠道的真实 adapter 查询及内置连接器 reconcile 读取模块状态 | `runtime/ingress.py`、`runtime/team_host.py`、`app/channels/adapters/base.py`、`app/scheduled_tasks/worker.py` |
| 2. Staff/SOP 层次绑定不完整 | Staff 默认模块、SOP 覆盖、节点逻辑槽覆盖；槽可以绑定资源、模块版本与 provider_config；空列表明确表示禁用，不再恢复默认值 | `composition/staff.py`、`slots.py`、`compiler.py` |
| 3. 插件依赖完整 Host 和私有实现 | 能力、记忆、交互使用纯数据调用对象与窄 Context；内置旧服务封装在 Host 侧；扩展操作通过 register_operation 注册，经稳定的 capability_invoke 代理进入，不再修改 Host 分支 | `contracts/provider.py`、`memory.py`、`interaction.py`、`operations.py`、`capabilities/local_services.py` |
| 4. Bridge 与旧 AgentLoop 耦合 | 两代引擎复用 TurnCoordinator；显式 TurnServices 适配原 SD 领域服务；不再临时改写 owner.memory、owner.response_generator、owner._enqueue_memory_capture；规划/回复策略在 runtime，DSH 会话传输在 bridge，模型协议在 app.llm | `app/core/turn_coordinator.py`、`turn_services.py`、`runtime/model_phases.py`、`bridge/session_runner.py`、`app/llm/tool_protocols.py` |
| 5. Hook 的顺序和监管失效 | 依赖优先于数字顺序；关键 Hook 异常拒绝，非关键观察者可跳过；完成工具也执行输出监管；真实回执/结果参与必需能力检查；重放重新鉴权、重新过后处理，只缓存监管投影 | `interactions/pipeline_host.py`、`capabilities/host.py`、`ledger.py`、`bridge/task_agent.py` |
| 6. 通知与回复是旁路实现 | 实际 HumanHandoffService 读取指派模块；AgentLoop 的通知入口读取通知模块；网页/渠道回复共享独立回复恢复服务，并调用回复模块；等待请求记录模块引用，配置不一致时拒绝隐式换绑 | `handoff/core.py`、`app/core/human_handoff_service.py`、`handoff_reply_service.py` |
| 7. 记忆只在 v3 局部可插拔 | v2/v3 共用同一个记忆 Host；召回、写入、管理列表与清理都调用同一 provider；SOP 可覆盖 Staff；外部记忆请求不含数据库、原始请求对象或模型密钥 | `contracts/memory.py`、`staffdeck_harness/memory.py`、`app/api/memories.py` |
| 8. PEP 局限于循环内 | 模块 API 对象访问、列表过滤、知识检索、实际渠道收发、记忆与通知增加 PEP；保留已有 OSS 领域检查；只支持 OSS_LOCAL/BUSINESS_BASE 两个部署权限包，Staff/SOP 不能覆盖它 | `app/security/api_policy.py`、`module_policy.py`、`channels/host.py`、`security/` |
| 9. 版本与卸载生命周期不实 | 回合持有旧注册表及版本/摘要；注册与启动分离；start/stop/dispose 及失败清理；停止接收新回合后排空，超时拒绝切换；进程必须收到 idle 才回池，取消/失败销毁进程 | `modules/registry.py`、`runtime/assembly.py`、`bridge/process_pool.py`、`session_runner.py` |

这里的“插件”是部署信任的 Python 模块，不是隔离执行的不可信代码沙箱。PEP 是必经宿主边界；它不能防御一个已被授予进程执行权限、主动绕开宿主去访问数据库的恶意 Python 包。安装权限保持为部署操作员权限。

## 2. 独立开发的颗粒度

- 业务能力模块：知识、通用技能、业务工具、扩展操作。实现 `invoke(ProviderContext, ModuleInvocation)`，返回 `ModuleResult`。
- 记忆模块：实现 `invoke(MemoryContext, MemoryCall)`，处理 `recall/capture/list/clear`。不能只替换召回而继续把管理请求发往另一个存储。
- 交互模块：实现一个或多个命名 Hook。默认套件现在由九个独立子模块组成；套件和子模块都可选择/停用。它们可进入步骤前、工具前后和输出监管点。
- 通知、指派与回复模块：推荐实现 `invoke(InteractionContext, InteractionCall)`。内置 `propose/notify/resolve` 对象作为兼容适配器保留，不是新插件必须依赖的 ORM 接口。新的主动通知渠道由 adapter 自己提供 `handoff_target`，不再要求修改 outbox 的渠道 target 表。
- 团队模块：TeamHost 为新插件提供纯数据调用和受控本地发布服务；现有团队持久化、任务 ID 与恢复逻辑仍复用 SD。
- 渠道模块：实现现有 ChannelAdapter 协议；使用 manifest 的 `metadata.channel` 声明渠道名，替换时停用同名默认 adapter。可选反应、附件、主动通知仍属于渠道适配模块内部。
- 引擎模块：实现 `open`，进入 `runtime.engine`；内置 v2/v3 的员工灰度选择保留，另一个已激活引擎模块会被实际选中。

数据库、事务、Invocation Ledger、TaskFrame 持久化、SOP 状态提交、Inbox/Outbox 不作为任意第三方可替换存储。它们是宿主服务。兼容适配器内部继续使用原 SD 服务，不要求将数据库再复制或拆出一套。

## 3. 如何放进 Staff / SOP

员工现有更新 API 的 `metadata.module_bindings` 保存默认模块选择。例如：

```json
{
  "module_bindings": {
    "runtime.memory": {"module_id": "memory.acme", "module_version": "1.0.0"},
    "staff.interaction": ["interaction.persona", "interaction.sop_execution_slice", "acme.output_review"],
    "handoff.notifier": ["handoff.notifier.web", "handoff.notifier.feishu"]
  }
}
```

`acme.*` 为示例包 ID，使用前必须安装并启用。未写的槽继承部署默认值；`[]` 禁用整个槽。权限包、运行内核不是 Staff 可以覆盖的槽。

在员工的 SOP 资源绑定 `AgentResourceBinding.metadata_json` 上保存 SOP 覆盖：

```json
{
  "module_bindings": {"runtime.memory": []},
  "slot_bindings": {
    "query_order/orders": {
      "resource_id": "tool_orders",
      "provider_module_id": "acme.orders",
      "module_version": "1.0.0",
      "provider_config": {"region": "cn"}
    }
  }
}
```

`query_order/orders` 表示节点 ID / 逻辑槽名；只写 `orders` 则是该 SOP 内的槽默认值。原有 `"orders": "tool_orders"` 字符串写法继续兼容。
同一资源的活动 SOP 节点绑定优先于 Staff 通用绑定，不会因为通用 grant 排在前面而调错 provider。
资源仍必须是该员工可用的资源；模块绑定不是授权。

## 4. 新能力的最小注册方式

```python
from staffdeck_harness.contracts.invocation import ModuleResult
from staffdeck_harness.contracts.manifest import ModuleKind, SlotName
from staffdeck_harness.contracts.operations import OperationContract
from staffdeck_harness.modules.registry import manifest

class Weather:
    def invoke(self, context, invocation):
        # context 没有 db、AgentLoop、model_config 或完整 Host。
        return ModuleResult.ok({"city": invocation.arguments["city"], "forecast": "sunny"})

def register(registry, ctx):
    registry.register_operation(OperationContract(
        "weather.lookup/v1", resource_type="tool", action="use",
        parameters={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    ))
    registry.install(
        manifest("acme.weather", "天气服务", version="1.0.0", kind=ModuleKind.CODE,
                 slots=[SlotName.STAFF_CAPABILITY], provides=["weather.lookup/v1"],
                 policy_actions=["weather.lookup/v1"]),
        Weather(), slot=SlotName.STAFF_CAPABILITY,
    )
```

在已有工具资源的 Staff 绑定 metadata 上设置 `operation: weather.lookup/v1`、`provider_module_id: acme.weather`。
模型通过 `capability_describe` 获取 schema，再通过 `capability_invoke` 调用。Host 校验 schema、资源激活、PEP、回执和后处理。插件不能改写已存在的内置权限操作语义。

## 5. 生命周期与兼容约束

注册函数只声明模块，不应启动线程或打开长连接。可选生命周期方法：

```python
def start_module(self, config): ...   # 激活时获取资源
def stop_module(self): ...            # 排空后停止
def dispose_module(self): ...         # 失败、预检、卸载后的最终清理；须可重复调用
```

- 配置保存不是热替换；新装配在重启成功后生效。Staff/SOP 资源选择在下一轮生效。
- 有在途回合时先排空；窗口结束仍未排空就拒绝本次切换，并恢复旧装配的接单能力。
- DSH 步骤调用 `finish_task` 关闭能力槽，但宿主继续等到引擎 idle。失败、超时、取消不会把忙进程放回池中。
- 后处理拒绝一个已成功执行的写操作，不会释放其幂等键，也不会把该副作用变成可重试。
- 企业权限配置失效时拒绝启动或切换，不自动改用开源权限。
- 最终回复在送入输出流之前经过监管；受监管时先缓冲文本，审查通过后再分块输出，避免先流出原文再“拒绝”。执行过程事件仍正常输出。
- 本次不增加钉钉/微信本来没有的主动私聊能力，不改变 DSH 的原生图片支持范围；原图片回退路径保留。

## 6. 验证

`backend/tests_harness/test_modular_boundary_regressions.py` 覆盖真实接口链路，不只测试类存在。
原模块测试保留业务结果断言，改为通过新 SDK 和共享服务接线。
`test_sealed_e2e_engine.py` 使用真实 DSH/Node、本机假模型和临时数据库验证 MCP 往返，不依赖外部模型凭证。
企业权限协议和对象/列表边界使用受控假权限服务验证；不能替代真实 Business 部署的联调。

本次验证记录（2026-09-05）：模块套件 539 通过、3 跳过、2 个已有 xfail；真实 DSH 封闭端到端通过；前端 247 通过，生产构建通过；隔离实例的管理员模块页与模型页可正常访问。
后端与模块全量运行 2596 通过，保留 11 个原有失败：9 个旧渠道 SQLite 迁移断言失败，2 个飞书 target 字段断言失败；这 11 项已在修改前 `815f7c8a` 的独立副本复现，不是本次九项模块化修复引入的回归。
