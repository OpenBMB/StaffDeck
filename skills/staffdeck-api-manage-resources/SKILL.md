---
name: staffdeck-api-manage-resources
description: Manage StaffDeck employee capability resources through Open API v1, including knowledge bases and cited search, general skills, HTTP tools, MCP servers and discovery, resource bindings, capability snapshots, and scheduled tasks. Use for adding, testing, publishing, binding, diagnosing, or scheduling an employee's executable capabilities.
---

# Manage StaffDeck capability resources

## Inspect before writing

1. Confirm the target `agent_id` and credential write access.
2. Fetch `GET /agents/{agent_id}/resources` and `/capabilities`.
3. Fetch live OpenAPI for payload schemas before creating HTTP tools, MCP servers, or scheduled tasks; those schemas may evolve.
4. Use the route inventory in [references/resources-api.md](references/resources-api.md).

## Apply capability scope

- Use `general` for capabilities the Harness may discover for ordinary tasks.
- Use `sop_specific` when a capability must be hidden unless the current SOP node explicitly references it.
- After creating or publishing a resource, bind it to the employee and re-read `/capabilities` to verify actual executability and any unavailable reason.
- Do not assume a stored resource is executable merely because creation succeeded.

## Knowledge

Prefer knowledge search when answering policy or factual questions and preserve returned citations. Use stable `external_id` values for repeatable entry upserts. Poll persistent Jobs for document ingestion and report failed stage and retryability.

## Skills, tools, and MCP

Create as draft or disabled where supported, test first, then publish or enable. Never place plaintext secrets in skill files or logs; use secret references and verify that read responses remain masked. Run MCP discovery before sync, inspect the discovered tool schemas, and sync only the intended tools.

## Scheduled tasks

Scheduled tasks use the same Harness v2 and SOP-specific rules as chat. Validate the employee's capability snapshot before activating a schedule. Prefer `forbid` concurrency unless overlapping runs are explicitly safe.

## Mutating operations

Use a unique `Idempotency-Key` for retryable creates. Archive rather than physically delete. Do not update a resource owned by another account merely because it is visible through the gallery.
