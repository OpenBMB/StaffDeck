# 完整调用链路
以用户给飞书机器人发一条 p2p 文本消息为例（群里 @ 机器人类似，多一步 mention 清洗）。

## 阶段一：子进程接收并归一化（feishu_runtime.py 子进程内）

1. 飞书 WebSocket 推送事件帧 → ProductionClient._handle_data_frame（feishu_runtime.py:180）给 watchdog 挂 token，调父类处理。
2. 事件分发 → EventDispatcherHandler（由 _build_event_dispatcher 构造，feishu_runtime.py:107）匹配到 p2_im_message_receive_v1，调用 receive 回调（feishu_runtime.py:154）。
3. 归一化 → _normalize_event(event, bot_open_id)（feishu_runtime.py:26）：
- 校验 header.app_id / tenant_key / message_id / sender.open_id
- 过滤掉 bot 自身、非 text 类型、空文本
- 群消息要求 @ 到本机器人，并剔除 @ key
- 构造 ChannelInbound（channel="feishu"、event_id=message_id、from_user_id=open_id、session_id=chat_id 或 open_id、text）
- 同时构造 target dict（message_id / receive_id_type / receive_id）
4. 落库暂存 → stage_feishu_inbound(...)（service_feishu_inbox.py:59）：
- 校验 binding 活跃 + revision + external_account_key + provider_tenant_key + identity_scope_key（首次写入钉住租户）
- encode_replay_envelope 把 ChannelInbound 序列化进 envelope（含 app_id/tenant_key）
- 写入 ChannelInboundEvent 表，status="received"，(binding_id, event_id) 唯一约束防重
- 返回 StageResult，STAGED 时带 event_pk
5. 通知父进程 → control.emit("INBOX_STAGED", event_pk=...)（feishu_runtime.py:171）通过 Pipe 发给 supervisor。

## 阶段二：父进程唤醒 intake（feishu_process.py → service_intake.py）
 
 6. supervisor 收到事件 → _accept_event（feishu_process.py:215）匹配 binding_id + config_revision + child_nonce + pid，写入 record.events 队列；遇到 INBOX_STAGED 调 wake_staged_inbound_worker()（service_intake.py:1064），_staged_inbound_wake.set()。
 7. intake 守护线程 → run_staged_inbound_daemon（service_intake.py:1068）被唤醒，查 ChannelInboundEvent 表 status="received" 的行，对每个 event_pk 调 process_staged_inbound（service_intake.py:967）。
 8. claim 抢占 → claim_staged_inbound（service_intake.py:167）用 UPDATE ... WHERE status="received" 原子地把状态置 processing 并写 processor_run_id，rowcount==1 才继续；失败/并发被别的线程抢走则跳过。
 9. 解码 envelope → _decode_and_validate_staged_event（service_intake.py:1125）重新校验 binding.external_account_key / provider_tenant_key / identity_scope_key 与 envelope 中的 app_id / tenant_key 一致，还原出 ChannelInbound。
10. reaction 标记 → _stage_received_reaction（service_intake.py:465）登记一条 ChannelDelivery(kind="reaction_add") 给原消息挂"处理中"标记（飞书支持 reaction 时）。
11. → process_inbound(binding, inbound, staged_event_pk=event_pk)（service_intake.py:707）。

## 阶段三：身份/会话锚定 + 对话轮（service_intake.py 主线程，持 _session_lock）

