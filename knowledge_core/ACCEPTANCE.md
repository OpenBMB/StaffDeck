# KM 独立化验收记录

## 可裁剪独立部署阶段：开发验收完成

本阶段使用 `acceptance/2026-10-14/modular-deployment` 的新证据。
此前插件收敛记录仅属于此前阶段，不能替代模块裁剪与离线降级验收。

最终门槛为 [modular-deployment/acceptance.json](acceptance/2026-10-14/modular-deployment/acceptance.json)：
17 项全部通过。复验命令见 [commands.md](acceptance/2026-10-14/modular-deployment/commands.md)。
缺失、失败、跳过、任务未终态、模型回退或证据文件不存在均不能通过。

| 验收面 | 最新证据与结果 |
| --- | --- |
| core、宿主及 PD runtime | `regression-final`：114 core、68 宿主兼容测试；`pd-runtime.xml`：66 测试，无失败或跳过 |
| 实际模块裁剪 | `composition-closeout`：9 配置的进程导入、路由、实际表检查通过；minimal 仅 6 张基础表，full 使用统一 KernelRuntime 与模块工厂，无旧服务运行时 |
| 组合浏览器 | `combination-browser`：minimal、仅认证、目录、目录加版本无空间、概念、发现、概念加发现、空间加目录版本共 8 组合通过；任务终态、编辑、查询、引用、Bucket/Chunk、桌面及移动端、禁用 API 不请求 |
| 取消与重启 | `tasks-closeout`：安装 wheel 的真实进程取消与硬重启通过；终态持久化、载荷清理、无残留半成品文档；组合测试覆盖并发冲突、取消和中断 |
| 降级与恢复 | `downgrade-closeout`：当前分支修订合并、共享内容及旧引用、持久备份、写入前恢复、中断恢复、写入后恢复拒绝及新增内容导出；迁移测试验证历史源转换后可由当前 Runtime 使用，重复迁移拒绝覆盖后续写入 |
| 自包含构建与词法 UI | `isolated-final`：只复制 knowledge_core 目录构建 UI/wheel、隔离安装和基础浏览器流程；实际服务关闭时导入审计通过 |
| 独立直连真实模型 | `isolated-model-final`：deepseek-v4-flash-0731 实际生成有效 Bucket、文档与 Bucket 路由；调用 ID 关联，无回退 |
| Docker | `docker`：自包含镜像构建、浏览器流程、6 张基础表、旧运行模块不可导入、正常关闭通过 |
| full wheel | `full-artifact`：隔离安装后 20 步 UI 流程、模块存储、实际进程宿主导入审计及正常关闭通过 |
| 四类入口递归兼容 | `compatibility-final`：固定 77c21973，189 案例、843 KM HTTP 调用、零差异；保留字段类型、null、顺序、错误详情及 envelope |
| SD/PD 完整生命周期 | `full-browser-current`：未经裁剪 app.main/Harness，双方主流程、版本、OKF、取消、发现审核/拒绝；SD 实际创建 Skill、Tool 和 receipt |
| PD 真实模型与 Agent | `pd-model`：无 SD 源码或服务、编译 PD 与安装 KM wheel；页面选择、后台入库、查询、真实 Agent 工具及 runtime 管理；provider1/deepseek-v4-flash-0731，14 次关联有效调用，无回退 |
| iframe 独立升级 | `iframe-upgrade`：固定 SD/PD 构建产物，仅替换 KM UI、重启 KM；两端 upgrade-before 至 upgrade-after，上传、下载、版本与移动端流程通过，宿主产物保持不变 |
| 发布与旧实现清理 | `publication.json`：审核、幂等发布、拒绝、丢失回写重试及历史发布保护；`cleanup.json`：67 张宿主表无知识存储，仅业务发布 receipt，无原生知识服务或 PD vendor 知识页面依赖 |

产品 wheel 不包含旧 `service/full_runtime/full_api/okf/versions/publication`。
历史 ORM 与迁移文件仅用于离线源库转换；普通启动不导入它们。
基础内容没有空间、目录或版本记录依赖；关闭模块不加载实现、不建表、不注册
业务路由。既有旧库需显式离线转换，不能静默切成 minimal。

调试中 fixture 与新运行时不匹配、旧 wheel 残留、atexit 审计未输出、解析取消
竞态及 Harness 子进程 PATH 缺少 Node 均曾导致失败，已修复并重新验收。
这些失败尝试及较早报告不作为当前通过证据。模型 fixture 只用于确定性发现与
发布流程，不计入真实模型证据。SD/KM 正常处理 SIGTERM；PD supervisor 的
该路径返回 1、Vite 返回 143，原始退出值保留，最终运行均无强制关闭。

本阶段无必需失败或未执行项。生产数据库转换、生产模型配置切换、提交与推送
未执行；这些不属于开发验收。转换工具仅在隔离副本演练。

