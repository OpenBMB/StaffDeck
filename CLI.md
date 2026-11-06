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

## 新建员工、技能和 SOP

下面的操作在已登录的实例中创建私有资源。将返回值中的 ID 复制到变量，
不要把展示名称作为 ID。资源命令输出 JSON，正文通过 UTF-8 文件或 stdin 传入。

```sh
printf '{"name":"核算助手","source_mode":"blank"}' | staffdeck agent create --json -
AGENT_ID='从上一步 data.id 复制'
```

将下面内容保存为 `skill.json`。这是一个带实际 Python 脚本的技能包：

```json
{
  "name": "核算技能",
  "slug": "cli-calculator",
  "status": "draft",
  "files": [
    {
      "path": "SKILL.md",
      "content": "---\nname: 核算技能\ndescription: 执行脚本计算三个整数a*b+c\n---\n实际运行 scripts/calculate.py，参数依次为a b c，返回脚本标准输出，不要口算。"
    },
    {
      "path": "scripts/calculate.py",
      "content": "import json,sys\na,b,c=map(int,sys.argv[1:4])\nprint(json.dumps({'total':a*b+c}))\n"
    }
  ]
}
```

```sh
staffdeck skill create --agent-id "$AGENT_ID" --json skill.json
SKILL_ID='从上一步 data.id 复制'
staffdeck skill publish --agent-id "$AGENT_ID" --slug cli-calculator
```

将下面内容保存为 `sop.json`，把两处 `SKILL_ID` 替换为刚创建的技能 ID：

```json
{
  "skill_id": "cli_calculation_sop",
  "name": "核算流程",
  "version": "1.0.0",
  "description": "调用核算技能计算a*b+c并交付结果",
  "trigger_intents": ["执行核算流程"],
  "nodes": [{
    "node_id": "calculate",
    "type": "action",
    "name": "执行核算",
    "instruction": "从请求中提取a,b,c，必须执行核算技能脚本并返回真实结果。",
    "capability_refs": {
      "general_skill_ids": ["SKILL_ID"],
      "required_general_skill_ids": ["SKILL_ID"]
    }
  }, {
    "node_id": "deliver",
    "type": "respond",
    "name": "交付结果",
    "instruction": "依据上一节点的真实执行结果，返回total，不要重新计算。"
  }],
  "edges": [{"source_node_id":"calculate","next_node_id":"deliver"}],
  "start_node_id": "calculate",
  "terminal_node_ids": ["deliver"]
}
```

```sh
staffdeck sop create --agent-id "$AGENT_ID" --json sop.json
DRAFT_ID='从上一步 data.id 复制'
staffdeck sop validate --agent-id "$AGENT_ID" --sop-id cli_calculation_sop --draft-id "$DRAFT_ID"
# 确认 data.valid 为 true 后发布：
staffdeck sop publish --agent-id "$AGENT_ID" --sop-id cli_calculation_sop --draft-id "$DRAFT_ID"
SOP_RESOURCE_ID='从发布响应 data.sop.id 复制'
```

创建技能、发布 SOP 会自动绑定到目标员工。手动调整绑定时，
`set-resources` 替换员工的**完整资源列表**，需保留所有要继续使用的资源。
SOP 的 `resource_type` 是 `skill`，绑定用发布响应中的 `data.sop.id`；
`--sop-id` 用的是 card 的 `skill_id`（本例为 `cli_calculation_sop`），两者不能混用。
通用技能的 `resource_type` 是 `general_skill`。

```sh
staffdeck agent resources --agent-id "$AGENT_ID"
printf '[{"resource_type":"general_skill","resource_id":"%s"},{"resource_type":"skill","resource_id":"%s"}]' \
  "$SKILL_ID" "$SOP_RESOURCE_ID" | staffdeck agent set-resources --agent-id "$AGENT_ID" --json -
staffdeck chat --agent "$AGENT_ID" "执行核算流程，a=37,b=19,c=17"
```

预期核算结果为 `720`。真实模型验收还应检查 `runs result` 的 `tool_calls`、
`task_results`，确认脚本成功执行、目标 SOP 完成，而不只核对回复文字。
使用 `staffdeck --output json chat --agent-id "$AGENT_ID" --message "..."`
可同时获得运行回执和最终结构化结果。

需要下载生成的文件时，先从产物列表读取 `task_frame_id` 和 `path`：

```sh
staffdeck runs artifacts --run-id RUN_ID
staffdeck runs download-artifact --run-id RUN_ID --task-frame-id TASK_FRAME_ID \
  --artifact-path '产物列表中的 path' --output-path ./result.json
```

`--artifact-path` 是服务端产物路径，`--output-path` 是本地保存位置；已有本地文件不会被覆盖。
