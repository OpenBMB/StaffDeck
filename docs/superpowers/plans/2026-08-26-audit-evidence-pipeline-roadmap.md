# 审核证据流水线总路线图 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 分四个可独立发布的阶段，把 StaffDeck 审核报告流程改造成可复用材料、可验证全文覆盖、可追溯知识依据并支持混合检索的证据流水线。

**Architecture:** 先修复 Harness 长文本分页和同 TaskFrame 复用，再建立审核项目材料边界；之后增加证据台账、覆盖 Gate 和分章节生成，最后在统一检索接口后接入 BM25、向量召回、reranker 与经连通性验证、管理员确认的模型预算。每个阶段保持普通聊天和普通 `knowledge_search` 的现有行为。

**Tech Stack:** Python 3.11、FastAPI、SQLModel/SQLite、Pydantic v2、pytest、React 18、TypeScript、Vitest、OpenAI-compatible APIs

**Spec:** `docs/superpowers/specs/2026-08-26-audit-material-evidence-pipeline-design.md`

## Global Constraints

- 同一审核项目中的同一份材料只需上传一次；不得跨租户、跨企业或跨审核项目自动共享。
- “全文覆盖”表示每份当前材料提取成功、字符区间连续分块、每块处理完成并进入可追踪台账，不承诺模型语义上使用每一个字。
- 项目材料不得自动写入企业长期知识库；稳定知识继续使用固定的知识库版本。
- 普通聊天继续保留每个 TaskFrame 最多两次成功 `knowledge_search` 的预算。
- 默认模型安全输入预算保持 `32_000` tokens；只有协议探针成功且管理员依据提供方文档确认上下文窗口后才能提高。
- 材料目标分块大小为 `900` 至 `1,200` 字符，字符区间联合必须精确覆盖 `[0, characters)`。
- 报告发布前，文件覆盖率、文本块覆盖率和必需要素状态覆盖率必须全部为 `100%`。
- 所有项目、材料、块、证据和报告查询必须包含 `tenant_id`；普通日志不得记录全文或 API Key。
- 数据库迁移只新增表和可空引用；历史附件不自动归属审核项目。
- 新功能使用租户功能开关灰度，先在本地 `tenant_demo` 验收。

---

## 阶段依赖图

```text
Phase 1 Harness 可靠性
  -> Phase 2 审核项目材料库
       -> Phase 3 证据台账与分章节报告
            -> Phase 4 混合检索、重排与模型预算
```

## 计划索引

| 顺序 | 计划 | 可独立发布的结果 | 发布门槛 |
|---|---|---|---|
| 1 | `2026-08-26-audit-evidence-phase-1-harness-reliability.md` | 长文本分页 token 不再被裁断；同一 TaskFrame 不重复要求已有附件 | Harness 定向测试与完整后端测试通过 |
| 2 | `2026-08-26-audit-evidence-phase-2-case-material-store.md` | 新对话选择同一审核项目可复用材料；版本、权限和处理状态可见 | 跨会话复用、跨项目隔离、前端构建通过 |
| 3 | `2026-08-26-audit-evidence-phase-3-ledger-reporting.md` | 全文块台账、审核要素覆盖 Gate、断点续写和可追溯报告 | 固定 40,000 字样本端到端验收通过 |
| 4 | `2026-08-26-audit-evidence-phase-4-hybrid-retrieval.md` | BM25 + 向量召回 + reranker；按审核要素批量检索 | 固定检索集召回指标不低于旧实现且引用有效 |

### Task 1: 建立每阶段基线与功能开关

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/.env.example`
- Create: `backend/tests/test_config.py`

**Interfaces:**
- Consumes: 现有 `Settings` 环境变量加载机制。
- Produces: `audit_case_enabled: bool`、`audit_evidence_pipeline_enabled: bool`、`hybrid_knowledge_retrieval_enabled: bool`。

- [ ] **Step 1: 为三个开关编写默认关闭测试**

```python
def test_audit_pipeline_feature_flags_default_to_false() -> None:
    settings = Settings()
    assert settings.audit_case_enabled is False
    assert settings.audit_evidence_pipeline_enabled is False
    assert settings.hybrid_knowledge_retrieval_enabled is False
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_config.py::test_audit_pipeline_feature_flags_default_to_false -v`

Expected: FAIL，`Settings` 尚无三个字段。

- [ ] **Step 3: 在 `Settings` 中增加明确默认值**

```python
audit_case_enabled: bool = False
audit_evidence_pipeline_enabled: bool = False
hybrid_knowledge_retrieval_enabled: bool = False
```

`backend/.env.example` 同步加入 `AUDIT_CASE_ENABLED=false`、`AUDIT_EVIDENCE_PIPELINE_ENABLED=false` 和 `HYBRID_KNOWLEDGE_RETRIEVAL_ENABLED=false`，避免首次部署意外启用迁移后的新路径。

- [ ] **Step 4: 运行定向测试与配置测试文件**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_config.py -v`