12. 指令拦截 → parse_command(inbound.text)（service_intake.py:728），若是 /bind、/员工 等指令直接 _stage_notice 回复并标记 event done，不进 AgentLoop。
13. 身份解析 → external_identity_for_message + resolve_or_provision_user（service_intake.py:858）按 open_id + scope 找或创建本地 User。
14. 去重 → _client_turn_seen_in_conv / _user_message_with_client_turn_exists 防崩溃恢复重放。
15. agent 路由 → resolve_current_agent（service_intake.py:875）取当前指针员工；maybe_auto_route（service_intake.py:878）可选 LLM 意图分发切换 agent。
16. 会话锚定 → find_or_create_channel_session（service_intake.py:881）按 binding + external_conv_id + agent_id 找或建 ChatSession，并把 target 写进 channel_target_json。
17. typing 上行 → _send_wechat_typing(..., state=1)（service_intake.py:940）通过 outbox 登记一条"对方正在输入"投递（飞书会走对应渠道的 send）。
18. 构建请求 → ChatTurnRequest(tenant_id, session_id, agent_id, user_id, message=_message_text(...), channel="feishu", client_turn_id=inbound.event_id)（service_intake.py:931）。
18a. trace 卡片初始化（仅 channel_feishu_trace_enabled 开启且 binding.channel=="feishu"）→ FeishuTraceStreamer.start()（feishu_trace.py）调 adapter.create_card 创建"正在执行"交互式卡片（msg_type=interactive），保存 message_id；失败仅记日志不阻塞 turn。streamer.on_event 作为 event_sink 传入 AgentLoop。
19. 执行对话轮 → AgentLoop(db, event_sink=streamer.on_event).handle_turn(request)（service_intake.py:962）→ HarnessV2Engine.run（harness_v2_engine.py:88）：
- 会话锁 acquire_harness_session + lease
- turn_store.claim 抢 turn（防并发同 client_turn_id 重放）
- _append_message(role="user") 落 Message 表
- _conversation_context 取历史消息
- planner.plan 生成 plan（含 router_decision）
- 跑 task frame / tool calls / materialize_task_attachments（若有附件，当前飞书入站 attachments=[]，这条链目前不走）
- LLM 产出回复 → _append_message(role="assistant")
- events.record 记 assistant_message_created 等，每个事件同步回调 event_sink → FeishuTraceStreamer.on_event → 重建 trace 行 → 节流 PATCH 更新"正在执行"卡片（仅 trace 开关开启时，见 18a）
20. typing 下行 → finally 里 _send_wechat_typing(..., state=2) 撤销输入态。
20a. trace 卡片定格 → turn 正常结束调 streamer.finish() 把卡片定格为"执行完成"（green）；异常路径调 streamer.abort() 定格为"执行失败"（red）。卡片更新失败仅记日志。
21. event 收尾 → event 置 done + processed_at（service_intake.py:959），提交。异常路径置 failed + _stage_error_notice。

## 阶段四：回复投递（service_outbox.py 守护线程）

22. 回复登记：HarnessV2Engine 在产出 assistant 消息时，由 ResponseGenerator/delivery 桥接把回复文本登记为一条 ChannelDelivery(kind="reply", status="pending", target_json=<ChatSession.channel_target_json>)（service_outbox.py:62 / :214）。
23. delivery 守护轮询 → run_delivery_daemon（service_outbox.py:657）→ _run_delivery_lane → _deliver_due（service_outbox.py:227）捞 pending 且到点的投递。
24. 单条投递 → _deliver_one_locked（service_outbox.py:388）：
- 校验 binding active、session 与 binding 账号一致
- get_channel_adapter("feishu") 取飞书适配器
- adapter.send(binding, target, text, idempotency_key=...)（service_outbox.py:506）调用飞书 im/v1/messages 发送回复（target.message_id 用于回复式消息，或 receive_id 发新消息）
- 成功置 delivered；失败按 retryable + max_attempts 重试或置 failed
25. reaction 收尾：reply 投递成功后，若该 event 之前挂过"处理中" reaction，_event_has_delivered_response 为真，登记一条 kind="reaction_remove" 清掉标记（service_outbox.py:495）。
链路全景图
飞书用户发消息
  │
  ▼
飞书 WS ──> 子进程 ProductionClient._handle_data_frame     [feishu_runtime.py:180]
  │           └> EventDispatcherHandler → receive         [feishu_runtime.py:154]
  │               └> _normalize_event                     [feishu_runtime.py:26]
  │               └> stage_feishu_inbound                 [service_feishu_inbox.py:59]
  │                   └> 写 ChannelInboundEvent(received)
  │           └> control.emit("INBOX_STAGED", event_pk)    [feishu_runtime.py:171]
  ▼
