# Offline RapidDoc PDF ingestion implementation plan

## Goal

在 StaffDeck 内建立统一的、离线可运行的结构化 PDF 文档提取链路：对有文本层的 PDF 保留现有 `pypdf` 快速路径；对扫描型或混合型 PDF 自动切换到精简 CPU RapidDoc；在审核材料、知识库、聊天附件和 Harness `extract_document_text` 四个入口得到一致的全文、页级结果和可追溯元数据。原始 PDF 永不被覆盖，全文不因界面预览或单次模型上下文限制而丢失，审核报告只能使用已完成处理的材料和知识证据包。

## Architecture

采用“原文件不变、统一提取、全文落盘、页级分块、按需装载”的五层结构：

1. `DocumentExtractor` 是唯一业务入口。它先用 `pypdf` 按页探测文本层；所有页面都有有效文字时继续走 native 路径；存在无文字页、扫描页或混合页时，交给 RapidDoc 的 `doc_analyze`，由其按页处理 OCR、版面、阅读顺序和表格。
2. RapidDoc 通过延迟导入的 `RapidDocAdapter` 接入，模型目录由配置明确指定。正常服务进程禁止隐式下载模型；模型准备脚本单独执行并记录版本、目录和校验信息。默认仅启用 ONNX Runtime CPU 需要的组件，不引入 GPU、PyTorch、VLM、公式识别或图片导出链路。
3. 统一结果包含 `text`、`pages`、`page_refs`、`method`、`engine`、`warnings`、字符数、页数和源文件 SHA-256。审核材料和知识库保存完整结果；UI 的摘要和聊天消息只使用截断预览，不能反向覆盖全文。
4. 审核材料上传先落原文件并置为 `pending`，再执行提取、全文存储、分块和证据处理。接口刷新时依据数据库状态恢复，不要求用户重新上传。报告生成前检查文件覆盖率、分块覆盖率和知识库证据状态；存在 pending/failed 时明确阻止发布或标记为不可发布，而不是静默遗漏。
5. 如果隔离基准证明 RapidDoc 的 ORT-only 组合无法稳定运行或达不到验收门槛，仍保留相同 `DocumentExtractor` 合约，启用受控的 RapidOCR+RapidTable 兼容适配器；不自动引入 MinerU、OpenVINO、GPU 或云端 OCR。

## Tech Stack