```sh
backend/.venv/bin/python knowledge_core/scripts/modular_gate.py --evidence-dir knowledge_core/acceptance/2026-10-14/modular-deployment
```

## 插件收敛与 PilotDeck 能力对等阶段

本阶段独立验收，不把上一阶段 SD hosted model 结果扩大为 PD 能力对等。
最新门槛以 [plugin-convergence/acceptance.json](acceptance/2026-10-14/plugin-convergence/acceptance.json)
为准；命令见 [commands.md](acceptance/2026-10-14/plugin-convergence/commands.md)。
任何证据缺失、失败、跳过或任务未到终态均标记未验收。

| 门槛 | 本阶段证据 |
| --- | --- |
| core 与宿主回归 | `plugin-convergence/regression`：core 56、宿主兼容 68；PD 60 runtime/config/registry/gateway、12 页面桥及路由、SD 2 页面桥测试，无失败/跳过 |
| 四类入口递归矩阵 | `plugin-convergence/compatibility/matrix.json`：固定 77c21973，189 案例、857 KM HTTP 调用，零差异 |
| 完整 SD/PD 浏览器 | `plugin-convergence/full-browser`：未经裁剪的 app.main lifespan、两端 20 步知识流程、取消、建议审核/拒绝和 SD 实际 Skill/Tool 发布；另经 PD runtime 审核/回读/导出通用建议 |
| PD 真实模型 | `plugin-convergence/pd-model`：wheel 和编译 PD 产物在无 SD 源码/服务的运行目录；页面选择、异步入库、查询及真实 AgentLoop knowledge_query；provider1/deepseek-v4-flash-0731，最新复验 9 次宿主/KM 关联模型返回，无检索回退 |
| 独立 UI 升级 | `plugin-convergence/upgrade`：固定 SD/PD 前端产物，仅替换 KM UI 并重启 KM；upgrade-before 到 upgrade-after，两端再次通过上传/下载/版本及移动端流程，宿主文件尺寸和修改时间保持一致 |
| 最新独立产物 | `plugin-convergence/artifact`：隔离安装 wheel、独立 UI 20 步流程，14 张领域表，无宿主导入 |
| 旧实现与存储清理 | `plugin-convergence/cleanup.json`：原生 service/parser/okf 和 PD vendor Knowledge 页面删除，AST 导入检查及隔离 SD 建库通过；67 张宿主表无知识领域存储，仅保留业务发布 receipt |

Runtime 共 48 项显式映射，逐项验证声明、HTTP 路径/方法、参数及只读写拒绝。
31 项管理流程在真实模型运行中执行，建议审核/回读/导出在完整浏览器运行的
显式模型 fixture 中经 runtime port 执行。发现 fixture 不计入真实模型证据。
历史算法回归迁入 core；宿主仅测 DTO、权限映射、HTTP 兼容与业务发布。

SQLite 回放在双方隔离库按相同名称顺序重建显式索引，配合有序 ID 与固定
PYTHONHASHSEED 消除随机索引创建顺序导致的查询计划差异；没有修改历史
实现或 fixture，数组顺序仍递归比较。历史基线不恢复到当前宿主。

调试中 Kimi 的 429 并发限制和 gateway 退出超时均曾阻止验收。
最终真实模型使用现有 DeepSeek 配置；修复 gateway 主动关闭 WebSocket 后
PD/gateway 返回 0，KM 正常处理 SIGTERM，无强制退出。
这些失败不作为通过证据。未提交、推送或切换生产数据/模型配置。

```sh
backend/.venv/bin/python knowledge_core/scripts/convergence_gate.py --evidence-dir knowledge_core/acceptance/2026-10-14/plugin-convergence
```

以下为上一阶段记录，仅说明独立服务、SD hosted model 和当时已验收流程。

2026-10-14：本轮剩余开发验收四项通过。上一轮完成结论仍撤回；当前结论只依据本轮重新执行的证据。缺失、跳过、失败、终态不符或证据文件不存在均不能通过统一验收。

两个工作区均保留在 `standalone_alpha1_decouple_km_1013`：

- StaffDeck：`/Users/a1/.codex/worktrees/standalone-alpha1-20261013/StaffDeck`
- PilotDeck：`/Users/a1/.codex/worktrees/standalone-alpha1-decouple-km-1013/PilotDeck`

稳定证据根目录为 [acceptance/2026-10-14](acceptance/2026-10-14/)。当前结论由 [acceptance.json](acceptance/2026-10-14/acceptance.json) 生成；复验命令见 [commands.md](acceptance/2026-10-14/commands.md)。