Expected: PASS。

- [ ] **Step 5: 提交功能开关**

```powershell
git add backend/app/config.py backend/.env.example backend/tests/test_config.py
git commit -m "feat: add audit pipeline feature flags"
```

### Task 2: 按顺序执行阶段计划

**Files:**
- Read: `docs/superpowers/plans/2026-08-26-audit-evidence-phase-1-harness-reliability.md`
- Read: `docs/superpowers/plans/2026-08-26-audit-evidence-phase-2-case-material-store.md`
- Read: `docs/superpowers/plans/2026-08-26-audit-evidence-phase-3-ledger-reporting.md`
- Read: `docs/superpowers/plans/2026-08-26-audit-evidence-phase-4-hybrid-retrieval.md`

**Interfaces:**
- Consumes: 前一阶段已通过的接口与迁移。
- Produces: 下一阶段计划中 `Consumes` 所列的稳定接口。

- [ ] **Step 1: 执行 Phase 1 并在 `tenant_demo` 打开基础回归场景**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_harness_filesystem.py tests/test_harness_v2.py -v`

Expected: PASS，且 40,000 字文本可以逐页读至 `eof=true`。

- [ ] **Step 2: 执行 Phase 2 并验证跨对话复用**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_audit_cases.py tests/test_audit_case_api.py tests/test_audit_case_migration.py -v`

Expected: PASS，同一项目新会话材料清单不为空，不同项目返回 404。

- [ ] **Step 3: 执行 Phase 3 并验证覆盖 Gate**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_audit_evidence.py tests/test_audit_reporting.py tests/test_audit_report_e2e.py -v`

Expected: PASS，缺少任一必需材料或失败块时发布被阻止。

- [ ] **Step 4: 执行 Phase 4 并验证检索回归**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest tests/test_hybrid_retrieval.py tests/test_audit_knowledge_orchestrator.py tests/test_model_configs_api.py -v`

Expected: PASS，固定查询集记录 BM25、vector、reranker 三段 trace。

### Task 3: 最终系统验收

**Files:**
- Create: `backend/tests/fixtures/audit_case_40k/manifest.json`
- Create: `backend/tests/test_audit_pipeline_acceptance.py`
- Modify: `README.zh.md`

**Interfaces:**
- Consumes: 四阶段所有公开 API。
- Produces: 可重复运行的验收样本与 Windows 操作说明。

- [ ] **Step 1: 固定验收断言**

```python
assert coverage.file_coverage == 1.0
assert coverage.chunk_coverage == 1.0
assert coverage.element_coverage == 1.0
assert coverage.publish_allowed is True
assert report.material_version_ids
assert report.knowledge_base_version_ids
```

- [ ] **Step 2: 运行后端完整测试**

Run: `cd backend; .\.venv\Scripts\python.exe -m pytest -q`

Expected: PASS。

- [ ] **Step 3: 运行后端静态检查**

Run: `cd backend; .\.venv\Scripts\python.exe -m ruff check app tests`

Expected: PASS，无新增告警。

- [ ] **Step 4: 运行前端测试和生产构建**

Run: `cd frontend-enterprise; npm test`

Expected: PASS。

Run: `cd frontend-enterprise; npm run build`

Expected: PASS。

- [ ] **Step 5: 使用 Windows 文档命令启动并验证**

Run: `.\scripts\dev_up.ps1 --detach`

Expected: `/api/health` 返回 `status=ok`，`/workspace/gallery` 可打开，审核项目选择器可见。

- [ ] **Step 6: 提交验收样本与文档**

```powershell
git add backend/tests/fixtures/audit_case_40k backend/tests/test_audit_pipeline_acceptance.py README.zh.md
git commit -m "test: add audit pipeline acceptance scenario"
```
