# StaffDeck 数字员工开放 API v1

本文档面向需要通过服务端调用 StaffDeck 数字员工的业务系统。开放 API 复用与对话端、自动任务相同的 AgentLoop/Harness v2 执行内核。

## 环境与接口描述

| 环境 | Base URL | Swagger | OpenAPI |
| --- | --- | --- | --- |
| 测试 | `http://39.102.210.77:10087/api/v1` | `/docs` | `/openapi.json` |
| 10086 | `http://39.102.210.77:10086/api/v1` | `/docs` | `/openapi.json` |

建议外部系统首先接入 10087。文档中的 `$BASE` 表示完整 Base URL。

## 密钥模型

业务调用统一使用：

```http
Authorization: Bearer sd_live_xxx
Content-Type: application/json
```

请求体不传 `tenant_id`。服务端从凭证推导租户、API Client、scope 和员工边界。

### 右上角用户菜单创建账号全量密钥

登录 StaffDeck 后，在整个界面右上角打开当前用户菜单，选择“API 全量密钥”。每个用户只能为自己创建、轮换和禁用账号密钥；管理员也不能从账号管理列表代其他用户生成密钥。这里创建的密钥绑定当前登录账号，而不是绑定单个数字员工。

| 类型 | 使用场景 | 权限边界 |
| --- | --- | --- |
| 账号全量密钥（大密钥） | 将当前 StaffDeck 账号的能力整体接入外部系统 | 可浏览和选择广场员工、创建员工、运行当前账号可访问的员工，并按账号本人权限管理自有员工的 SOP、知识、技能、工具和定时任务 |

权限不是创建时固化的员工 ID 列表，而是在每次请求时根据账号重新计算：

- 管理员账号可访问并管理租户内全部未隐藏员工；
- 普通成员可访问自己创建的员工、总员工和已发布到广场的员工，但只能修改自己创建的员工；
- 账号角色、员工归属、发布或隐藏状态变化后，下一次 API 请求立即按新权限执行。

账号全量密钥仍然不是租户配置密钥：

- 不能超出该账号本人在界面中的管理范围，也不能跨租户；
- 不能读取模型供应商密钥、工具明文凭证或原始模型 COT；
- 不能创建其他账号的密钥，也不能获取租户级审计或租户级用量。

### 员工设置页创建运行密钥

员工卡片右上角的“API 密钥”入口仅创建绑定该员工的运行密钥。它可以创建会话、运行任务、读取 Trace 与产物，但不能读取完整资源配置，也不能调用其他员工。

密钥明文只在创建或轮换时返回一次。StaffDeck 仅保存带服务端 pepper 的摘要和可识别前缀。

### 服务端创建 API Client

需要租户管理员 JWT 进行首次引导：

```bash
curl -X POST "$BASE/api-clients" \
  -H "Authorization: Bearer $ADMIN_JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "ERP integration",
    "scopes": ["credentials:write", "agents:*", "sessions:*", "runs:*"]
  }'
```

随后创建租户密钥或绑定员工的密钥：

```bash
curl -X POST "$BASE/api-clients/$CLIENT_ID/credentials" \
  -H "Authorization: Bearer $ADMIN_JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "finance employee runtime",
    "agent_id": "agent_xxx",
    "scopes": ["agents:read", "capabilities:read", "sessions:read", "sessions:write", "runs:create", "runs:read", "runs:cancel"]
  }'
```

不传 `agent_id` 时创建租户密钥；传入后创建只能访问指定员工的密钥。Credential scope 必须是 API Client scope 的子集。

## 最小调用链

### 1. 创建持续会话

```bash
curl -X POST "$BASE/agents/$AGENT_ID/sessions" \
  -H "Authorization: Bearer $STAFFDECK_API_KEY" \
  -H "Idempotency-Key: crm-session-10001" \
  -H "Content-Type: application/json" \
  -d '{
    "external_session_id": "crm-session-10001",
    "external_user_id": "customer-9001",
    "title": "订单咨询",
    "metadata": {"channel": "crm"}
  }'
```

保存响应中的 `id` 作为 `$SESSION_ID`。相同 Credential 重复提交相同 `external_session_id` 时返回原会话。

