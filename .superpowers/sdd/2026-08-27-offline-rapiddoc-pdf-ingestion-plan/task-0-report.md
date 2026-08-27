# Task 0 Report: RapidDoc 隔离基准和可行性门槛

## 修改文件

- `backend/tests/test_rapiddoc_benchmark.py`
- `scripts/benchmark_rapiddoc.py`
- `docs/superpowers/benchmarks/README.md`
- `.superpowers/sdd/2026-08-27-offline-rapiddoc-pdf-ingestion-plan/task-0-report.md`

## TDD Red

命令：

```powershell
Set-Location .\backend; .\.venv\Scripts\python.exe -m pytest tests\test_rapiddoc_benchmark.py -q
```

第一次 Red 结果里混入了当前执行环境的 `tmp_path` 目录权限问题：

```text
PermissionError: [WinError 5] 拒绝访问: 'C:\Users\Administrator\AppData\Local\Temp\pytest-of-Administrator'
```

该错误与需求无关，因此先把测试改为使用工作区可控临时目录，再重新执行同一条 Red 命令，得到与任务相关的失败证据：

```text
FFFFFFFFFFFFFFFFFFFFFFF
FileNotFoundError: [Errno 2] No such file or directory: '...\\work\\StaffDeck\\scripts\\benchmark_rapiddoc.py'
23 failed in 0.43s
```

这次 Red 证明：

- 目标脚本尚不存在。
- schema 校验、仓库外 PDF 约束和命令行报告行为都还未实现。

## Green / 回归 / 静态检查

### 1. 任务级 pytest

命令：

```powershell
Set-Location .\backend; .\.venv\Scripts\python.exe -m pytest tests\test_rapiddoc_benchmark.py -q
```

结果：

```text
.......................                                                  [100%]
23 passed in 2.25s
```

### 2. 静态检查

命令：

```powershell
Set-Location .\backend; .\.venv\Scripts\python.exe -m ruff check tests\test_rapiddoc_benchmark.py ..\scripts\benchmark_rapiddoc.py
```

结果：

```text
All checks passed!
```

### 3. 前端回归

命令：

```powershell
Set-Location ..\frontend-enterprise; npm test -- --run
```

结果：

```text
Test Files  55 passed (55)
Tests  240 passed (240)
Duration  57.94s
```

## 本次实现内容

- 新增 `backend/tests/test_rapiddoc_benchmark.py`，先以 TDD 固化 benchmark JSON 必填字段、负数资源值拒绝、页数/页哈希一致性约束，以及“输入 PDF 不得位于仓库内”的保护。
- 新增 `scripts/benchmark_rapiddoc.py`，提供显式 `--pdf`、`--output`、`--model-dir`、`--offline-replay` 参数，校验仓库外 PDF 输入，读取离线回放 JSON，计算模型目录和当前环境包目录大小，验证 schema 并写出报告。
- 新增 `docs/superpowers/benchmarks/README.md`，把 probe 约束、验收门槛和“哪些值必须实测、当前仍待补”写清楚。

## 是否实际运行了 RapidDoc probe

没有。

本次任务只建立了隔离基准的 schema、路径边界、报告脚本和门槛文档，没有在当前 turn 内：

- 创建 `.rapiddoc-probe`
- 联网安装 RapidDoc / ORT 目标依赖
- 使用真实仓库外 PDF 执行 RapidDoc 解析
- 产出实测的包体积、模型体积、峰值 RSS、耗时或断网重放结果

## 未完成部分和风险

1. `scripts/benchmark_rapiddoc.py` 当前是“报告固化器”，不是“RapidDoc 执行器”。
   - 它会验证和落盘离线回放结果，但不会自行驱动 RapidDoc 解析，也不会自行采集峰值 RSS。
2. `docs/superpowers/benchmarks/README.md` 中所有实测项仍是“待实际 probe 填写”。
   - 这符合“不把未实测数字写成事实”的约束，但也意味着 Task 0 的可行性结论还没有被真实数据闭环。
3. 由于仓库当前存在大量与审核项目管理相关的未提交改动，本次只运行了前端整体验证，没有改动这些既有文件。
4. `docs/` 目录被 `.gitignore` 忽略。
   - 如果要把 `docs/superpowers/benchmarks/README.md` 纳入本次提交，需要使用强制暂存。

## 2026-08-27 Fix Report Append

### Review findings restated

1. 旧实现没有真正创建或使用隔离 `.rapiddoc-probe`，也没有在依赖与模型可用时执行真实 probe。
2. 旧实现把 `offline_replay.matched` 硬编码为 `True`，且接受调用方提供的 replay 值作为“证明”，没有把 replay 与真实 PDF 和 fresh replay 结果重新绑定比对。

### 这轮 Red

命令：

```powershell
Set-Location .\backend; .\.venv\Scripts\python.exe -m pytest tests\test_rapiddoc_benchmark.py -q
```

第一次新增 orchestration / replay 约束后的失败结果：

```text
......................FFEEEFFFF
FAILED test_probe_creation_command_uses_platform_specific_python
FAILED test_compare_offline_replay_marks_mismatches
FAILED test_main_runs_live_probe_and_offline_replay_against_same_pdf
FAILED test_main_reports_offline_replay_mismatch_in_output
FAILED test_main_fails_clearly_when_model_dir_missing
ERROR  test_ensure_probe_environment_creates_probe_venv_when_missing
ERROR  test_execute_probe_collects_metrics_from_rapiddoc_module
ERROR  test_execute_probe_fails_clearly_when_dependency_missing
6 failed, 22 passed, 3 errors in 0.72s
```

