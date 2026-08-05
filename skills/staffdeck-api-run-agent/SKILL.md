---
name: staffdeck-api-run-agent
description: Run a StaffDeck digital employee through Open API v1, create or continue sessions, submit stateful or stateless runs, poll durable jobs, stream SSE events, continue awaiting-input SOPs, cancel work, and download Harness artifacts. Use whenever another agent must invoke a StaffDeck employee and return auditable results with citations and tool activity.
---

# Run a StaffDeck agent

## Prepare

1. Require `STAFFDECK_BASE_URL`, `STAFFDECK_API_KEY`, and an accessible `agent_id`.
2. Read [references/run-api.md](references/run-api.md) for routes and payloads.
3. Generate a unique, stable `Idempotency-Key` for each logical session or run. Reuse it only to retry the identical request.

## Choose session mode

- Use a persistent session when the task may require user clarification, a pending SOP, or follow-up turns.
- Use stateless mode only for an isolated request that must not affect later conversations.
- Reuse the same `session_id` for every continuation of an `awaiting_input` run.

## Execute

1. For a persistent conversation, create or recover a session with `POST /agents/{agent_id}/sessions`. Supply a stable `external_session_id` from the caller's system.
2. For an interactive answer, prefer `POST /agents/{agent_id}/runs:stream` and consume SSE in the same request. Retain the `X-Run-ID` response header.
3. For detached execution, use `POST /agents/{agent_id}/runs`, then consume `GET /runs/{run_id}/events` or poll `GET /runs/{run_id}` with bounded backoff.
4. Stop polling at `succeeded`, `failed`, `cancelled`, or `awaiting_input`.
5. Call `/result` only after `succeeded`.
6. Preserve `reply`, `citations`, `tool_calls`, `task_results`, `session_state`, and `artifacts` in the result passed to the caller.

## Continue or cancel

- On `awaiting_input`, report the requested information and submit the user's answer as a new run in the same session.
- On `ACTION_BUDGET_EXHAUSTED`, preserve the session and continue only when authorized; do not silently loop forever.
- Cancel with `POST /runs/{run_id}:cancel` when the caller requests cancellation or the result is no longer needed.
- Use `Last-Event-ID` when reconnecting SSE.
- Append `run.output.delta` content in order. Replace the accumulated text when `run.output.replace` is emitted, and stop display streaming at `run.output.completed`.

## Evidence and privacy

Treat public Trace as an auditable execution summary, not raw COT. Keep citation metadata attached to the claims it supports. Do not expose masked credentials, sensitive model inputs, or unrelated session data.
