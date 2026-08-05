---
name: staffdeck-api-auth
description: Authenticate to StaffDeck Open API v1, inspect the current credential boundary, list accessible agents, browse the gallery, add a gallery agent, or bootstrap API clients and credentials. Use for StaffDeck API key setup, authorization failures, agent selection, scope checks, and cross-agent access questions.
---

# StaffDeck API authentication

Use the API credential supplied by the user. Never print, persist, commit, or place a plaintext key in a command argument when an environment variable or secret store is available.

## Establish the endpoint

1. Use `STAFFDECK_BASE_URL`; default to `http://39.102.210.77:10087/api/v1` only when the user has not supplied an environment.
2. Use `STAFFDECK_API_KEY` for `Authorization: Bearer ...`.
3. Fetch `$STAFFDECK_BASE_URL/openapi.json` before using an endpoint not listed in [references/endpoints.md](references/endpoints.md).
4. Do not send `tenant_id`; the server derives tenant, user, scopes, and employee boundary from the credential.

## Select an agent

1. Call `GET /agents` to list agents visible to the current account.
2. If the requested agent is not present, call `GET /gallery/agents`.
3. Add an existing gallery agent with `POST /gallery/agents/{agent_id}:add` and an `Idempotency-Key`.
4. Copy a gallery agent only when an independently editable copy is required: `POST /agents` with `source_mode=copy` and `copy_from_agent_id`.
5. Confirm the selected `agent_id` before performing writes.

## Respect credential boundaries

- Treat an account-wide key as the current account, not as a tenant superuser. Its access changes with the account's role and ownership.
- Treat an agent runtime key as restricted to its bound agent. Do not attempt to enumerate or operate other agents.
- Do not request model provider secrets, plaintext tool credentials, raw COT, user management, or database administration through v1.
- On `401`, verify key format and revocation. On `403`, inspect required scopes and agent ownership. On `404`, do not infer whether an inaccessible cross-tenant resource exists.

## Bootstrap only when authorized

Use `POST /api-clients` and `/api-clients/{client_id}/credentials` only with a tenant-admin login JWT and explicit authorization to create credentials. Return the new plaintext key once and instruct the caller to store it immediately.

For request examples and scope-sensitive routes, read [references/endpoints.md](references/endpoints.md).
