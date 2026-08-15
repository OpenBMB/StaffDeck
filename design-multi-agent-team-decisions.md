# 多 Agent 团队功能 · 决策记录 v1.1

> 日期：2026-08-10（v1）；2026-08-11（v1.1 修订，体验测试反馈）
> 状态：已锁定（经 ni-unknown-first 关键决策访谈确认）
> 背景：当前平台只有独立的数字员工（`agent_profiles`），不存在团队、任务下发、认领、共享黑板机制。本记录锁定该功能的 5 组关键决策，作为数据模型与架构边界的依据。
> v1.1 修订：决策 2 赛制改为 3 轮血条制；决策 3 补充工作区聊天集成。增量 1-4 已全部实现并真机验收通过。

## 贯穿原则：HITL（Human in the Loop）

> 产品界面统一使用“项目领导”；`TL` 仅保留为内部角色、数据字段与 API 的兼容标识。

人在每个关键环节保留介入点：下发可审、竞标可看可改判、过程以多线程方式全程可见、结果可收、黑板可治理。

## 决策 1：TL 身份

- **选择**：TL 由数字员工扮演（团队成员的"角色标记"）+ 人类控制台兜底
- **理由**：纯人工 TL 丧失多 agent 差异点；纯 agent TL 的运行时风险（唤醒循环、错误派发）会拖垮 v1。混合制让 v1 即可演示完整闭环，人始终可介入
- **被否**：纯人工 TL（降级为兜底通道）；纯 agent TL（v2 演进目标，架构天然兼容）

## 决策 2：任务认领机制（v1.1 修订：3 轮血条赛制）

- **选择**：指派 + 认领混合 + **竞标制**——≥2 名候选且 TL 未锁人时开启竞标。赛制（v1.1）：默认 3 轮（团队 config `bid_rebuttal_rounds` 可调，0=直接裁决），round 1 陈述、后续轮反驳；**每轮结束 TL 轻量打分（0-10）**，候选 HP = 100 − Σ(10 − 该轮得分) × 3，下限 0；HP 归零淘汰（`bid_eliminated` 审计），存活 ≤1 人提前终局；末轮后 TL 在存活者中裁决（bid_award 块），胜者为王。打分失败兜底全员 5 分不阻塞流程。超时/失败候选 → TL 用已有材料照样判
- **理由**：认领动作有理由、可审计，契合 HITL；候选上限 3 人控制成本；血条由真实打分驱动，不做假数据
- **被否**：纯指派（保留为 TL 锁人选项）；纯认领抢单（无人认领/错误认领风险）；无界自由辩论（v2）；单次裁决无血条（v1 原案，体验不足被 v1.1 替代）

## 决策 3：执行载体与反馈 + 多线程（v1.1 修订：工作区聊天集成）

- **选择**：任务 = 绑定 `(team, task)` 的独立 harness 会话 + 团队上下文注入（任务描述/竞标记录/黑板 top-K/花名册）；完成产出三件套：结构化报告 + 黑板写入建议 + TL 唤醒事件；TL 验收运行判定通过/退回/升级。**任务全部后台异步执行**，统一线程列表跨团队自由切换；同一成员默认串行（并发上限团队级可配），跨团队不受限
- **v1.1 补充**：`ChatSession` 增加 `team_id` 列（SQLite ALTER 迁移）；团队 TL 会话落该标记，主聊天端点（`/api/chat/turn`、`/api/chat/stream`）命中团队 TL 会话时自动注入团队上下文并做派任务后处理（`process_tl_reply` 三处复用）。TL 对话因此是工作区 `/workspace/chat/:sessionId` 里的正规多轮流式会话，任务执行会话也可从看板直接跳转查看
- **被否**：全团队共享长会话（上下文污染、无法并行）；把人锚定在单任务对话的同步模型；团队页内嵌一次性对话面板（v1 原案，不符合 harness 会话特性，被工作区集成替代）

## 决策 4：黑板语义

- **选择**：TL 裁决写入（并入验收运行，零额外成本）+ 双通道读取（启动注入 top-K + `blackboard` 查询工具）+ 按团队隔离；写入走**轻量入库流水线**（对齐 `backend/app/knowledge/service.py` 的 `INGEST_STAGES`）：解析 → 规范化（去重/合并/更新，黑板是活文档）→ 结构化写入 → 生成引用（回链任务报告）→ 刷新黑板索引 → 高价值条目可升级走完整 `INGEST_STAGES` 沉淀到知识库
- **被否**：原始内容直写；自由集市直写（信噪比失控）；逐条独立审批（成本翻倍）

## 决策 5：权限与验收

- **选择**：建团队与建员工同权，创建者为 Owner；角色 = Owner / 协作人 / 成员（含 TL 标记，可随时换任）；黑板按团队隔离，跨团队流动只经由人；v1 不接飞书/微信渠道
- **v1 验收**：建团队→人对 TL 下需求→TL 拆解投放→竞标留痕→中标执行→报告+黑板建议→TL 验收/人可改判→超时升级，全链路无任务丢失、步步有审计
- **被否**：管理员审批建团队；v1 接渠道

## 表结构草案

- `teams`：id, tenant_id, name, description, owner_user_id, config_json（成员并发上限/竞标轮数/超时阈值）, status, created_at, updated_at
- `team_members`：id, team_id, agent_id, role（`leader`/`member`）, joined_at；唯一约束 `(team_id, agent_id)`；TL 只是 role，非特权实体
- `team_tasks`：id, team_id, parent_task_id（拆解层级）, title, description, priority, status（`pending/bidding/in_progress/review/rework/done/cancelled/escalated`）, created_by, suggested_agent_id, assignee_agent_id, session_id（harness 会话绑定）, timeout_at, report_json, review_json, version（乐观锁）
- `team_task_bids`：id, task_id, agent_id, round, kind（`statement/rebuttal`）, content, score, score_rationale
- `team_task_events`：审计流水（task_id, actor_type, actor_id, event_type, payload_json）
- `team_blackboard_entries`：id, team_id, content, tags_json, source_agent_id, source_task_id, citation_json, status（`pending_review/active/archived`）, pinned
- `team_wake_events`：唤醒队列（team_id, target_agent_id, trigger 类型, payload_json, status）——TL/成员的唤起机制

## 运行时边界

- **执行**：复用 Harness v2 的 session/run 设施；`team_tasks.session_id ↔ harness session` 绑定复用 `channel_binding_agents` 的既有模式
- **唤醒**：`team_wake_events` 由任务状态变迁产生，消费侧是 `scheduled_tasks` 触发器的泛化（事件驱动，非常驻进程）
- **并发安全**：认领/中标落库用条件更新的原子 claim，渠道层 `service_intake.py` 已有成熟同款
- **工具**：新增 `blackboard` 查询工具挂进成员执行会话
- **前端**：新增 TeamsPage（团队管理）+ 任务看板（竞标记录、报告、改判入口）+ 统一线程列表；不动现有 AgentsPage 的单员工路径
- **明确不做（v1）**：渠道接入、纯 agent TL 常驻循环、无界辩论、agent 自动跨团队信息流转
