# StaffDeck OSS 安装与升级

## 全新源码安装

需要 Python 3.11+、Node.js 22.x ≥22.19 或 ≥24，以及 npm。
准备流程通过 npm 缓存调用项目锁定的 pnpm，无需全局安装 pnpm 或启用 Corepack。

```sh
git clone https://github.com/OpenBMB/StaffDeck.git
cd StaffDeck
python3 prepare.py
staffdeck serve
```

打开 `http://127.0.0.1:5173/workspace/gallery`。全新安装的账号密码均为 `admin`。
首次初始化携带 IT、人事、行政、财务、法务五位员工及其 SOP、知识库、技能和示例连接器。
先在网页配置模型；密钥、数据库和会话不随发行包提供。
CLI 操作见 [CLI.md](CLI.md)。后台部署也可运行 `backend/.venv/bin/python launch.py`。
Windows 使用 `python prepare.py` 和 `backend\.venv\Scripts\python.exe launch.py`。

## 升级旧版：继续使用原数据

升级会保留原账号密码、员工、资源绑定、SOP 版本、知识库、会话、定时任务和附件。
默认员工只初始化一次，已有数据库不会重新导入示例或重置密码。

1. 先备份整个数据目录、原 `.env` 和 `APP_SECRET`。使用 `dev_up.sh` 管理的服务
   可保持运行，脚本会在准备成功后停止原进程并启动新版。
2. 在同一源码位置更新；原 `backend/.env` 和 `backend/skill_agent_loop.db` 会被识别，
   SQLite 相对路径继续按旧版的 backend 目录解析，不依赖当前终端位置。
3. 使用 `scripts/dev_up.sh --detach` 启动：缺少 Harness v3 构建产物、依赖或 Python SDK 时，
   会自动构建前端与引擎并安装本地 SDK；已有产物时跳过引擎重建。准备失败会退出，
   保留仍在运行的原服务。Windows 可使用 `scripts/dev_up.ps1 --detach`。
   使用 `launch.py` 或自行管理的 uvicorn 服务时，仍需先运行 `python3 prepare.py`。
   启动前会校验已有模型密钥能否解密，
   并在数据目录的 `backups/pre-upgrade-*.db` 创建权限受限的 SQLite 备份。
4. 使用原账号密码登录，核对员工、会话、知识库及定时任务；不要把原密码改成新装默认密码。

桌面版继续使用原数据目录：

| 系统 | 默认数据目录 |
| --- | --- |
| macOS | `~/Library/Application Support/StaffDeck` |
| Windows | `%APPDATA%\StaffDeck` |
| Linux | `~/.local/share/StaffDeck` |

`ULTRARAG_DATA_DIR` 显式指定独立实例时，不会自动接管另一个源码目录的数据。
若原数据位于其他位置，设置原 `DATABASE_URL` 和 `APP_SECRET`；附件和工作目录也需保留。
存在多个候选数据库或密钥不匹配时，升级会停止，不会创建空库替代原数据。
旧版从未自定义签名密钥时，保留其历史默认值以解密原凭据；全新安装仍生成随机密钥。
不要删除、替换或公开 `.installation-seed`；旧安装的 `installation-secrets.json` 仍可读取。
新安装不把外部 `APP_SECRET` 或初始密码复制到 JSON：原环境配置应持续保留，随机签名材料
仅保存在本机权限受限的二进制文件中。

默认租户沿用旧开源版的 `tenant_demo`。自定义租户须将 `distribution/manifest.json` 中的
`installation.tenant_id` 设置为原值，再运行 `prepare.py` 使前端和 CLI 使用相同租户。
不修改数据库里的租户或用户 ID。

需要回退时：停止新版，恢复升级前的**数据库备份、配置和密钥**，再启动对应旧版本。
不要让两个不同版本同时写同一个数据库。已有附件文件不会被迁移程序移动或删除。

### 升级后提示 Harness v3 engine not built

`harness-engine/apps/cli/lib/bin.js missing` 表示运行时尚未构建成功。
这个文件是构建产物，不在 Git 和源码压缩包中。只执行 `git pull` 或安装 Python
依赖不会生成它。使用本版 `scripts/dev_up.sh --detach` 会自动检测并完成准备，
再启动服务；前端或运行时准备失败时，会在停止原服务前退出。

若使用旧版启动脚本或自行管理的服务，可在源码根目录手动执行完整准备流程：

```sh
cd /path/to/StaffDeck
node --version
# Node 需要 22.x >=22.19，或 >=24；pnpm 由准备流程按项目锁定版本调用。
python3 prepare.py --no-cli-link
```

准备命令成功后，核对启动文件并按原来的服务方式重新启动：

```sh
test -f harness-engine/apps/cli/lib/bin.js
backend/.venv/bin/python launch.py
```

`prepare.py` 同时构建前端、Harness 运行时并安装本地 Python SDK；
它不初始化或重置业务数据库。保留原数据目录、`.env` 和签名密钥，
准备过程中若有错误，应先解决该错误，再启动服务。
报错若指向别的引擎目录，还需核对原有 `HARNESS_V3_ROOT` 配置是否指向预期安装。

## Docker

```sh
docker compose up --build -d
```

默认仅绑定本机 5173，数据放在 `staffdeck-data` 命名卷。升级保留原卷和原密钥，
不要使用 `docker compose down -v` 删除数据卷。桌面签名、公证及发布步骤见
[Release 构建](packaging/RELEASE.md)。
