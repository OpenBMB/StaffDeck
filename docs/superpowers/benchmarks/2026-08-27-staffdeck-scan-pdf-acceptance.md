# StaffDeck 扫描 PDF 离线验收记录

## 结论

**未通过生产启用门禁：待真实模型和脱敏扫描样本。**

本记录只保存统计结果、状态和错误摘要，不保存用户审核 PDF、提取全文、模型文件、API Key 或生成的审核报告。当前结果不能证明 RapidDoc 的中文表格识别质量，也不能证明真实 4 万字审核报告已经完成端到端验收。

## 本次环境

- 验收日期：2026-08-28
- 操作系统：Windows PowerShell
- Python：使用仓库 `backend/.venv`
- OCR engine 配置：`rapiddoc`
- 设备路径：CPU/ONNX Runtime 设计路径
- `STRUCTURED_PDF_ENABLED`：`false`

## 已验证项目

| 项目 | 结果 | 证据摘要 |
| --- | --- | --- |
| 原生文字 PDF 路径 | 通过 | 后端定向回归覆盖原生 PDF、完整文本和页引用 |
| 共享 PDF 入口 | 通过 | 知识库、审核材料、聊天附件和 Harness 定向回归通过 |
| OCR 模型无隐式下载 | 通过 | 启动脚本只做本地 readiness 检查；缺模型时给出显式 prepare 命令 |
| `--check-only` | 通过 | 当前返回 `ready=false`、`manifest_exists=false`、`missing_count=1`、退出码 `1` |
| 结构化 PDF 默认状态 | 通过 | `/api/health` 返回 `structured_pdf.status=disabled` |
| Windows 服务启动 | 通过 | `/api/health` HTTP 200；`/workspace/gallery` HTTP 200 |
| 后端定向回归 | 通过 | 226 passed |
| 前端全量测试 | 通过 | 56 个测试文件、242 passed |
| 前端构建 | 通过 | `tsc -b && vite build` 通过；仅有既有大 chunk 警告 |

## 尚未完成的真实验收

以下项目不能用单元测试或模拟 RapidDoc 返回值代替：

1. RapidDoc/ONNX Runtime 在目标电脑上的实际安装、模型版本、模型体积、峰值内存和耗时。
2. 脱敏的中文扫描审核表、英文扫描页、混合 PDF、表格密集 PDF，以及损坏/加密 PDF。
3. “审核组准备会记录”“首末次会议签到表”“受审核组织信息确认表”同类扫描材料的页级 OCR 结果。
4. 关闭网络后的重复解析结果是否与首次解析在页数、字符数、表格数和页文本哈希上匹配。
5. 扫描材料进入审核项目后，从上传、OCR、分块、知识库命中到报告生成的真实 coverage 100% 流程。

当前 `--check-only` 显示本机没有准备 RapidDoc 模型，因此没有运行真实 OCR benchmark，也没有填写虚构的准确率、耗时、内存或 replay 数值。

## 目标电脑上的补验步骤

请把脱敏样本放在仓库外部，并在项目根目录执行。Windows PowerShell：

```powershell
.\backend\.venv\Scripts\python.exe scripts\prepare_rapiddoc_models.py --check-only
.\backend\.venv\Scripts\python.exe scripts\prepare_rapiddoc_models.py --prepare
.\backend\.venv\Scripts\python.exe scripts\prepare_rapiddoc_models.py --check-only
```

确认 `ready=true` 后，在 `backend/.env` 开启：

```dotenv
STRUCTURED_PDF_ENABLED="true"
STRUCTURED_PDF_ENGINE="rapiddoc"
```

随后使用当前系统命令启动并检查：

```powershell
.\scripts\dev_up.ps1 --detach
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:5173/api/health
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:5173/workspace/gallery
```

再用外部样本执行 `scripts/benchmark_rapiddoc.py`，只把版本、页数、非空页数、字符数、表格数、耗时、峰值内存、首尾哨兵哈希和离线 replay 状态写回本记录。验收通过前，不应把 `STRUCTURED_PDF_ENABLED` 改成默认值，也不应删除原始材料或完整派生文本。

## 全量回归备注

后端全量回归结果为 `2079 passed, 4 skipped, 24 failed`。24 项失败均不属于本次 RapidDoc 定向测试，主要是当前 Windows 环境的符号链接权限、`fcntl` 不可用、POSIX bash 预期、运行时目录写权限、并发清理时序和旧测试中文编码输出问题；定向 RapidDoc/文档/审核材料回归为 `226 passed`。这些环境问题没有被标记为 OCR 验收通过。