### 2. 发起 Run

```bash
curl -X POST "$BASE/agents/$AGENT_ID/runs" \
  -H "Authorization: Bearer $STAFFDECK_API_KEY" \
  -H "Idempotency-Key: crm-run-10001" \
  -H "Content-Type: application/json" \
  -d "{
    \"input\": \"查询差旅费报销标准\",
    \"session_id\": \"$SESSION_ID\",
    \"session_mode\": \"stateful\",
    \"metadata\": {\"business_id\": \"expense-1001\"}
  }"
```

无状态调用不传 `session_id`，并设置 `"session_mode": "stateless"`。接口返回 HTTP 202 和 Run Job：

```json
{
  "id": "apijob_xxx",
  "kind": "run",
  "status": "queued",
  "stage": "queued",
  "progress": 0,
  "agent_id": "agent_xxx"
}
```

### 3. 查询状态和结果

```http
GET /runs/{run_id}
GET /runs/{run_id}/result
POST /runs/{run_id}:cancel
```

Job 状态：`queued`、`running`、`awaiting_input`、`succeeded`、`failed`、`cancelled`。

成功结果包含：

```json
{
  "run_id": "apijob_xxx",
  "agent_id": "agent_xxx",
  "session_id": "session_xxx",
  "reply": "根据报销制度……",
  "citations": [],
  "tool_calls": [],
  "task_results": [],
  "awaiting_input": null,
  "session_state": {},
  "artifacts": []
}
```

### 4. SSE 实时事件

```bash
curl -N "$BASE/runs/$RUN_ID/events" \
  -H "Authorization: Bearer $STAFFDECK_API_KEY" \
  -H "Accept: text/event-stream"
```

断线后携带 `Last-Event-ID` 续传。公开 Trace 包含意图、TaskFrame、能力选择、工具结果、引用和回复阶段，不包含模型原始 COT。

### 5. 下载 Harness 产物

```http
GET /runs/{run_id}/artifacts
GET /runs/{run_id}/artifacts/{task_frame_id}?path=report.md
```

产物路径会进行工作区边界校验，不能通过相对路径访问其他任务或服务器文件。

## 核心资源 API

### 数字员工

```text
GET/POST  /agents
GET/PATCH /agents/{agent_id}
POST      /agents/{agent_id}:archive
GET/PUT   /agents/{agent_id}/models
GET/PUT   /agents/{agent_id}/resources
GET       /agents/{agent_id}/capabilities
```

模型接口只接受和返回已有 `model_config_id`，不会暴露供应商 API Key。

### 开放广场员工

```text
GET  /gallery/agents
GET  /gallery/agents/{agent_id}
POST /gallery/agents/{agent_id}:add
```

`GET /gallery/agents` 只返回已发布、启用且不是总员工的广场员工，每条记录包含 `added`，表示当前账号是否已经选择使用。选择员工时建议携带幂等键：

```bash
curl -X POST "$BASE/gallery/agents/$AGENT_ID:add" \
  -H "Authorization: Bearer $STAFFDECK_API_KEY" \
  -H "Idempotency-Key: gallery-add-$AGENT_ID"
```

`:add` 将广场员工加入当前账号的可用员工列表，不复制资源。如果需要在“我的数字员工”中新建一份可独立修改的副本，使用已有的创建接口：

```bash
curl -X POST "$BASE/agents" \
  -H "Authorization: Bearer $STAFFDECK_API_KEY" \
  -H "Idempotency-Key: copy-gallery-$AGENT_ID" \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"我的员工副本\",\"source_mode\":\"copy\",\"copy_from_agent_id\":\"$AGENT_ID\"}"
```

### SOP

```text
GET/POST /agents/{agent_id}/sops
POST     /agents/{agent_id}/sops:generate
POST     /agents/{agent_id}/sops/{sop_id}:rewrite
PUT      /agents/{agent_id}/sops/{sop_id}
PATCH    /agents/{agent_id}/sops/{sop_id}
POST     /sops/{sop_id}:validate
POST     /sops/{sop_id}:publish
GET      /sops/{sop_id}/versions
GET      /sops/{sop_id}/versions/{version}/diff
POST     /sops/{sop_id}/versions/{version}:rollback
```

