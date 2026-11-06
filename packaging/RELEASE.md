# OSS Release 构建

公开仓库直接构建 OSS 产品，不需要连接内部打包服务、企业权限中心或私有仓库。
根目录的 `distribution/manifest.json` 定义公开产品默认值；构建时版本由 tag 或手工输入覆盖。
运行时与各自的第三方许可证随源码包提供，仓库 LICENSE 保持 AGPL-3.0。

## GitHub Actions

- PR 会调用 `oss-checks` 验证公开边界、旧版升级、CLI、前端及 Harness 的干净构建。
- 非草稿 PR 还会构建四个平台的未签名安装包；草稿仅跑 OSS 检查，不读取签名凭据。
- 原 `release` 工作流继续接受 `v*` 或数字开头的 tag，以及 `workflow_dispatch`。
- 发布前先通过同一套 OSS 检查，再分别构建 macOS arm64/x86_64、Windows x64、Linux x64。
- Node 构建工具和随安装包提供的 Node 均为 22.19.0，满足 Harness 的运行时要求。
- macOS 先读取文件头筛选原生代码候选，再执行完整的 `file` / `lipo` 架构校验，
  避免为 Node 依赖中的每个普通资源启动子进程；签名仍处理所有真实 Mach-O 文件。
- 每个平台先执行 `prepare.py --without-backend` 构建前端和运行时，原生脚本再安装
  后端及本地 SDK，使用 `SKIP_FRONTEND=1` 复用已验证的前端产物。
- 原生包同时携带桌面入口和 `staffdeck-cli`。发布检查会执行 CLI 帮助、员工列表及登录验收。
- 重启验收从桌面入口切换到控制台的 `staffdeck serve`，使用同一数据库并验证原登录凭据。
- 源码 ZIP 从当前公开 Git 工作树的已跟踪文件生成，附 SHA-256 校验；不包含
  `.env`、数据库、模型密钥、构建缓存、企业前端、内部模块或内部构建机协议。
- tag 成功后将各平台安装包及源码 ZIP 上传到 Release。手工触发只上传 Actions artifacts。

签名和公证继续使用仓库原有的 macOS/Windows Secrets；PR 验证不使用签名凭据。
未配置签名时产物保留原有的未签名行为，不会假称通过公证。

## 本地复现

```sh
corepack enable
python3 prepare.py --no-cli-link
backend/.venv/bin/python -m pip install pytest pytest-asyncio
backend/.venv/bin/python -m pytest backend/tests/test_cli_console.py backend/tests/test_oss_storage_upgrade.py
python3 packaging/source_release.py --version 0.6.0 --output oss-source-output
```

原生构建分别使用 `packaging/build_macos.sh`、`packaging/build_linux.sh` 或
`packaging/build_windows.ps1`，设置 `VERSION` 和 `SKIP_FRONTEND=1`。
只能在对应系统和架构上验证原生产物。合并 PR 不会自动创建 tag 或发布 Release。
