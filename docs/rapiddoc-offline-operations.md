# StaffDeck 离线 RapidDoc 操作说明

StaffDeck 对 PDF 采用两条路径：带文字层的 PDF 直接使用原生解析；扫描型或文字层为空的 PDF，在显式开启结构化 PDF 后使用本机 RapidDoc + ONNX Runtime CPU。启动时不会自动下载 OCR 依赖或模型。

## 1. 默认状态

`backend/.env.example` 默认关闭结构化 PDF：

```dotenv
STRUCTURED_PDF_ENABLED="false"
STRUCTURED_PDF_ENGINE="rapiddoc"
RAPID_MODELS_DIR=""
```

关闭时不会影响普通文字 PDF。要处理扫描 PDF，先使用项目 OCR extra 安装官方 `rapid-doc==0.9.10` 和 `onnxruntime==1.20.1`，再显式准备模型。本项目不启用 OpenVINO、GPU 或云端 OCR。

Windows PowerShell：

```powershell
.\backend\.venv\Scripts\python.exe -m pip install -e ".[ocr]"
```

macOS、Linux 或 WSL：

```bash
backend/.venv/bin/python -m pip install -e '.[ocr]'
```

## 2. 检查模型（不会下载）

Windows PowerShell：

```powershell
.\backend\.venv\Scripts\python.exe scripts\prepare_rapiddoc_models.py --check-only
```

macOS、Linux 或 WSL：

```bash
backend/.venv/bin/python scripts/prepare_rapiddoc_models.py --check-only
```

返回 JSON 中的 `ready: true`、`manifest_exists: true` 和 `missing_count: 0` 才表示本地模型完整。退出码为 `0` 表示就绪，退出码为 `1` 表示尚未准备好或清单不完整。

## 3. 显式准备模型

模型准备可能需要一次网络访问；它只会在你明确执行 `--prepare` 时发生。准备器只下载 RapidDoc 当前版本所需的 6 个版面、表格和 OCR 分类 ONNX 模型，并校验 SHA-256；OCR 检测/识别小模型由官方 wheel 提供。准备完成后，运行时通过 `HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1` 和 `STAFFDECK_RAPIDDOC_OFFLINE=1` 强制离线。

Windows PowerShell：

```powershell
.\backend\.venv\Scripts\python.exe scripts\prepare_rapiddoc_models.py --prepare
```

macOS、Linux 或 WSL：

```bash
backend/.venv/bin/python scripts/prepare_rapiddoc_models.py --prepare
```

如需指定模型目录，请在两条命令末尾追加 `--model-dir`。也可以在 `backend/.env` 设置 `RAPID_MODELS_DIR`。不要把模型目录放入 Git，也不要把 API Key 写入准备脚本参数。

## 4. 开启并启动

在 `backend/.env` 中设置：

```dotenv
STRUCTURED_PDF_ENABLED="true"
STRUCTURED_PDF_ENGINE="rapiddoc"
RAPID_MODELS_DIR=""
```

Windows PowerShell：

```powershell
.\scripts\dev_up.ps1 --detach
```

macOS、Linux 或 WSL：

```bash
scripts/dev_up.sh --detach
```

如果 Windows PowerShell 的执行策略阻止脚本，可使用系统允许的策略显式调用：

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\dev_up.ps1 --detach
```

当结构化 PDF 已开启但模型没有准备好时，启动命令会停止并提示先执行 `prepare_rapiddoc_models.py --prepare`；它不会静默回退，也不会在后台下载模型。若结构化 PDF 关闭，启动不会检查 RapidDoc 模型。

## 5. 运行后检查

Windows PowerShell：

```powershell
.\scripts\dev_status.ps1
curl.exe http://127.0.0.1:5173/api/health
Start-Process http://127.0.0.1:5173/workspace/gallery
```

macOS、Linux 或 WSL：

```bash
scripts/dev_status.sh
curl http://127.0.0.1:5173/api/health
```

`/api/health` 的 `structured_pdf.status` 为 `disabled`、`needs_prepare` 或 `ready`。应用总体的 `status: "ok"` 只表示 StaffDeck 服务存活，不代表 OCR 模型已经就绪；审核材料页面会继续显示每份文件的提取方法、页数、字数和失败原因。

## 6. 审核材料处理约束

- 原始上传文件始终保留；OCR 产生的全文和页级信息单独保存。
- 带文字层的 PDF 不调用 OCR，避免不必要的耗时和识别误差。
- 扫描 PDF 只有在 RapidDoc 模型和依赖就绪时才会进入结构化提取。
- OCR 失败时可以在页面重试可恢复错误；如果是文字层缺失或空文本导致的同一文件必然失败，应替换文件或先准备 OCR 后再处理。
- 模型准备和真实扫描 PDF 验收必须在目标电脑上完成，仓库单元测试中的模拟 RapidDoc 结果不能替代真实识别质量、耗时和内存测试。
