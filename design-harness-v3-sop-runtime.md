# SOP 运行控制模块迁移

## 归属与装配

SOP 的节点、槽位、分支待办、终态、挂起恢复由 `staffdeck_harness.sop` 实现。
不再由旧 `AgentLoop` 的私有方法实现，也不由 Harness v3 引擎的 AgentLoop 决定持久化状态。

| 层 | 实现与职责 | 拼接点 |
| --- | --- | --- |
| 运行调度内核 | `TurnCoordinator`：轮次、预算、租约、调度与事务 | `SopHost` |
| SOP 运行模块 | `sop.lifecycle/state/graph/finalizer`：流程实例及状态规则 | `SopRuntimePort` |
| SOP 前处理/监管 | 现有 `sop.execution_slice`、`sop.output_supervisor` 交互模块 | Loop 前后 hooks |
| Bridge | 引擎上下文、模型调用、步骤结果与事件的适配 | Harness v3 引擎 SDK、固定步骤提交协议 |
| 持久化内核 | 原有 TaskFrameStore、循环 checkpoint、CAS 和数据库 | 复用，不新建存储插件 |

`sop.runtime` 注册到专用单实现槽 `runtime.sop`，通过 `build(SopDependencies)` 创建
实现 `SopRuntimePort` 的实例。依赖只有数据库会话、事件记录和人工协作入口，没有旧 AgentLoop。
SOP 的 Staff 绑定/内容装卸仍在 `staff.sop`，不是用更换整个状态机来添加一个员工流程。

## 调用边界

1. 调度器选中 SOP TaskFrame，调用 `SopHost.activate_frame`；Host 按当前权限校验 SOP。
2. 模块激活/恢复执行位置；已有前处理投影当前 SOP 步骤，Bridge 交给 Harness v3 引擎执行。
3. Harness v3 引擎提交结构化结果；调度器调用 `SopHost.after_execution`，Host 再次复核权限。
4. SOP 模块检查必填槽位、决定默认下一节点、处理分支、人工介入和完成判定，返回 `SopAdvance`。
5. 调度器只根据返回结果继续执行或停止本次推进，并用已有租约/CAS 保存任务帧和循环状态。

普通对话不进入 SOP 步骤推进。挂起/恢复继续关联同一 SOP 执行实例，跨 SOP 实例上下文隔离。
历史版本的节点推进和事件顺序沿用；没有改表、重建数据库或引入另一份 SOP 状态真相。

## 独立开发与替换

可独立开发：SOP 运行实现、图规则测试、前处理/监管 hooks、SOP 内容包。
实现包不得提交调用方事务、绕过权限/租约、直接驱动模型或工具；复用现有状态字段及版本语义。
`SopRuntimePort` 明确列出前处理所需目录/状态接口、激活恢复接口及结果推进接口。

部署级替换步骤：外部包的注册函数安装另一个 `runtime.sop` provider（声明 `sop.lifecycle/v1`），
在启动配置中关闭默认 `sop.runtime` 并启用新实现。注册表校验同一槽只能有一个实现，
Host 校验返回的运行实例是否满足协议。未知/缺失实现失败关闭，不回落到旧 AgentLoop。
Host 固定执行权限检查，所以替代实现无法通过省略 PEP 声明来取消权限校验。

这是启动级替换，不是执行途中热换；现有运行应先排空，再加载新代际。
已有实例的 schema/状态迁移兼容性由替代实现负责，不能只实现方法名就宣称兼容。

## 兼容入口与验收

旧 `app.core.skill_runtime`、`graph_rules`、`turn_finalizer` 变为兼容导出；
旧 AgentLoop 的 SOP 方法只向新模块委托。v3 调度器没有反向调用这些方法。
其余会话入口、消息/渠道服务仍复用现有代码，本次不宣称整个应用全部迁入新包。

- 原 v2、SOP 状态、图规则、人工策略及终态回归测试保留。
- 图规则可替换接点测试迁到实际拥有规则的 SOP 模块。
- 独立模块测试覆盖等待、恢复、推进、完成，以及注册替换、协议拒绝和权限拒绝不改状态。
- 真实 Harness v3 引擎两节点 SOP 测试将旧 AgentLoop SOP 方法改为抛错，仍须挂起、恢复和完成。
- AST 依赖测试禁止 SOP 包依赖旧 AgentLoop、调度器或旧状态实现路径。

## 验证记录（2026-09-05）

- SOP/v2/图规则/人工策略及整个模块测试组：708 passed、3 skipped、2 xfailed。
- 真实 Harness v3 引擎：7 passed，包括默认 SOP 实现和通过插件发现/禁用默认实现后装配替代模块两种路径。
  两种路径都禁止调用旧 AgentLoop 的 SOP 状态方法。
- 独立进程导入 SOP 模块未加载旧 AgentLoop、TurnCoordinator、旧状态实现或 Harness v3 worker。
- 最后一次全量回归：2613 passed、13 failed、3 skipped、2 xfailed。
  其中 11 项是此前已确认的渠道迁移/字段预期基线问题；另两项为微信恢复与公共 API 事件测试，
  隔离复跑相关组 45 passed。旧提交独立副本的微信/公共 API 组也出现微信时序失败（60 passed、1 failed），
  但不是完全相同的失败用例，因此不把这两项直接宣称为已证明的同一基线故障。
  本次未修改渠道或公共 API 的业务代码。
- 5173 已重新部署，生产前端构建通过；管理员实际查看新 SOP 模块启用状态。
  使用已有部署验证会话进行纯对话冷恢复，仍正确返回旧测试标记；业务能力调用数为 0。
  未执行真实业务 SOP，状态推进与替代模块使用上述隔离 Harness v3 测试验收。
