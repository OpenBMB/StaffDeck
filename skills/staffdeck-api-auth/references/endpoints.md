# Authentication and agent endpoints

## Common headers

```http
Authorization: Bearer ${STAFFDECK_API_KEY}
Content-Type: application/json
```

Add `Idempotency-Key` to creates and other retryable writes. Never reuse a key with different request content.

## Agents and gallery

```text
GET        /agents
POST       /agents
GET        /agents/{agent_id}
PATCH      /agents/{agent_id}             If-Match required
POST       /agents/{agent_id}:archive
GET        /agents/{agent_id}/capabilities
GET/PUT    /agents/{agent_id}/models
GET/PUT    /agents/{agent_id}/resources
GET        /gallery/agents
GET        /gallery/agents/{agent_id}
POST       /gallery/agents/{agent_id}:add
```

Create a blank agent:

```json
{"name":"Finance assistant","source_mode":"blank","metadata":{}}
```

Create an editable gallery copy:

```json
{"name":"Finance assistant copy","source_mode":"copy","copy_from_agent_id":"agent_xxx"}
```

## API client bootstrap

These routes require a tenant-admin login JWT, not an `sd_live_` key:

```text
GET/POST  /api-clients
PATCH     /api-clients/{client_id}
GET/POST  /api-clients/{client_id}/credentials
POST      /credentials/{credential_id}:rotate
POST      /credentials/{credential_id}:revoke
```

Credential bodies accept `name`, `scopes`, optional `agent_id`, and optional UTC RFC3339 `expires_at`. The plaintext `api_key` is returned only on create or rotate.
