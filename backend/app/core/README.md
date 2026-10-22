# app/core 结构约定

这个目录放「对话执行内核」。它曾经是 46 个文件平铺，2026-10 起按职责收进二级包。

## 分层（只允许向左依赖）

```
base ← converse ← plan ← capability ← sop ← engine
base ← knowledge_flow
artifacts（独立，只依赖 models / llm）
```

| 包 | 放什么 | 不该放 |
|---|---|---|
| `base/` | 横切基础：取消、UI 开关、请求编译、上下文投影 | 业务判断、模型调用 |
| `converse/` | 把执行结果说成人话：回复生成、人设提示 | 检索、工具调用 |
| `plan/` | 意图识别、任务拆分、斜杠命令、路由 | 具体执行 |
| `capability/` | 能力清单、发现、重放策略、产物登记 | 能力调用本身 |
| `sop/` | SOP 图规则、节点执行、边条件、槽位表单 | 通用对话逻辑 |
| `engine/` | 执行内核：门面、轮次编排、决策循环、能力调用、帧存储、会话租约、恢复 | 纯数据投影 |
| `knowledge_flow/` | 检索决策与预取（知识入库在 `app/knowledge/`） | 文档解析 |
| `artifacts/` | 产物生成：幻灯片、发票、文档审阅 | 对话编排 |

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
| 2 | `plan` + `capability` + `sop` + `knowledge_flow` + `artifacts` | 待做 |
| 3 | `engine`(11) | 待做 |
| 4 | 拆 6 个超大文件（最大 3023 行） | 待做 |

单文件上限 800 行，批次 4 完成后由 `--strict` 守住。

完整计划与执行记录在仓库根目录 `docs/core-refactor-plan.md` 与 `docs/core-refactor-log.md`（`/docs/` 未纳入版本库，属本地资料）。
