# RapidDoc Offline Benchmark Gate

本目录只记录 StaffDeck 离线 RapidDoc 探针的准入门槛、执行约束和实测结论，不直接修改生产解析路径。

## 当前结论

- 当前仓库已经建立显式 probe orchestration、基准 JSON schema、输入边界和命令行报告脚本。
- 当前仓库没有提交任何真实审核 PDF、模型文件或 API key。
- 当前仓库没有把 RapidDoc 依赖安装进生产 `backend/.venv`。
- 截至 2026-08-27，RapidDoc ORT-only 方案尚未在本仓库内留下实测结果，因此下面所有“实测值”字段都必须保持待补状态，不能用估算值代替。

## Probe 约束

- 输入 PDF 必须由操作者显式通过 `--pdf` 提供，且文件路径必须位于仓库外。
- Probe 仅允许在隔离环境中执行；Windows 使用 `py -3.11 -m venv .rapiddoc-probe`，macOS/Linux/WSL 使用 `python3.11 -m venv .rapiddoc-probe`。
- 只有显式 probe 环境允许联网安装 RapidDoc 目标依赖；不得把测量缓存复制进正式环境。
- Probe 结果只写入显式 `--output` 目标，不回写仓库源 PDF。
- `scripts/benchmark_rapiddoc.py` 在 probe 依赖和模型齐备时，会先运行一次 live probe，再在同一 PDF 上运行一次 fresh offline replay，并把 replay 原始结果写入显式 `--offline-replay` 路径。
- 真实审核文件只用于本机人工验收，不进入 Git。

## 验收门槛

RapidDoc 只有在真实材料隔离验证全部通过后才进入生产依赖：

1. 新增依赖、模型和缓存的稳定磁盘占用不超过 2 GB。
2. 单任务峰值内存不超过 4 GB。
3. 当前测试 PC 上，普通 20 页、约 300 DPI 的审核扫描件处理时间不超过 3 分钟。
4. 清晰中英文印刷文字的人工抽样完整率达到 98% 左右。
5. 关键审核表格的行列对应关系人工抽样正确率不低于 90%。
6. 每个输入页面在输出中都有页级结果、空白页标记或明确失败记录，不允许静默漏页。
7. 模型准备完成后，断网环境可以完成启动和解析。
8. 原生文字 PDF 的解析性能和文本结果不得出现明显回退。

如果 ORT-only 不能运行，或者任一门槛未满足，则保持同一适配器合约并改走 RapidOCR + RapidTable 兼容路径；不绕过门槛直接上线。

## 报告格式

`scripts/benchmark_rapiddoc.py` 当前负责固化基准报告边界和 probe 编排：

- 必填字段：`engine`、`package_bytes`、`model_bytes`、`peak_rss_bytes`、`elapsed_seconds`、`page_count`、`non_empty_pages`、`text_chars`、`table_count`、`offline_replay`、`warnings`
- 拒绝缺字段、负数资源值，以及 `non_empty_pages > page_count` 或 `offline_replay.page_sha256` 数量与 `page_count` 不一致的结果
- 脚本会在仓库根下创建或复用 `.rapiddoc-probe`，并用该环境的 Python 子进程执行真正的 probe
- `package_bytes` 取自 `.rapiddoc-probe` 环境的 `site-packages` 实测大小，`model_bytes` 取自显式 `--model-dir`
- `offline_replay` 至少保留 `matched`、`source_sha256`、逐页 `page_sha256`、`char_count`、`table_count` 和 `mismatch_fields`
- `matched` 不接受调用方硬编码输入；脚本会重新计算源 PDF 的 `SHA-256`，并比较 live probe 与 fresh replay 的 `page_count`、`page_sha256`、`text_chars` 和 `table_count`
- 缺少显式 PDF、模型目录、RapidDoc 或 ONNX Runtime 时，脚本会明确失败，不把未测得值写进报告

## 待补实测记录

| 项目 | 当前状态 |
| --- | --- |
| 测试机 | 待实际 probe 填写 |
| RapidDoc 版本 | 待实际 probe 填写 |
| Python 版本 | 待实际 probe 填写 |
| ORT-only 依赖包尺寸 | 待实际 probe 填写 |
| 模型目录尺寸 | 待实际 probe 填写 |
| 峰值 RSS | 待实际 probe 填写 |
| 处理耗时 | 待实际 probe 填写 |
| 输入 PDF 类型 | 待实际 probe 填写 |
| 断网重放结果 | 待实际 probe 填写 |

## 建议执行顺序

1. 在显式 probe 环境创建 `.rapiddoc-probe`。
2. 仅在该环境安装 RapidDoc 目标依赖并准备模型目录。
3. 在仓库外准备待测 PDF，并准备好模型目录。
4. 运行 `scripts/benchmark_rapiddoc.py`；脚本会生成基准 JSON，并把 fresh offline replay 原始结果写入显式 `--offline-replay` 路径。
5. 把实测机型、版本、尺寸、耗时、内存和结论回填到本目录文档；未实测项继续保持待补，不写估算值。
