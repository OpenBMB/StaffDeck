# AgentLoop 跨宿主对拍

`tools/agent-loop-parity` 是只用于测试的确定性 harness，不改变 StaffDeck
Harness 状态机，也不改变 PilotDeck `AgentLoop`。

完整实验方法、版本和最近一次结果见
[`EXPERIMENT_RESULTS.zh.md`](./EXPERIMENT_RESULTS.zh.md)。
两组逐场景行为差异见
[`PARITY_BEHAVIOR_DIFFS.zh.md`](./PARITY_BEHAVIOR_DIFFS.zh.md)。

运行 orchestrator：

```bash
python tools/agent-loop-parity/run.py \
  --pilotdeck-root /path/to/PilotDeck-current \
  --staffdeck-root /path/to/StaffDeck-current \
  --scenario all
```

只运行完整 PilotDeck native/sidecar 对拍：

```bash
python tools/agent-loop-parity/run.py \
  --pilotdeck-root /path/to/PilotDeck-current \
  --staffdeck-root /path/to/StaffDeck-current \
  --pilotdeck-baseline working-tree \
  --pair pilotdeck \
  --pilotdeck-surface gateway \
  --adapter-timeout-seconds 30 \
  --scenario all
```

`--pilotdeck-surface gateway` 会启动真实 PilotDeck HTTP/WebSocket Gateway、Session、
ToolRuntime 和 PermissionRuntime。native 与 sidecar 只在 AgentLoop runner 选择上不同；mock
只位于外部模型和工具执行边界。`--pilotdeck-baseline working-tree` 用于隔离 sidecar 差异，改为
`origin/main` 时结果还会包含 baseline 与当前版本之间的产品漂移。

adapter 从环境变量读取 scenario、`q`、mock URL 和 trace 输出路径。缺少命令、
入口失败或未写 trace 都会标记为 `BLOCKED`，不会生成伪造成功结果。baseline
ref 会在临时 detached worktree 中运行，当前实现使用显式 root；临时 worktree
会在运行结束后移除。

StaffDeck 的两个 adapter 共享同一个 engine 实现，都会通过真实
`AgentLoop.handle_turn()` 进入 `HarnessV2Engine.run()`；两条产品执行路径只由
`PILOTDECK_AGENT_LOOP_ENABLED` 区分。adapter 会把所选 checkout 的 `backend`
显式放在 `PYTHONPATH` 首位，并在启动时校验 `app` 的真实模块路径。共享 `.venv`
只提供依赖，不得把 baseline 导回当前工作分支。PilotDeck native adapter 只从所选
checkout 的 `dist` 动态加载原生 `createAgentSession`。

比较器忽略时间、随机身份和 transport envelope，但保留消息、图片内容、模型
请求/响应、tool 调用顺序与参数、permission decision、checkpoint 投影、错误
分类、终态和用户可见输出。StaffDeck 的 `TaskExecutionResult` 只能通过显式
adapter 归一化；任何未声明差异都会使对拍退出码为 `1`。

场景矩阵当前包含 60 个 fixture，分为 `core-regression`、`core-resilience`、
`staffdeck-workflow` 和 `known-gap`。同时包含通用 AgentLoop 场景和 StaffDeck SOP/Harness 场景。SOP 场景的
fixture 由 `staffdeck_engine_impl.py` 转换为真实 `Skill`、`TaskFrame`、slot、
forced SOP snapshot、team/scheduled interaction，再通过正式的
`AgentLoop.handle_turn()` 入口执行。mock provider/tool 只提供确定性的外部边界
响应，不复制 StaffDeck 状态机。

报告把差异分成两类：`Semantic Differences` 包括 TaskFrame 状态/步骤、slot、
pending/awaiting/handoff、required capability、knowledge budget、依赖结果、
tool side effect、终态和用户输出，任何一项都会使场景失败；`Format Warnings`
包括 provider envelope、JSON 字段布局和 checkpoint 包装差异，只记录不改变退出码。

仓库同时提供真实实现：`pilotdeck_native_impl.mjs`、
`pilotdeck_sidecar_impl.py`、`staffdeck_legacy_impl.py` 和
`staffdeck_pilotdeck_impl.py`。baseline worktree 会软链接源 checkout 的
`node_modules` 或 `backend/.venv`，但构建和导入使用 baseline 自己的源码。
四个 `--*-cmd` 参数仅用于覆盖默认真实 adapter。