- Python 3.11+、FastAPI、SQLModel、现有 `pypdf` parser、现有 `AsyncJobQueue`。
- RapidDoc 的 CPU/ONNX Runtime 路径；`RAPID_MODELS_DIR` 作为模型缓存目录。
- 现有审核材料 blob 存储、`AuditCaseMaterial`/`AuditCaseMaterialChunk`、知识库文档与分块模型。
- React + TypeScript + Vitest；沿用 `MaterialManager`、知识库页面和现有 API 错误呈现。
- PowerShell 命令在当前 Windows 主机执行；Linux/macOS/WSL 验证时使用仓库已有的 `scripts/dev_up.sh --detach`，Windows 使用 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev_up.ps1 --detach`。

## Spec

依据已冻结规格 [docs/superpowers/specs/2026-08-27-offline-rapiddoc-pdf-ingestion-design.md](../specs/2026-08-27-offline-rapiddoc-pdf-ingestion-design.md) 实施。验收目标为：原生 PDF 行为不回归；扫描 PDF 每页都有可追溯输出；模型准备完成后断网仍可处理；稳定模型目录占用不超过 2 GB；峰值内存不超过 4 GB；测试 PC 上 20 页约 300 DPI 扫描 PDF 在 3 分钟内完成；清晰中英文印刷体字符准确率目标约 98%；关键审核表格行列映射目标不低于 90%。这些性能和准确率是上线门槛，不在没有真实材料基准的情况下预先宣称已经达到。

## Global Constraints

- 先测试后实现；每个实现任务都必须先看到失败测试，再写最小实现，再运行同一测试变绿。
- 不修改、重编码、替换或删除用户原始上传文件；提取失败也保留原文件和错误码。
- 核心提取服务不设置 24,000 字或 30 页的静默截断；限制只允许出现在 UI 预览、单次 Harness 输出或模型上下文编排层，并且必须返回 continuation/coverage 信息。
- 普通运行不访问外网下载模型；只有显式模型准备命令允许联网。缺少模型、缺少包、加密 PDF、损坏 PDF、OCR 超时和 OCR 空结果必须返回稳定错误码。
- OCR 不使用外部云 OCR，不接入 IntSig/CamScanner，不把 MinerU 加入运行时依赖。
- 不破坏当前知识库 lexical/BM25/hybrid/re-ranker 逻辑；OCR 只负责产生高质量、可追溯的文档文本和版面元数据。
- 继续保留现有 native PDF 快速路径，避免所有普通 PDF 都付出 OCR 成本。
- 现有工作区包含用户未提交的审核项目管理改动；实施中不得 reset、checkout、clean 或覆盖这些改动。新提交只包含本功能涉及的文件。
- 本计划引用的当前事实已经在仓库中核实：`backend/app/knowledge/parser.py` 使用 `pypdf`，`backend/app/session/attachments.py` 存在独立 PDF 提取逻辑，`backend/app/harness/filesystem.py` 提供 `extract_document_text`，审核材料模型已有 extraction/processing 状态和分块表。

## Required sub-skill: `superpowers:test-driven-development`

每个任务按 Red → Green → Refactor 执行；实现完成后必须使用 `superpowers:verification-before-completion` 的证据标准验证，不能只依据“服务启动”报告完成。

## Implementation tasks

### Task 0: 建立 RapidDoc 隔离基准和可行性门槛

Files:

- Create `scripts/benchmark_rapiddoc.py`
- Create `backend/tests/test_rapiddoc_benchmark.py`
- Create `docs/superpowers/benchmarks/README.md`

Tests first:

1. 在 `backend/tests/test_rapiddoc_benchmark.py` 先定义并测试基准 JSON 的 schema：必须包含 `engine`、`package_bytes`、`model_bytes`、`peak_rss_bytes`、`elapsed_seconds`、`page_count`、`non_empty_pages`、`text_chars`、`table_count`、`offline_replay` 和 `warnings`；缺字段、负数资源值和页数不一致均失败。
2. 增加“输入文件不在仓库中”的测试约束，避免真实审核材料被脚本默认纳入提交或测试夹具。
3. 先运行：

   `Set-Location .\backend; .\.venv\Scripts\python.exe -m pytest tests\test_rapiddoc_benchmark.py -q`

   确认测试先失败，再实现 schema 校验和命令行报告。

Implementation:

1. `benchmark_rapiddoc.py` 接收显式 `--pdf`、`--output`、`--model-dir` 和 `--offline-replay` 参数，只读用户提供的本地 PDF，不写入仓库源目录。
2. 脚本在隔离 probe venv 中测量：RapidDoc/ORT 包安装体积、模型目录体积、峰值 RSS、每页耗时、空页数量、字符数、表格数量、OCR warning 和断网重放结果。对同一输入保存页数、字符数、页 SHA-256 与表格计数，作为离线回放判定依据。
3. 使用当前平台对应的 Python 3.11 命令创建隔离环境：Windows 使用 `py -3.11 -m venv .rapiddoc-probe`；macOS/Linux/WSL 使用 `python3.11 -m venv .rapiddoc-probe`。仅在 probe 环境安装 RapidDoc 目标依赖，禁止把测量用缓存复制进正式环境。
4. 正式决策写入 `docs/superpowers/benchmarks/README.md`：记录测试机、RapidDoc 版本、Python 版本、依赖包尺寸、模型尺寸、实测耗时/内存和输入 PDF 类型。若 ORT-only 不能运行或不满足规格，则在同一适配器合约下启用 RapidOCR+RapidTable 兼容路径，并记录原因；不绕过门槛直接上线。

Verification and commit:

- `Set-Location .\backend; .\.venv\Scripts\python.exe -m pytest tests\test_rapiddoc_benchmark.py -q`
- `Set-Location ..\frontend-enterprise; npm test -- --run`
- Commit: `test: add isolated RapidDoc benchmark gate`

### Task 1: 建立统一文档提取合约、配置和模型生命周期

Files:

- Modify `backend/app/config.py`
- Modify `backend/pyproject.toml`
- Modify `backend/.env.example`
- Create `backend/app/documents/__init__.py`
- Create `backend/app/documents/extraction.py`
- Create `backend/app/documents/rapiddoc_adapter.py`
- Create `backend/app/documents/model_manager.py`
- Create `scripts/prepare_rapiddoc_models.py`
- Create `backend/tests/test_document_extraction.py`
- Create `backend/tests/test_rapiddoc_adapter.py`

Tests first:

1. 在 `test_document_extraction.py` 测试 `DocumentExtractor` 合约：有文本层的 PDF 返回 `method=native`；扫描 PDF 调用注入的 structured adapter；普通文本/Markdown/DOCX 继续走原有 parser；结果含全文、页号、字符数和源 SHA-256。
2. 测试扫描 PDF 的 adapter 缺失、模型目录缺失、无 OCR 结果、超时、损坏 PDF 和加密 PDF，分别返回稳定错误码而不是裸 traceback。
3. 测试核心结果不截断：输入超过 24,000 字时 `text` 和 page payload 长度完整，只有 `preview` helper 截断。
4. 在 `test_rapiddoc_adapter.py` 使用 fake RapidDoc module 测试 `doc_analyze` 参数：CPU/ORT、`table_enable=True`、阅读顺序开启、公式/图片/checkbox 默认关闭、模型目录来自 `RAPID_MODELS_DIR`；缺少包时不得在 import 阶段让整个 FastAPI 应用崩溃。
5. 先运行：

   `Set-Location .\backend; .\.venv\Scripts\python.exe -m pytest tests\test_document_extraction.py tests\test_rapiddoc_adapter.py -q`

   确认失败后再实现。

Implementation:

1. `extraction.py` 定义 `ExtractedPage`、`DocumentExtractionResult`、`DocumentExtractionError` 和 `DocumentExtractor`。结果至少提供：`text`、`pages`、`page_refs`、`source_sha256`、`source_page_count`、`method`、`engine`、`engine_version`、`warnings`、`char_count`、`table_count`。
2. native probe 按页调用现有 `pypdf`，只有每个页面都有非空正文时才直接返回 native 结果；只要出现扫描页/空页，就把完整原 PDF 交给 structured adapter，避免把混合 PDF 的 OCR 页面遗漏掉。
3. `rapiddoc_adapter.py` 使用 lazy import 和显式参数映射，统一把 RapidDoc 的页面、段落、表格和阅读顺序归一成 StaffDeck schema。任何异常映射到 `DOCUMENT_EXTRACTION_FAILED`、`OCR_DEPENDENCY_MISSING`、`OCR_MODEL_MISSING`、`OCR_TIMEOUT`、`PDF_ENCRYPTED` 或 `PDF_CORRUPTED`。
4. `model_manager.py` 只负责模型目录检查、版本/校验清单和 offline readiness；不在 API 请求期间下载。`prepare_rapiddoc_models.py` 才执行显式预取，并支持 `--check-only` 和 `--model-dir`。
5. 配置加入结构化 PDF 开关、引擎、模型目录、最大页数/像素、单文件超时、worker 数和是否允许 OCR fallback；默认保持 native 行为，正式启用前必须完成 Task 0 基准。
6. `pyproject.toml` 只加入经 Task 0 验证的最小 CPU 依赖，并使用可选 extras 或 lock 约束隔离 OCR 依赖，避免默认安装拉入 GPU/PyTorch/VLM；`.env.example` 提供明确的模型目录与 offline 配置说明。

Verification and commit:

- `Set-Location .\backend; .\.venv\Scripts\python.exe -m pytest tests\test_document_extraction.py tests\test_rapiddoc_adapter.py -q`
- `Set-Location .\backend; .\.venv\Scripts\python.exe -m ruff check app\documents app\config.py tests\test_document_extraction.py tests\test_rapiddoc_adapter.py`
- `Set-Location .\backend; .\.venv\Scripts\python.exe scripts\prepare_rapiddoc_models.py --check-only`
- Commit: `feat: add unified offline structured document extraction`

### Task 2: 将知识库摄取接入统一提取服务

Files:

- Modify `backend/app/knowledge/parser.py`
- Modify `backend/app/knowledge/service.py`
- Modify `backend/app/knowledge/schema.py`
- Modify `backend/app/knowledge/okf.py` only where source metadata is serialized
- Modify `backend/tests/test_knowledge_base.py`
- Create `backend/tests/test_knowledge_document_extraction.py`

Tests first:

1. 测试带文本层 PDF 的知识库上传仍产生与现有 native parser 等价的正文和文件类型。
2. 测试扫描 PDF 通过注入的 `DocumentExtractor` 进入知识库 ingestion，保存完整正文、页引用、OCR engine/method 和 warning；不能因为 preview 限制而截断存储文本。
3. 测试 OCR 失败时文档 job 为失败且保存稳定错误信息，原始 blob 仍存在；重试可以重新提取，不创建重复文档或重复分块。
4. 测试同一 PDF 的内容 SHA-256、提取文本 SHA-256 和版本元数据写入文档来源 metadata，后续 BM25/vector/reranker 仍从同一规范化文本读取。
5. 先运行：

   `Set-Location .\backend; .\.venv\Scripts\python.exe -m pytest tests\test_knowledge_base.py tests\test_knowledge_document_extraction.py -q`

   确认新测试失败后实现。

Implementation:

1. 保留 `extract_text(filename, content)` 兼容接口，但内部委托 `DocumentExtractor`；需要页级数据的摄取任务改用完整 `DocumentExtractionResult`。
2. `KnowledgeService` 将 extraction metadata 与文档状态一起持久化；正文用于现有 section/bucket/chunk pipeline，page refs 贯穿到 chunk citation，不能仅存一段无法定位页码的长字符串。
3. 失败 job 不删除原文、不清空旧版本；相同源 SHA-256 重试时复用当前文档版本或显式新版本，保证向量索引和 lexical 索引不会出现幽灵副本。
4. 知识检索仍由现有 BM25、hybrid 和 reranker 负责；本任务只修改输入文档质量和 provenance，不改变已存在的排序权重。

Verification and commit:

- `Set-Location .\backend; .\.venv\Scripts\python.exe -m pytest tests\test_knowledge_base.py tests\test_knowledge_document_extraction.py -q`
- `Set-Location .\backend; .\.venv\Scripts\python.exe -m ruff check app\knowledge tests\test_knowledge_document_extraction.py`
- Commit: `feat: route knowledge ingestion through structured PDF extraction`

### Task 3: 将审核材料改为持久化、可重试的 OCR/分块流水线

Files:

- Modify `backend/app/db/models.py`
- Modify `backend/app/audit_cases/schema.py`
- Modify `backend/app/audit_cases/service.py`
- Modify `backend/app/audit_cases/chunking.py`
- Modify `backend/app/audit_cases/evidence.py`
- Modify `backend/app/audit_cases/coverage.py`
- Modify `backend/app/api/audit_cases.py`
- Modify `backend/app/async_jobs.py`
- Modify `backend/app/main.py` only for worker registration/startup wiring
- Modify `backend/tests/test_audit_cases.py`
- Modify `backend/tests/test_audit_case_api.py`
- Modify `backend/tests/test_audit_evidence.py`
- Create `backend/tests/test_audit_material_ocr_pipeline.py`

Tests first:

1. 测试上传接口只持久化一次原始 bytes，返回 material `pending`，不要求 HTTP 请求等待 OCR 完成；测试 worker 完成后状态变成 `succeeded` 并可通过现有详情/coverage 接口读取。
2. 测试扫描 PDF 生成完整 `extracted.txt`、页级 chunk、`page_refs_json`、字符数、页数、提取方法、engine 和 warning；验证首尾哨兵文本均存在，证明没有 24,000 字截断。
3. 测试 OCR 失败、模型缺失和超时的状态转换：`extraction_status=failed`、`processing_status=failed`、原文件仍可下载、retry 只重置当前版本状态并重新排队。
4. 测试报告生成/coverage 在任一当前材料 pending/failed 或任一 chunk 未处理时不能宣称 `publish_allowed=True`，并返回缺失材料清单；全部完成后保持已有报告 e2e 行为。
5. 测试重复请求幂等：同一个 material/job 不重复生成分块，worker 重启后依据数据库状态可恢复；旧版本材料不污染当前版本。
6. 先运行：

   `Set-Location .\backend; .\.venv\Scripts\python.exe -m pytest tests\test_audit_material_ocr_pipeline.py tests\test_audit_cases.py tests\test_audit_case_api.py tests\test_audit_evidence.py -q`

   确认失败后再实现。

Implementation:

1. 在 `AuditCaseMaterial` 增加经过迁移安全处理的提取元数据字段：页数、提取方法、engine/version、warning JSON、提取文本 SHA-256 和 job id；读取旧数据库时按默认值兼容。
2. 把 `process_material` 拆为可重入 worker 逻辑：读取原 blob → `DocumentExtractor` → 原子写完整 extracted text → 删除当前 material 的旧 chunks → 按页/段落 chunk → 提交状态与事件。失败时不删除原 blob 或旧的可追溯错误信息。
3. 使用现有 `AsyncJobQueue` 处理 OCR/分块；请求线程只完成上传和排队。若当前队列是进程内队列，则数据库状态必须是事实来源，服务重启后由 recovery scan 重新排队 pending/processing 项。
4. `audit_cases.py` 保留当前 API 兼容的详情、retry、coverage 和报告入口，但不再在报告请求中同步偷偷处理所有材料；报告入口先读取 coverage，未就绪则返回明确的 409/202 语义和材料状态。
5. `chunking.py` 为每个 chunk 保存页范围和字符范围；`evidence.py` 处理全部成功 chunk，使用分页/批量读取避免把四万字一次性塞进单个 prompt。`coverage.py` 将“文件完整、分块完整、知识证据状态”作为报告可发布门槛。
6. 保留现有的 `PDF_TEXT_LAYER_MISSING` 前端可读错误用于 OCR 未启用/未准备的降级提示，但 OCR 启用且失败时展示更具体的错误码与重试入口。

Verification and commit:

- `Set-Location .\backend; .\.venv\Scripts\python.exe -m pytest tests\test_audit_material_ocr_pipeline.py tests\test_audit_cases.py tests\test_audit_case_api.py tests\test_audit_evidence.py -q`
- `Set-Location .\backend; .\.venv\Scripts\python.exe -m ruff check app\audit_cases app\api\audit_cases.py app\async_jobs.py tests\test_audit_material_ocr_pipeline.py`
- Commit: `feat: make audit material OCR processing durable and retryable`

### Task 4: 覆盖聊天附件和 Harness 文档工具入口

Files:

- Modify `backend/app/session/attachments.py`
- Modify `backend/app/session/session_schema.py`
- Modify `backend/app/session/attachment_store.py` only for derived-text metadata if required
- Modify `backend/app/harness/filesystem.py`
- Modify `backend/app/api/chat.py` only for response/status serialization if required
- Modify `backend/tests/test_chat_attachments.py`
- Modify `backend/tests/test_harness_v2.py`
- Create `backend/tests/test_shared_pdf_entrypoints.py`

Tests first:

1. 测试聊天上传扫描 PDF 时不再直接调用私有 `_pdf_attachment` 的 pypdf 逻辑，而是调用统一 extractor；完整正文保留在服务端可读取位置，`ChatAttachmentRead.text` 只作为受限预览或摘要，不作为全文存储。
2. 测试 30 页以上和超过 24,000 字 PDF：附件元数据必须声明完整页数/全文可读取，预览可以截断但必须说明如何通过 sandbox path/`extract_document_text` 分页读取。
3. 测试 Harness `extract_document_text` 对同一 PDF 与知识库/审核材料得到相同 method、page count、非空页数和首尾哨兵文本；读取输出仍遵守现有 continuation token，而不制造 `INVALID_CONTINUATION`。
4. 测试缺少 OCR 依赖/模型时聊天响应为可读错误，不让整个聊天请求崩溃；原附件仍可由授权的 typed file path 读取。
5. 先运行：

   `Set-Location .\backend; .\.venv\Scripts\python.exe -m pytest tests\test_shared_pdf_entrypoints.py tests\test_chat_attachments.py tests\test_harness_v2.py -q`

   确认失败后实现。

Implementation:

1. 删除重复的 PDF 解析分支，`parse_chat_attachment` 依赖统一 extractor；保留图片 data URL 与纯文本附件现有行为。
2. `ChatAttachmentRead` 增加可选 extraction metadata 或引用对象，不把完整 OCR 文本塞入每轮模型请求；模型需要正文时通过 Harness 分页读取统一生成的 UTF-8 文本。
3. `attachment_store.py` 保存原 payload 和派生提取文本的完整性信息，校验 tenant/user/attachment id，不暴露宿主机绝对路径。
4. Harness 输出包含 `page_count`、`extraction_method`、`warnings` 和 `continuation_token`；上下文投影层只做展示截断，保留完整文件可重读性。

Verification and commit:

- `Set-Location .\backend; .\.venv\Scripts\python.exe -m pytest tests\test_shared_pdf_entrypoints.py tests\test_chat_attachments.py tests\test_harness_v2.py -q`
- `Set-Location .\backend; .\.venv\Scripts\python.exe -m ruff check app\session app\harness tests\test_shared_pdf_entrypoints.py`
- Commit: `feat: unify chat and harness PDF extraction`

### Task 5: 完善审核材料和知识库的前端状态呈现

Files:

- Modify `frontend-enterprise/src/pages/audit-cases/auditCaseApi.ts`
- Modify `frontend-enterprise/src/pages/audit-cases/auditCaseTypes.ts`
- Modify `frontend-enterprise/src/pages/audit-cases/components/MaterialManager.tsx`
- Modify `frontend-enterprise/src/pages/audit-cases/components/CoveragePanel.tsx`
- Modify `frontend-enterprise/src/pages/audit-cases/auditCaseErrors.ts`
- Modify `frontend-enterprise/src/pages/KnowledgePage.tsx` only for ingestion status if the page owns upload status
- Modify `frontend-enterprise/src/pages/audit-cases/components/MaterialManager.test.tsx`
- Modify `frontend-enterprise/src/pages/audit-cases/AuditCaseDetailPage.test.tsx`
- Modify `frontend-enterprise/src/pages/KnowledgePage.retrieval.test.tsx` only where ingestion metadata is displayed

Tests first:

1. 测试 `pending`、`processing`、`succeeded`、`failed` 四种状态的文案、颜色和重试按钮；失败项显示错误码对应的处理建议，成功项显示页数、字符数和 OCR/native 方法。
2. 测试上传后列表自动刷新或轮询状态，不因短暂 pending 要求用户重新上传；报告按钮在 coverage 未达到发布门槛时显示缺失原因。
3. 测试完整文本不在卡片中渲染，卡片只显示摘要；用户可以打开详情/下载原文/分页查看派生文本。
4. 先运行：

   `Set-Location .\frontend-enterprise; npm test -- --run src/pages/audit-cases/components/MaterialManager.test.tsx src/pages/audit-cases/AuditCaseDetailPage.test.tsx src/pages/KnowledgePage.retrieval.test.tsx`

   确认失败后实现。

Implementation:

1. 将后端状态和 metadata 类型化，避免前端用“有无 text”猜测 OCR 是否完成。
2. 在 `MaterialManager` 添加 OCR 处理中的说明、失败原因、重试和“原件保留”提示；不改变用户已经确认的审核材料分类结构。
3. 在 `CoveragePanel` 展示文件覆盖率、分块覆盖率、页覆盖率和知识库证据状态；未满足时禁用或明确阻止生成可发布报告。
4. 不在前端拼接四万字全文，不把全文放进 React state 的单个可见卡片；详情读取使用已有分页接口或后端生成的文本资源。

Verification and commit:

- `Set-Location .\frontend-enterprise; npm test -- --run src/pages/audit-cases/components/MaterialManager.test.tsx src/pages/audit-cases/AuditCaseDetailPage.test.tsx src/pages/KnowledgePage.retrieval.test.tsx`
- `Set-Location .\frontend-enterprise; npm run build`
- Commit: `feat: show durable PDF extraction status in enterprise UI`

### Task 6: 模型准备、启动脚本和操作文档

Files:

- Modify `scripts/dev.py`
- Modify `scripts/dev_supervisor.py` only for health/status visibility if needed
- Modify `scripts/dev_up.ps1` only if startup must validate prepared OCR models
- Modify `scripts/dev_up.sh` only if startup must validate prepared OCR models
- Modify `README.md`
- Modify `README.zh.md`
- Modify `backend/.env.example`
- Create `docs/rapiddoc-offline-operations.md`
- Modify `backend/tests/test_dev_scripts.py`

Tests first:

1. 测试开发启动默认不下载模型，不因机器尚未启用 OCR 而破坏普通文本层 PDF 启动；开启 `STRUCTURED_PDF_ENABLED=true` 且模型缺失时，健康/状态输出说明“需先 prepare”，而不是静默失败。
2. 测试 `--check-only` 返回模型目录、版本和缺失文件；测试 Windows 启动命令和 macOS/Linux/WSL 启动命令在文档中分别出现，不混用 shell 语法。
3. 先运行：

   `Set-Location .\backend; .\.venv\Scripts\python.exe -m pytest tests\test_dev_scripts.py -q`

   确认新测试失败后实现。

Implementation:

1. 编写明确的两阶段操作：先运行 `scripts/prepare_rapiddoc_models.py` 检查/准备模型，再运行开发服务器；普通 `dev_up` 只检查配置和服务，不执行隐式网络下载。
2. 在文档中分别给出 Windows PowerShell 和 macOS/Linux/WSL 命令：模型准备、启动、状态、停止、`/api/health`、`/workspace/gallery` 和离线回放检查。PowerShell 说明执行策略受限时使用仓库已验证的 `-ExecutionPolicy Bypass` 调用方式。
3. 健康状态中增加结构化提取 readiness、当前 engine、模型目录存在性和 OCR worker 状态；不把 API Key 或模型路径中的敏感信息写入日志。

Verification and commit:

- `Set-Location .\backend; .\.venv\Scripts\python.exe -m pytest tests\test_dev_scripts.py -q`
- `Set-Location .\backend; .\.venv\Scripts\python.exe -m ruff check app scripts tests\test_dev_scripts.py`
- `Set-Location .\frontend-enterprise; npm run build`
- Commit: `docs: document offline RapidDoc preparation and startup`

### Task 7: 真实扫描材料验收、全量回归和运行验证

Files:

- Create `docs/superpowers/benchmarks/2026-08-27-staffdeck-scan-pdf-acceptance.md`
- Do not commit user-provided audit PDFs, extracted full text, OCR model files, API keys or generated reports.

Tests and acceptance sequence:

1. 准备一组不提交到仓库的脱敏真实样本：纯文本 PDF、扫描型中文审核表、扫描型英文页、混合 PDF、表格密集 PDF、损坏/加密 PDF。至少包含当前曾失败的“审核组准备会记录”“首末次会议签到表”“受审核组织信息确认表”同类扫描材料。
2. 运行基准并把只含统计值、版本和错误摘要的结果写入 acceptance markdown；记录输入页数、首尾文本哨兵、空页数、表格计数、耗时、峰值内存和离线 replay。
3. 运行后端定向回归：

   `Set-Location .\backend; .\.venv\Scripts\python.exe -m pytest tests\test_document_extraction.py tests\test_rapiddoc_adapter.py tests\test_knowledge_base.py tests\test_audit_material_ocr_pipeline.py tests\test_audit_cases.py tests\test_audit_case_api.py tests\test_audit_evidence.py tests\test_chat_attachments.py tests\test_harness_v2.py -q`

4. 运行后端全量回归：

   `Set-Location .\backend; .\.venv\Scripts\python.exe -m pytest -q`

5. 运行前端全量测试和构建：

   `Set-Location .\frontend-enterprise; npm test -- --run`

   `npm run build`

6. 启动并验证当前 Windows 服务：

   `Set-Location ..; powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev_up.ps1 --detach`

   `Invoke-WebRequest -UseBasicParsing http://127.0.0.1:5173/api/health`

   `Invoke-WebRequest -UseBasicParsing http://127.0.0.1:5173/workspace/gallery`

   检查返回 HTTP 200，并确认健康状态中的 OCR readiness 与实际模型准备状态一致。

