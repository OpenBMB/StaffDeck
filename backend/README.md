# Backend

FastAPI backend for the Skill Agent Loop MVP.

## Run

From the repository root, prefer:

```bash
scripts/dev_up.sh
```

For backend-only debugging:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
.venv/bin/uvicorn single_port_app:app --host 127.0.0.1 --port 5173
```

Swagger UI: `http://localhost:5173/docs`

The current production schema migration path supports SQLite only. Non-SQLite
database URLs are not a supported deployment configuration.

`CORS_ORIGINS` controls the allowed frontend origins. The root `scripts/dev_up.sh`
sets the local single-port origin by default and can add a public tunnel origin with
`PUBLIC_APP_ORIGIN`.

## Agent process sandbox

Commands exposed by the Harness are always executed through an OS process
sandbox. Anthropic's Sandbox Runtime is the preferred backend because it uses
the native primitive on macOS, Linux, and Windows. Release builds bundle the
SRT package and a matching Node binary, so end users do not need Node or a
global npm installation. Source deployments prepare the same reviewed runtime
once from the repository root:

```bash
python3 packaging/fetch_sandbox_runtime.py packaging/sandbox_runtime
```

The bootstrap uses the committed dependency lock and `npm ci`, verifies the
downloaded Node archive, and applies StaffDeck's reviewed unrestricted-network
compatibility patch. The repository-local runtime takes precedence over a
global `srt` installation. Unreviewed global installations are ignored unless
development explicitly sets `STAFFDECK_ALLOW_GLOBAL_SRT=true`; normal
`scripts/dev_up.sh` startup prepares the reviewed local runtime automatically.

When `srt` is unavailable, Linux deployments may use the existing Bubblewrap
backend (`bwrap`). There is no unsandboxed fallback: the `exec_command`
capability is reported as unavailable until one of these backends is installed.
The sandbox keeps the current TaskFrame workspace as the only writable area.
The tenant administrator selects unrestricted network access (the default), a
domain allowlist, or complete network denial in Runtime Settings. Policies are
enforced by SRT and fail closed when the selected backend cannot represent one.

On Windows, SRT also needs its one-time elevated setup so it can create the
dedicated `srt-sandbox` account and egress filter:

```powershell
srt windows-install
```

The macOS backend uses Seatbelt and works on both Intel (`x86_64`) and Apple
Silicon (`arm64`) builds. The packaging scripts bundle the matching Node
binary for the build architecture and sign it with the rest of the app.

## General Skill Code Runtime

通用技能生成的 Python/Bash runner 不直接依赖系统 Python。运行时按以下顺序选择环境：

1. `GENERAL_SKILL_RUNTIME_PYTHON` 指定的 Python；
2. `GENERAL_SKILL_RUNTIME_VENV` 指定虚拟环境中的 Python；
3. `backend/.venv/bin/python`；
4. 自动创建 `backend/.runtime_venv`。

`GENERAL_SKILL_RUNTIME_PACKAGES` 默认安装/校验 `requests,httpx`，用于通用 API
访问。需要文档解析或数据处理时可以扩展为：

```bash
GENERAL_SKILL_RUNTIME_PACKAGES="requests,httpx,beautifulsoup4,lxml,pypdf,python-docx,pandas,numpy,python-dateutil"
```

如果部署环境禁止自动安装依赖，设置：

```bash
GENERAL_SKILL_RUNTIME_AUTO_INSTALL="false"
GENERAL_SKILL_RUNTIME_PYTHON="/path/to/prepared/venv/bin/python"
```

## Demo Seed

Startup seeds:

- `tenant_demo`
- refund skill `after_sales_refund`
- exchange skill `after_sales_exchange`
- mock HTTP tool `order.query`

Set `DEMO_MODEL_API_KEY` before first startup if you want a default model config to be created automatically.