生成、改写和回滚只产生员工私有草稿。草稿必须显式发布后才能参与意图匹配和执行。

### 知识

```text
GET/POST /agents/{agent_id}/knowledge-bases
PATCH    /agents/{agent_id}/knowledge-bases/{knowledge_base_id}
POST     /agents/{agent_id}/knowledge-bases/{knowledge_base_id}:search
POST     /agents/{agent_id}/knowledge-bases/{knowledge_base_id}/entries
POST     /agents/{agent_id}/knowledge-bases/{knowledge_base_id}/documents
GET      /agents/{agent_id}/knowledge-bases/{knowledge_base_id}/documents
GET      /agents/{agent_id}/knowledge-bases/{knowledge_base_id}/concepts
```

知识检索返回 `citations`。文本批量写入和文件导入返回持久化 Job。

### 通用技能、工具和 MCP

```text
GET/POST /agents/{agent_id}/general-skills
POST     /agents/{agent_id}/general-skills/{slug}:publish
POST     /agents/{agent_id}/general-skills/{slug}:test

GET/POST /agents/{agent_id}/tools
PUT      /agents/{agent_id}/tools/{tool_id}
POST     /agents/{agent_id}/tools/{tool_id}:test

GET/POST /agents/{agent_id}/mcp-servers
POST     /agents/{agent_id}/mcp-servers/{server_id}:discover
POST     /agents/{agent_id}/mcp-servers/{server_id}:sync
```

工具和 MCP 读取响应会掩码认证头、环境变量和连接凭证。

### 定时任务

```text
GET/POST /agents/{agent_id}/scheduled-tasks
PATCH    /agents/{agent_id}/scheduled-tasks/{task_id}
POST     /agents/{agent_id}/scheduled-tasks/{task_id}:run
GET      /agents/{agent_id}/scheduled-tasks/{task_id}/runs
POST     /agents/{agent_id}/scheduled-tasks/{task_id}:pause
POST     /agents/{agent_id}/scheduled-tasks/{task_id}:resume
POST     /agents/{agent_id}/scheduled-tasks/{task_id}:archive
```

定时任务复用与对话相同的 Harness v2 和 SOP-specific 能力判断。

## 通用协议

### 幂等

创建 Run、会话、SOP 草稿和知识导入时应传：

```http
Idempotency-Key: 外部系统生成的唯一请求 ID
```

相同路径和相同内容返回原资源；同一个 Key 携带不同内容返回 `409 IDEMPOTENCY_CONFLICT`。幂等记录默认保留 24 小时。

### 并发更新

员工、会话和 SOP 草稿读取响应包含 `ETag`。更新时必须传：

```http
If-Match: "当前 ETag"
```

缺失返回 428，资源已经变化时返回 412。

### 错误结构

错误统一使用 `application/problem+json`：

```json
{
  "type": "urn:staffdeck:error:validation_error",
  "title": "VALIDATION_ERROR",
  "status": 422,
  "code": "VALIDATION_ERROR",
  "detail": "The request payload is invalid.",
  "request_id": "req_xxx",
  "errors": []
}
```

客户端可以传 `X-Request-ID`；未传时 StaffDeck 自动生成，并在响应头和错误体中返回。

### Webhook 验签

Webhook 请求包含：

```http
X-StaffDeck-Event-ID: evt_xxx
X-StaffDeck-Timestamp: 1785811200
X-StaffDeck-Signature: v1=<hex-digest>
```

签名内容为：

```text
HMAC-SHA256(webhook_secret, timestamp + "." + raw_request_body)
```

接收方应校验时间戳窗口、签名，并以 Event ID 去重。

## 当前版本边界

- 列表响应使用 `data`/`next_cursor` 结构，但部分接口目前仍固定返回 `next_cursor: null`。
- ETag 和幂等优先覆盖核心写入链路，尚未覆盖每一个管理接口。
- v1 不开放模型供应商密钥、渠道机器人凭证、用户账号和数据库管理。
- 删除操作默认归档；不提供不可恢复的物理删除。
- 公共 API 不输出原始模型 COT，只提供可审计的结构化执行事件。