父进程 FeishuProcessSupervisor._accept_event               [feishu_process.py:215]
  └> wake_staged_inbound_worker()                          [service_intake.py:1064]
  ▼
intake 守护线程 run_staged_inbound_daemon                  [service_intake.py:1068]
  └> process_staged_inbound(event_pk)                      [service_intake.py:967]
      ├> claim_staged_inbound (UPDATE→processing)          [service_intake.py:167]
      ├> _decode_and_validate_staged_event                 [service_intake.py:1125]
      ├> _stage_received_reaction                          [service_intake.py:465]
      └> process_inbound(binding, inbound, event_pk)      [service_intake.py:707]
          ├> parse_command（指令直接 _stage_notice 返回）
          ├> resolve_or_provision_user
          ├> resolve_current_agent / maybe_auto_route
          ├> find_or_create_channel_session
          ├> _send_wechat_typing(state=1)
          ├> FeishuTraceStreamer.start()（trace 开关开启时）  [feishu_trace.py]
          │   └> adapter.create_card → 创建"正在执行"卡片，保存 message_id
          ├> AgentLoop.handle_turn(ChatTurnRequest, event_sink=streamer.on_event)  [agent_loop.py:136]
          │   └> HarnessV2Engine.run                       [harness_v2_engine.py:88]
          │       ├> acquire_harness_session + turn_store.claim
          │       ├> _append_message(user)
          │       ├> planner.plan + router_decision
          │       ├> TaskFrame / tools / LLM
          │       ├> events.record → event_sink → streamer.on_event → 节流 PATCH 更新卡片
          │       ├> materialize_task_attachments（当前 attachments=[] 不走）
          │       └> _append_message(assistant) → 登记 ChannelDelivery(reply,pending)
          ├> streamer.finish() / streamer.abort()（trace 开关开启时）
          ├> _send_wechat_typing(state=2)
          └> event → done
  ▼
delivery 守护线程 run_delivery_daemon                      [service_outbox.py:657]
  └> _deliver_due → _deliver_one_locked                   [service_outbox.py:388]
      └> adapter.send(binding, target, reply_text)        [service_outbox.py:506]
          └> 飞书 im/v1/messages 发回复
      └> 成功 → delivered；失败 → 重试/failed
      └> 若有 reaction → 登记 reaction_remove 清标记
  ▼
用户在飞书看到机器人回复
关键设计点
- 子进程隔离：每个 binding 一个 spawn 子进程跑 WS 长连接，崩溃/backoff/重启不影响主进程，由 FeishuProcessSupervisor 监控。
- durable inbox：事件先落 ChannelInboundEvent(received) 再异步处理，即使进程在处理中崩溃，重启后 sweep_stale_inbound_events 会把 processing 遗留行回收重跑（service_intake.py:1243）。
- 幂等：claim_staged_inbound 用 UPDATE WHERE status="received" 原子抢占，(binding_id, event_id) 唯一约束防重投递；turn 层 turn_store.claim 再防一次。
- 串行：_session_lock(session_id) 保证同一会话同一时刻只有一个 turn 在跑。
- 附件断点：当前 _normalize_event 在 message_type != "text" 时直接返回 None 丢弃，这正是 channel-attachments-plan.md 要改造的入口点。
- 实时 trace 卡片：channel_feishu_trace_enabled 开启时，intake worker 在 handle_turn 前创建一张"正在执行"交互式卡片，通过 EventLog.event_sink 钩子把每个 trace 事件（SOP 匹配/步骤/工具/知识检索）实时渲染为可读行并节流 PATCH 更新卡片，结束后定格为完成/失败状态。卡片是"进度展示"，与正文回复（outbox 幂等投递）互不影响；卡片创建/更新失败仅记日志不阻塞 turn。开关关闭或非飞书渠道走原路径不创建卡片。