7. 用一份扫描 PDF 完成端到端流程：上传 → pending → OCR succeeded → 全文/页级分块 → coverage 100% → 知识库命中 → 审核报告生成。验证报告引用的页号和材料 id 可回到原始文件，且不要求重复上传。
8. 用一份故意失败的样本验证错误路径：界面显示稳定错误码、原文件仍在、retry 可重新处理、报告不会伪装成完整成功。

Final review and commit:

- 检查 `git diff --check`。
- 检查 `git status --short`，确认不包含模型、原始材料、API Key、数据库临时文件和旧任务未相关改动。
- 复核完整文本、页引用、coverage 和离线 replay 的证据文件。
- Commit: `test: validate offline scanned PDF ingestion end to end`

## Plan self-review checklist

- 每个任务都列出了具体文件，而不是只写“修改相关代码”。
- 每个任务都先列失败测试和运行命令，再列实现和变绿验证命令。
- RapidDoc 的包/模型体积没有被未经实测的数字替代；体积、内存、耗时和准确率均通过 Task 0/Task 7 实测。
- 四个 PDF 入口均被覆盖：知识库、审核材料、聊天附件、Harness 文档工具。
- 原始文件、完整 OCR 文本、预览文本、分块和模型上下文被明确分层，避免“预览截断等于数据丢失”。
- 现有 BM25、向量索引、reranker 和审核 coverage gate 被保留，OCR 只改变文档提取层。
- 计划没有依赖隐式网络下载、云 OCR、MinerU 或 GPU/PyTorch。
- 没有未落地标记、占位步骤或模糊的重复性步骤。
