# Contract: Model Subscription API

All endpoints require the existing authenticated tenant administrator and `tenant_id`. Every status/error payload is sanitized: it must not contain an OAuth token, authorization code, cookie, callback URL, email address or account identifier.

## Subscription account lifecycle

### `GET /api/enterprise/model-configs/codex-subscription/account?tenant_id={tenant_id}`

Returns the local subscription state.

```json
{
  "status": "connected",
  "plan_type": "plus",
  "message": "已连接 ChatGPT 订阅"
}
```

`status` is one of `connected`, `pending`, `requires_login`, or `unavailable`.

### `POST /api/enterprise/model-configs/codex-subscription/login?tenant_id={tenant_id}`

Starts browser authorization locally. The server opens the default browser; the response never includes the authorization URL or login identifier.

### `POST /api/enterprise/model-configs/codex-subscription/login/cancel?tenant_id={tenant_id}`

Cancels the process-local pending login, if one exists, and returns the sanitized account state.

### `POST /api/enterprise/model-configs/codex-subscription/logout?tenant_id={tenant_id}`

Signs the local Codex account out. The UI must require confirmation because this affects the device-level Codex login. Returns sanitized account state.

## Extended model configuration payloads

`POST /api/enterprise/model-configs` and `PUT /api/enterprise/model-configs/{id}` accept and return `auth_mode`.

### API Key model example

```json
{
  "tenant_id": "tenant_demo",
  "name": "Primary API model",
  "auth_mode": "api_key",
  "api_protocol": "openai_responses",
  "base_url": "https://example.invalid/v1",
  "api_key": "secret supplied only on write",
  "model": "gpt-example"
}
```

### Subscription model example

```json
{
  "tenant_id": "tenant_demo",
  "name": "Codex subscription model",
  "auth_mode": "chatgpt_subscription",
  "model": "gpt-5.1-codex"
}
```

For subscription payloads, `api_key`, `base_url`, `api_protocol`, `extra_body`, and `protocol_options` are omitted. The server selects its runtime protocol and rejects conflicting direct-provider credentials.

## Stable error codes

| Code | Meaning |
|---|---|
| `MODEL_SUBSCRIPTION_AUTH_REQUIRED` | No usable local ChatGPT/Codex login exists. |
| `MODEL_SUBSCRIPTION_RUNTIME_UNAVAILABLE` | `codex app-server` is unavailable, incompatible or could not start. |
| `MODEL_SUBSCRIPTION_LOGIN_PENDING` | A browser login is already in progress. |
| `MODEL_SUBSCRIPTION_LOGIN_CANCELLED` | The pending login was cancelled. |
| `MODEL_SUBSCRIPTION_ACCESS_DENIED` | The subscription account cannot use the requested model or has no remaining service access. |
| `MODEL_SUBSCRIPTION_API_KEY_FORBIDDEN` | A subscription payload attempted to provide direct API credentials. |
