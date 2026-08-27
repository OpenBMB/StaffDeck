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
