# Run API reference

## Persistent session

```http
POST /agents/{agent_id}/sessions
Idempotency-Key: caller-session-id
```

```json
{
  "external_session_id":"crm-10001",
  "external_user_id":"customer-9001",
  "title":"Expense inquiry",
  "metadata":{"channel":"crm"}
}
```

Session routes:

```text
GET    /agents/{agent_id}/sessions
GET    /agents/{agent_id}/sessions/{session_id}
PATCH  /agents/{agent_id}/sessions/{session_id}  If-Match required
```

## Run

Stateful body:

```json
{"input":"查询差旅费标准","session_id":"session_xxx","session_mode":"stateful","metadata":{}}
```

Stateless body:

```json
{"input":"总结这段内容","session_mode":"stateless","metadata":{}}
```

Routes:

```text
POST  /agents/{agent_id}/runs:stream      creates durable run; returns SSE in the same response
POST  /agents/{agent_id}/runs             returns 202 durable job
GET   /runs/{run_id}
GET   /runs/{run_id}/result               only after succeeded
GET   /runs/{run_id}/events               SSE; supports Last-Event-ID
POST  /runs/{run_id}:cancel
GET   /runs/{run_id}/artifacts
GET   /runs/{run_id}/artifacts/{task_frame_id}?path={encoded_path}
```

The streaming POST exposes its durable Run ID in `X-Run-ID`. Accumulate `run.output.delta`, replace the buffer on `run.output.replace`, and treat `run.output.completed` as the end of visible reply streaming. Continue to use `/result` for the final structured citations, tool calls, task results, session state, and artifacts.

Terminal job states are `succeeded`, `failed`, and `cancelled`. `awaiting_input` requires a new run in the same session. Never assume HTTP 202 means execution succeeded.
