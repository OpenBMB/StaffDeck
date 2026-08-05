# SOP API reference

## Draft creation

```text
GET   /agents/{agent_id}/sops
POST  /agents/{agent_id}/sops             body: {"content": SkillCard}
POST  /agents/{agent_id}/sops:generate    returns Job
POST  /agents/{agent_id}/sops/{sop_id}:rewrite  returns Job
GET   /agents/{agent_id}/sops/{sop_id}/drafts/{draft_id}
```

Generate body:

```json
{"title":"Expense policy","raw_content":"source text","business_domain":"finance"}
```

Rewrite body:

```json
{"instruction":"新增经理审批节点","target_paths":["/steps"],"draft_id":"draft_xxx"}
```

## Draft editing and release

```text
PUT    /agents/{agent_id}/sops/{sop_id}?draft_id={draft_id}  If-Match required
PATCH  /agents/{agent_id}/sops/{sop_id}?draft_id={draft_id}  If-Match required
POST   /sops/{sop_id}:validate?agent_id={agent_id}&draft_id={draft_id}
POST   /sops/{sop_id}:publish?agent_id={agent_id} body: {"draft_id":"draft_xxx"}
POST   /sops/{sop_id}:archive?agent_id={agent_id}
```

Example JSON Patch:

```json
[{"op":"replace","path":"/description","value":"Updated description"}]
```

## Versions

```text
GET   /sops/{sop_id}/versions?agent_id={agent_id}
GET   /sops/{sop_id}/versions/{version}?agent_id={agent_id}
GET   /sops/{sop_id}/versions/{version}/diff?agent_id={agent_id}&compare_to={version}
POST  /sops/{sop_id}/versions/{version}:rollback?agent_id={agent_id}
```

Job routes are `GET /jobs/{job_id}`, `GET /jobs/{job_id}/result`, `GET /jobs/{job_id}/events`, and `POST /jobs/{job_id}:cancel`.
