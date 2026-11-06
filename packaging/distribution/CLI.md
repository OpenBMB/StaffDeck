# StaffDeck CLI 使用指南

CLI 与网页使用同一服务、权限和 Harness v3 执行链路。对话、SOP、知识库、技能、
连接器、执行记录和定时任务都通过公共 API 访问。

## 安装后开始使用

源码安装运行 `python3 prepare.py`，会自动将 `staffdeck` 注册到用户的 PATH。
桌面安装首次打开 StaffDeck 应用后会注册随附的 CLI；请新开一个 Terminal。
Linux deb 安装后直接提供 `staffdeck`、`staffdeck-cli`。
构建机使用 `python3 prepare.py --no-cli-link`，避免注册构建机的终端命令。

```sh
staffdeck --help
staffdeck serve                 # 启动服务，保留此终端；桌面应用也能启动服务
# 在另一个终端：
staffdeck login                 # 全新安装：admin / admin；升级使用原账号密码
staffdeck agent list
staffdeck chat                  # 输入序号或名称选择员工
staffdeck chat --agent 财务      # 直接开始交互对话
staffdeck chat --agent IT "你好" # 发送一条消息后退出
```

先在网页配置可用模型，再执行真实对话。默认五位员工归属 `@admin`，安装包不含模型密钥。
连接器使用示例服务配置，接入实际业务前请修改接口和认证配置。

聊天中 `/new` 开始新会话，`/exit` 退出。普通追问沿用当前会话。
`agent list` 和 `agents list` 等价，兼容 `--chat`、`--auth`。

## 连接地址与账号

```sh
staffdeck auth status
staffdeck logout
staffdeck --base-url https://staff.example.com --profile remote login --tenant-id tenant_demo
staffdeck --profile remote agent list
staffdeck --profile remote chat --agent IT
```

本机登录默认读取安装的租户和服务实际选定的端口；未保存端口时使用 `127.0.0.1:5173`。
自定义租户使用 `--tenant-id`。通过 `--base-url` 连接远程服务，允许带代理路径前缀。
非本机连接要求 HTTPS。密码交互输入，不放在命令参数中；自动化可使用 `--password-stdin`。
登录仅保存本设备的有限期 API 凭据，不保存密码。`logout` 撤销本设备凭据。
POSIX 下连接文件权限为 0600，目录为 0700。

若无法找到 `staffdeck`，新开 Terminal；也可直接运行源码安装的
`backend/.venv/bin/staffdeck`（Windows：`backend\.venv\Scripts\staffdeck.exe`）。
macOS 桌面包随附 `StaffDeck.app/Contents/MacOS/staffdeck-cli`；
Windows 随附安装目录中的 `staffdeck-cli.exe`。

## 脚本、资源和执行记录

默认聊天输出可读文本。脚本需要稳定 JSON 时使用 `staffdeck --output json ...`，
或运行独立机器接口 `staffdeck-api ...`（源码安装）。

```sh
staffdeck --output json agents list
staffdeck sop list --agent-id AGENT_ID
staffdeck skill list --agent-id AGENT_ID
staffdeck tool list --agent-id AGENT_ID
staffdeck knowledge-bases list --agent-id AGENT_ID
staffdeck scheduled-tasks list --agent-id AGENT_ID
staffdeck runs get --run-id RUN_ID
staffdeck runs result --run-id RUN_ID
staffdeck runs events --run-id RUN_ID
staffdeck guide
```

各资源的参数、写入版本和请求体通过 `staffdeck RESOURCE COMMAND --help` 查看。
API Key 可通过 `STAFFDECK_API_KEY` 注入，服务地址通过 `STAFFDECK_BASE_URL` 设置。
使用 `STAFFDECK_CONFIG_DIR` 指定独立连接目录，`--profile` 区分环境。

等待超时、断网或 Ctrl+C 不会自动取消远程执行。按提示保留的运行编号查询状态，
确认结果后再决定是否重试；不要重复提交可能已执行的业务操作。
