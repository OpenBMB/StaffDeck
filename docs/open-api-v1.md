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

### 员工设置页创建密钥

员工卡片右上角的“API 密钥”入口提供两种员工级密钥：

| 类型 | 使用场景 | 权限边界 |
| --- | --- | --- |
| 运行密钥 | 对话、任务、会话、Trace 和产物 | 只能运行绑定员工，不能读取完整资源配置 |
| 员工全量密钥（大密钥） | 将一个员工完整接入外部业务系统 | 可读取该员工的 SOP、知识、技能、工具、定时任务、会话和运行结果，并可运行该员工 |

员工全量密钥仍然不是租户管理员密钥：

- 不能访问其他员工；
- 不能新增、修改或发布员工配置；
- 不能读取模型供应商密钥、工具明文凭证或原始模型 COT；
- 不能获取租户级审计和跨员工用量。

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
