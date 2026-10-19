# A 方案（合并 `00ed05f`）到底能省多少时间 —— 基于真实事件的核算

样本：`session_ab6c0ec2064069eb`（员工 **AI G.O**，问题「项目在哪」，单轮 16.33s，含完整 route 事件）。  
聚合样本：`harness.task_action` n=16、`knowledge_search` n=15、路由 LLM 调用 n=15。

## 一、先更正一个前提（我上一条给错了）

我说过「决策轮 7.2k/4.5s → 159 tok/1.6s」，暗示省 3~4.5s。**实际 iter1 早就在轻量模型上**：

```
06:19:15.662 LLM harness.task_action iteration=1 lightweight_decision=True
             payload_chars=14293  model=deepseek-v4.1-flash
06:19:20.332 LLM ... duration_ms=4669.3 input_tokens=7163 output_tokens=156
```

`harness_agent.py:302-315` 的「轻量节点直通」已经在首轮生效：`iteration == 1` 时用  
`lightweight_model_config` + `LIGHTWEIGHT_DIRECTIVE`（`harness_agent.py:80`）。  
所以 A 方案替换的是**轮轻量决策**，不是轮满血决策。

**净收益 = iter1 实测耗时 − judge 耗时**，而不是「iter1 → 0」。

## 二、真实轮次预算（AI G.O，16.33s）

| 阶段                           | 起止              | 实测         | 说明                                       |
| ---------------------------- | --------------- | ---------- | ---------------------------------------- |
| 前置（建帧/路由/装配）                 | 14.654 → 15.662 | **1.01s**  | `turn_plan_created`→`task_frame_started` |
| 决策轮 iter1                    | 15.662 → 20.332 | **4.67s**  | 轻量，7163 in / 156 out                     |
| `knowledge_search`           | 20.347 → 23.332 | **2.98s**  | 内含两次路由 LLM                               |
| └ `knowledge.document_route` | 20.496 → 21.803 | 1.31s      | 602 in / 20 out                          |
| └ `knowledge.bucket_route`   | 21.868 → 23.208 | 1.34s      | 1182 in / 36 out                         |
| └ 其余（词法+DB+装配）               | —               | 0.33s      | 仅 11%                                    |
| 作答轮 iter2                    | 23.365 → 30.785 | **7.42s**  | 满血，18889 in / 427 out                    |
| 收尾                           | 30.785 → 30.981 | **0.20s**  |                                          |
| **合计**                       | 14.654 → 30.981 | **16.33s** |                                          |

`memory.capture`（2815 in / 4.45s）在 `complete` **之后**才跑完 → **不在用户感知路径上**，  
算「一轮几次 LLM 调用」时算它，算「一轮多慢」时不该算它。

## 三、A 方案的净收益

| 口径              | iter1 | judge  | 净省        | 轮次降幅                     |
| --------------- | ----- | ------ | --------- | ------------------------ |
| 本样本轮（iter1=p90） | 4.67s | ~1.60s | **3.07s** | 16.33 → 13.26s（**−19%**） |
| 全样本 p50（n=16）   | 3.68s | ~1.60s | **2.08s** | 约 −13%                   |
| 全样本 mean        | 3.81s | ~1.60s | 2.21s     | 约 −14%                   |

judge 耗时取自 2026-10-13 实测 **1.6s**（`knowledge.prefetch_judge`，159 tok，thinking 关闭，  
超时 10s；`output_policy.py:67`）。该事件已被 `agent_events` 清理，无法重测。  
敏感度：judge 若 1.2s → p50 省 2.48s；若 2.5s → 只省 1.18s。**judge 越快，A 越值。**

Token 账（本样本）：iter1 的 **7163 tok 被 ~160 tok 取代**，本轮输入 token 降 **27%**  
（26052 → 19049；含两次路由则 27836 → 20833，降 25%）。

