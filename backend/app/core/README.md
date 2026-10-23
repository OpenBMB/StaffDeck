# app/core 结构约定

这个目录放「对话执行内核」。它曾经是 46 个文件平铺，2026-10 起按职责收进二级包。

## 分层（只允许向左依赖）

```
base ← {converse, capability} ← plan ← sop ← knowledge_flow ← engine
artifacts（独立，只依赖 models / llm）
```

层级顺序按**实测依赖**定，不是按直觉排的：

- `capability` 在 `plan` 之下——`plan/slash.py` 用 `capability/discovery.py` 的 `model_descriptor` 解析模型参数；
- `knowledge_flow` 在 `sop` 之上——`knowledge_flow/routing.py` 用 `sop/edge_eval.py` 的边条件做路由打分；
- `graph_rules` 放 `base` 而不是 `sop`——它被 `plan/planner.py` 与 `sop/step_executor.py` 共用，必须落在两者之下。

| 包 | 放什么 | 不该放 |
|---|---|---|
| `base/` | 横切基础：取消、UI 开关、请求编译、上下文投影、纯图规则（`graph_rules`） | 业务判断、模型调用 |
| `converse/` | 把执行结果说成人话：回复生成、人设提示 | 检索、工具调用 |
| `plan/` | 意图识别、任务拆分、斜杠命令、路由、帧策略 | 具体执行 |
| `capability/` | 能力清单、发现、重放策略、产物登记 | 能力调用本身 |
| `sop/` | SOP 场景、节点执行、边条件、槽位表单（`slots/`） | 通用对话逻辑 |
| `engine/` | 执行内核：`facade`(门面)、`turn`(轮次)、`step_loop`(决策循环)、`invoke`(能力调用)、`frames`(帧存储)、`session/`(租约锁清理)、`recovery`、`attachments`、`finalizer`、`handoff`、`skill_runtime` | 纯数据投影 |
| `knowledge_flow/` | 检索决策与预取（知识入库在 `app/knowledge/`） | 文档解析 |
| `artifacts/` | 产物生成：幻灯片、发票、文档审阅 | 对话编排 |

`engine/deprecated/` 放旧执行路径（`StepAgent`、`ReflectionAgent`），生产代码已无引用，只剩 3 个历史测试在用，新代码不要碰。

`engine` 内部 `invoke` 与 `step_loop` 互相调用，属正常：它们是同一个执行回路的两端，不要拆到两个包。

## 新增代码放哪

1. 只依赖 `models` / `llm` / 标准库 → 放 `base/`。
2. 要读写 `ChatSession`、调模型、调工具 → 放 `engine/`。
3. 只做「输入 → 结构化决策」且不执行 → 放 `plan/` 或 `capability/`。
4. 与 SOP 图、槽位、边条件相关 → 放 `sop/`。
5. 不确定时先放 `engine/`，等它长出第二个调用方再上移。

## 两个脚本

```bash
cd backend
python scripts/core_import_graph.py --strict   # 层级越界 / 跨包循环 / 超大文件
python scripts/rewrite_core_imports.py         # 搬文件后批量改引用（先 git mv）
```

`rewrite_core_imports.py` 的 `MOVES` 表按批次追加，做完一个批次就留一条记录。

## 迁移进度

| 批次 | 内容 | 状态 |
|---|---|---|
| 0 | 校验与改写脚本 | 完成 |
| 1 | `base`(6) + `converse`(2) | 完成，57 处引用已改 |
| 2 | `plan`(4) + `capability`(4) + `sop`(6+`slots/`) + `knowledge_flow`(3) + `artifacts`(4)，另 `graph_rules` 入 `base` | 完成，97 处引用已改 |
| 3 | `engine`(16)，含 `session/`(4) 与 `deprecated/`(2) | 完成，105 处引用已改 |
| 4 | 拆 6 个超大文件（最大 3023 行） | 待做 |

批次 3 后 `app/core/` 顶层只剩 `__init__.py`（一行 re-export `AgentLoop`）。全部 46 个模块分属 8 个包，
层级越界 0、跨包循环 0。剩下的是体积问题：`engine/turn.py` 3023 行、`engine/facade.py` 1908 行等 6 个文件超 800 行，由批次 4 处理。

单文件上限 800 行，批次 4 完成后由 `--strict` 守住。

完整计划与执行记录在仓库根目录 `docs/core-refactor-plan.md` 与 `docs/core-refactor-log.md`（`/docs/` 未纳入版本库，属本地资料）。