| 门槛 | 结果 | 本轮证据 |
| --- | --- | --- |
| 完整 lifespan 浏览器 | 通过 | [full-browser/report.json](acceptance/2026-10-14/full-browser/report.json)：SD 完整 `app.main:app`、Harness 和后台生命周期；SD/PD 同一隔离运行完成主流程、Bucket/Chunk 回读、版本、OKF、取消、审核/拒绝，以及 SD 实际 Skill/Tool 和发布记录创建 |
| 递归兼容矩阵 | 通过 | [compatibility/matrix.json](acceptance/2026-10-14/compatibility/matrix.json)：189 案例、零字段差异、无必需项缺失；主基线 `77c219737a9e8e24d6df39ba73212c60f5851b90`，当前 853 次独立 KM HTTP 调用 |
| 真实 hosted model | 通过 | [hosted-model-final/report.json](acceptance/2026-10-14/hosted-model-final/report.json)：实际 `provider1/deepseek-v4-flash-0731`；上传/查询携带所选模型；四次宿主/KM 关联调用返回 buckets、discoveries、selected_document_ids、selected_bucket_ids，检索返回证据且无词法回退 |
| core 与最新产物 | 通过 | [regression/report.json](acceptance/2026-10-14/regression/report.json)：core 32 passed、兼容 107 passed，无跳过；[independent-final/report.json](acceptance/2026-10-14/independent-final/report.json)：最新 wheel 在无宿主源码/PYTHONPATH/服务的临时安装环境完成浏览器全流程 |

## 兼容范围与规范化

覆盖旧 REST 知识库、文档、任务、Bucket/Chunk、OKF、发现建议和版本操作；公开 HTTP 与 Python SDK 全部 13 方法；模块 manifest 全部 32 操作。逐案例保留基线、请求、预期、实际与字段路径差异，递归检查存在性、类型、值、null、数组顺序、错误详情和模块 envelope。

历史版和当前版使用相同输入与显式解析/模型/发布 fixture。当前宿主只经 HTTP 调用独立 KM。任务回读必须到预期的 succeeded、failed 或 cancelled；知识上传任务使用 `/agents/{agent_id}/knowledge-jobs/{id}`，通用任务接口的 404 不算终态。

生成 ID 使用双向一致映射。验收组合根在两侧注入有序 ID，消除历史 SQLite 未排序扫描受随机主键影响的问题，不修改历史业务实现或响应 fixture。只规范化指定时间字段；ZIP 比较文件内容与 Markdown frontmatter，保留正文、引用关联和数组顺序。

错误案例覆盖认证、权限、跨空间、缺失、无效输入、并发编辑、未知版本、解析失败、能力端口失败和重复发布。历史公开接口缺失必需字段返回 500、未知版本回滚返回 200 并建立版本的行为按基线保留。公开 API/SDK 历史上没有发现发布方法，报告明确列为不适用，另验证入库幂等性。`e39ec532` 仅为 SDK fixture 补充来源。

## 产物与生命周期

独立 [wheel](acceptance/2026-10-14/artifacts/knowledge_core-0.1.0-py3-none-any.whl) 包含最新 UI。零身份配置下创建、入库、编辑、查询、引用、Wiki、版本、隐藏/恢复、关系图、OKF 和桌面/移动端通过。数据库仅 14 张 `knowledge_*` 表；实际服务关闭时 [runtime-imports.json](acceptance/2026-10-14/independent-final/runtime-imports.json) 为空，没有用户、租户、员工或其他宿主业务表。

启动日志、请求、截图与退出记录均保留。SD/KM 有 `Application shutdown complete`，没有强制 SIGKILL。uvicorn 正常处理 SIGTERM 后以信号退出，Vite 为 143；PD supervisor 的 SIGTERM 路径返回 1。原始退出值保留，没有伪装为零。

发现建议生成采用显式 fixture；实际 SD 发布适配器在隔离宿主库创建 Skill、Tool 和 receipt。真实模型证据另来自 hosted-model-final，fixture 与词法检索不计入模型门槛。

## 通过、失败与未执行

- 当前必需项全部通过：矩阵无差异，回归无失败/跳过，浏览器无必需流程缺失。
- 空间参数丢失、发现排序、概念刷新、模型选择刷新丢失及 UI 原始 JSON 展示问题已修复并重跑。旧失败与代表性快照不作为当前结论。
- 实施前 25 passed 证据保留在 [baseline.json](acceptance/2026-10-14/baseline.json)。新增递归比较、模型选择和拒绝不完整验收的测试后，core 为 32 passed。
- 未执行提交、推送、生产数据切换和生产模型配置切换。模型验收通过现有凭据解析器使用临时知识数据，凭据不写入报告。

```sh
backend/.venv/bin/python knowledge_core/scripts/acceptance_gate.py --evidence-dir knowledge_core/acceptance/2026-10-14
```

仅四项通过才返回 0 和 `status=complete`。