说明：

- 失败项对应 review 指出的真实缺口：缺少 `.rapiddoc-probe` orchestration、缺少 live probe / fresh replay 双执行路径、缺少 replay 比对逻辑、缺少显式失败码。
- 其中 3 个 `tmp_path` 错误仍是当前执行环境的系统临时目录权限噪音，因此测试夹具随后改为工作区可控临时目录。

补上 internal-probe 参数约束测试后的第二次 Red：

```powershell
Set-Location .\backend; .\.venv\Scripts\python.exe -m pytest tests\test_rapiddoc_benchmark.py -q
```

```text
........................F.......
FAILED test_parse_args_allows_internal_probe_without_report_outputs
SystemExit: 2
__main__.py: error: the following arguments are required: --offline-replay
1 failed, 31 passed in 0.63s
```

这次 Red 证明还有一个真实控制流问题：

- `run_probe_subprocess` 调起内部 probe 时，参数解析器仍错误要求 `--offline-replay`，会在进入 probe 前直接失败。

### 这轮修复内容

- `scripts/benchmark_rapiddoc.py`
  - 新增 `.rapiddoc-probe` 路径解析、平台相关 `Python 3.11` 建环境命令和 probe Python 路径解析。
  - 新增 probe env 创建/复用逻辑；缺少模型目录时明确抛出 `OCR_MODEL_MISSING`。
  - 新增 outer orchestration：同一 PDF 先跑一次 live probe，再跑一次 fresh offline replay，并把 replay 原始结果写入显式 `--offline-replay` 路径。
  - 新增 inner probe 执行路径：在 probe Python 子进程里调用 `rapiddoc.doc_analyze`，并显式要求 `onnxruntime` 可导入。
  - 新增页级文本摘要、页 SHA-256、字符数、表格数、warning 聚合和峰值 RSS 采集。
  - 去掉 `offline_replay.matched = True` 硬编码，改为基于真实源 PDF `SHA-256` 和 fresh replay 结果重算。
  - 新增稳定 mismatch warning：`OFFLINE_REPLAY_MISMATCH: ...`。
  - 修正 internal probe 模式的参数解析，让它不再错误依赖 `--offline-replay`。

- `backend/tests/test_rapiddoc_benchmark.py`
  - 新增 probe venv command、probe env 创建、inner probe 指标采集、依赖缺失失败、fresh replay mismatch、outer orchestration、internal-probe 参数模式等测试。
  - 全部临时目录改为工作区可写位置，避开系统 `tmp_path` 权限噪音。

- `docs/superpowers/benchmarks/README.md`
  - 文档改为匹配当前真实行为：脚本会创建或复用 `.rapiddoc-probe`、运行 live probe 与 fresh offline replay、测量 probe env `site-packages` 与模型目录体积，并在缺少 PDF / 模型 / 依赖时明确失败。

### Green / 静态检查 / CLI failure smoke

Focused pytest：

```powershell
Set-Location .\backend; .\.venv\Scripts\python.exe -m pytest tests\test_rapiddoc_benchmark.py -q
```

```text
................................
32 passed in 0.35s
```

Ruff：

```powershell
Set-Location .\backend; .\.venv\Scripts\python.exe -m ruff check tests\test_rapiddoc_benchmark.py ..\scripts\benchmark_rapiddoc.py
```

```text
All checks passed!
```

缺模型目录时的 CLI failure smoke：

```powershell
$probeSmokeDir = Join-Path 'C:\Users\Administrator\Documents\Codex\2026-08-24\https-raw-githubusercontent-com-openbmb-staffdeck\work' 'task0-probe-smoke'
New-Item -ItemType Directory -Force -Path $probeSmokeDir | Out-Null
$pdfPath = Join-Path $probeSmokeDir 'sample.pdf'
Set-Content -LiteralPath $pdfPath -Value '%PDF-1.4' -Encoding utf8
Set-Location .\backend
.\.venv\Scripts\python.exe ..\scripts\benchmark_rapiddoc.py --pdf $pdfPath --output (Join-Path $probeSmokeDir 'benchmark.json') --model-dir (Join-Path $probeSmokeDir 'missing-models') --offline-replay (Join-Path $probeSmokeDir 'offline-replay.json')
```

```text
ProbeExecutionError: OCR_MODEL_MISSING: model directory does not exist: C:\Users\Administrator\Documents\Codex\2026-08-24\https-raw-githubusercontent-com-openbmb-staffdeck\work\task0-probe-smoke\missing-models
```

### 是否实际运行了 RapidDoc probe

没有在当前 turn 内对真实 RapidDoc / ONNX Runtime 依赖和真实仓库外审核 PDF 做成功 probe。

已核实的事实：

- 代码路径现在会在模型与依赖可用时创建或复用 `.rapiddoc-probe`，并尝试通过 probe Python 子进程运行 live probe 与 fresh replay。
- 缺模型目录时，脚本会明确失败，不产出伪造的“已测量”结果。

暂时无法在本 turn 内核实的部分：

- 当前环境没有提供可用的 RapidDoc / ONNX Runtime probe 依赖和真实仓库外模型目录，因此没有成功产出真实 measured `package_bytes`、`peak_rss_bytes`、`elapsed_seconds`、`page_count`、`non_empty_pages`、`text_chars`、`table_count` 和 offline replay 一致性结果。
- 因此，本任务现在完成的是“可执行的隔离 probe gate 与校验流程”，不是“RapidDoc 已在本机上完成实测并达标”。
