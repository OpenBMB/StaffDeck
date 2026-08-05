# Resource API reference

## Employee bindings

```text
GET/PUT  /agents/{agent_id}/resources
GET      /agents/{agent_id}/capabilities
```

Resource binding items use `resource_type` (`skill`, `general_skill`, `knowledge_base`, or `tool`), `resource_id`, `status`, and optional metadata.

## Knowledge

```text
GET/POST  /agents/{agent_id}/knowledge-bases
PATCH     /agents/{agent_id}/knowledge-bases/{kb_id}
POST      /agents/{agent_id}/knowledge-bases/{kb_id}:archive
POST      /agents/{agent_id}/knowledge-bases/{kb_id}:search
POST      /agents/{agent_id}/knowledge-bases/{kb_id}/entries
POST      /agents/{agent_id}/knowledge-bases/{kb_id}/documents
GET       /agents/{agent_id}/knowledge-bases/{kb_id}/versions
POST      /agents/{agent_id}/knowledge-bases/{kb_id}:rollback
GET/PATCH /agents/{agent_id}/knowledge-bases/{kb_id}/documents
GET       /agents/{agent_id}/knowledge-bases/{kb_id}/concepts
```

Entry upsert body:

```json
{"entries":[{"external_id":"policy-1","title":"Policy","content":"...","metadata":{}}]}
```

## General skills

```text
GET/POST  /agents/{agent_id}/general-skills
POST      /agents/{agent_id}/general-skills/{slug}:publish
POST      /agents/{agent_id}/general-skills/{slug}:archive
POST      /agents/{agent_id}/general-skills/{slug}:test
```

## Tools and MCP

```text
GET/POST  /agents/{agent_id}/tools
PUT       /agents/{agent_id}/tools/{tool_id}
POST      /agents/{agent_id}/tools/{tool_id}:test
POST      /agents/{agent_id}/tools/{tool_id}:archive
GET/POST  /agents/{agent_id}/mcp-servers
PUT       /agents/{agent_id}/mcp-servers/{server_id}
POST      /agents/{agent_id}/mcp-servers/{server_id}:discover
POST      /agents/{agent_id}/mcp-servers/{server_id}:sync
```

## Scheduled tasks

```text
GET/POST  /agents/{agent_id}/scheduled-tasks
PATCH     /agents/{agent_id}/scheduled-tasks/{task_id}
POST      /agents/{agent_id}/scheduled-tasks/{task_id}:run
GET       /agents/{agent_id}/scheduled-tasks/{task_id}/runs
POST      /agents/{agent_id}/scheduled-tasks/{task_id}:pause
POST      /agents/{agent_id}/scheduled-tasks/{task_id}:resume
POST      /agents/{agent_id}/scheduled-tasks/{task_id}:archive
```

Before constructing complex request bodies, fetch `$STAFFDECK_BASE_URL/openapi.json` and use its current schema as the authority.