**作用范围限制（重要）**：`plan_knowledge_prefill_actions` 有四道前置条件  
（`knowledge_prefetch.py:132-160`）：`first_turn=True`、`kind=="conversation"`、  
`required_knowledge_base_ids` 为空、manifest 里无 `general_skill`/`tool`。  
→ **只有"新帧首轮"才省**；同会话追问、SOP 帧、带技能的员工一律不省。  
本样本 18 个 turn 时长从 6.7s 到 114s 不等，长轮（5 个 iteration）里 A 只省 1/N。

## 四、比 A 更大的两笔（同一轮样本）

`knowledge_search` 这一步 **2.98s 里有 2.65s（89%）是两次路由 LLM**。  
聚合口径（n=15）：搜索总 p50 **6.76s**，路由 LLM p50 **2.65s**（39%）；  
慢会话里占比 **89% / 94% / 96%**（`session_ab6c0ec2` / `37b7e8e6` / `b8d9972e`）。

| 路由                         | n | p50 耗时     | p50 输入   | p50 输出 |
| -------------------------- | - | ---------- | -------- | ------ |
| `knowledge.document_route` | 7 | **2292ms** | 652 tok  | 93     |
| `knowledge.bucket_route`   | 8 | **4592ms** | 1234 tok | 559    |

1. **`document_route` 单候选短路**：AI G.O 只有 1 个候选文档（探针实测 `candidate_count=1`），  
   却花 1.31s 让 LLM 从 1 个选项里选 1 个。加一条 `candidate_count <= 1 → 直接选中`  
   的确定性守卫即可，**风险近乎为零**。（`SMALL_CANDIDATE_CHUNK_LIMIT=50` 挡不住它——  
   AI G.O 是 51 chunk。）
2. **Laya 接管两个路由**：这正是 `laya_router.score_candidates` 的目标，  
   可省掉 2.65s 中的大部分（Laya 是 ~百 ms 级往返）。注意上一轮发现的 oversize 缺陷  
   已由你 `MAX_CANDIDATES=60` 修掉。

## 五、叠加账（同一轮样本，实测值）

| 阶段                   | 现状         | A 方案       | + 单候选短路    | + Laya 接管路由 |
| -------------------- | ---------- | ---------- | ---------- | ----------- |
| 框架开销                 | 1.21s      | 1.21       | 1.21       | 1.21        |
| 决策轮 iter1            | 4.67s      | —          | —          | —           |
| judge                | —          | 1.60       | 1.60       | 1.60        |
| `document_route` LLM | 1.31s      | 1.31       | —          | —           |
| `bucket_route` LLM   | 1.34s      | 1.34       | 1.34       | —           |
| 搜索其余                 | 0.33s      | 0.33       | 0.33       | 0.33        |
| 作答轮 iter2            | 7.42s      | 7.42       | 7.42       | 7.42        |
| 收尾                   | 0.20s      | 0.20       | 0.20       | 0.20        |
| **合计**               | **16.33s** | **13.26s** | **11.94s** | **10.61s**  |
| **降幅**               | —          | **−19%**   | **−27%**   | **−35%**    |

**A 方案单独：省 3.07s（−19%）。加上另外两项：省 5.72s（−35%）。**

落地顺序建议：A（已有代码 + 15 个测试 + 可字符串开关回退）→ 单候选短路（3 行）→ Laya。  
A 的收益最小但最稳；Laya 的收益最大但需先过准确率评测（见 `07_next_probes.md`）。

## 六、口径提醒

- 本样本 16.33s 的数字是**完整轮次**；harness 的 `stream_delta` 全部聚集在收尾前 ~60ms  
  （非逐 token 流出），所以用户感知等待 ≈ 总时长，不存在「TTFT 更短」的缓冲。
- 全部数字来自 `tenant_demo` 生产事件，**只读查询**，未触发任何 LLM/Laya 调用。
- 单轮采样；p50/p90 取自 16 条 `harness.task_action`。样本量小，量级可信、小数位不可信。
