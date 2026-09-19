# Frontend contracts and optional enterprise surfaces

Both frontends use the same control-authentication provider and shared Runtime.
Changing the SPA does not change the identity database, runtime service or permission profile.

## Shared contracts

- Login: `{token,user,refresh_mode}`. `refresh_mode=cookie` means refresh credentials
  remain in the HttpOnly `/api/auth` cookie; no refresh token belongs in browser storage.
- Existing clients keep the same login shape. Clients sending
  `X-StaffDeck-Auth-Contract: session-v1` can receive an explicit first-password challenge.
  A challenge is never an authenticated user session.
- Refresh returns the same session contract. Logout revokes the refresh session.
- Resource-directory scopes use the selected Staff source: `mine`, `gallery`,
  `shared`, `managed`, `available`, `visible`. Exact source `allowed_actions` are
  preserved in `metadata.directory_access.allowed_actions`; adapters cannot invent grants.
- Conversation execution/history, UI runtime settings, scheduled work, memory,
  tool execution tests and work records use the existing local Runtime APIs.
- Replaying a rewritten HTTP request body must retain the original ASGI receive
  channel after the first body. This is required for streaming and disconnect handling.

## Optional resource-service contracts

An enabled `module.management` provider can expose `handle_frontend(ManagementRequest)`
for its own `management_domain`. The shared host authenticates the actor, enforces
request size limits and holds a registry lease through response completion/disconnect.
Providers return existing `ServiceResponse` or `StreamingServiceResponse` values.

| Domain | Consumer namespace |
|---|---|
| staff | `/api/agent-control-plane`, selected model/category admin endpoints, `/api/organization` |
| sop | `/api/agent-sops` |
| tool | `/api/agent-tools` |
| skill | `/api/skills` |
| knowledge | `/api/knowledge/v1` |

These optional contracts retain their authoritative service pagination, namespaces,
versions, permissions, conditional headers, multipart bodies, jobs and lifecycle states.
Resource directory aliases and Runtime-owned operations are resolved locally first.
The provider must validate domain/tenant boundaries and forward only to its deployment-owned
origin with the authenticated user's authority. No worker/admin credential substitution
or automatic mutation replay is permitted.

The OSS implementation is not required to implement enterprise-only features.
When unavailable, return HTTP 501 with `detail.code=FEATURE_UNAVAILABLE`.
Never return a successful empty catalog, fabricated permission, fabricated version
or guessed document/job state to make a page render. JSON projection failures are
HTTP 502 `FRONTEND_RESPONSE_INVALID`, including when a prior mutation may have committed.

## Verification

- `tests/test_frontend_contract.py`: shared stream completion, real disconnect propagation,
  optional-feature rejection, exact permissions/query preservation, authentication and route ownership.
- `tests_harness/test_control_auth_refresh.py`: HttpOnly/path-scoped refresh, logout revocation,
  opt-in password challenge and cross-origin rejection.
- Enterprise adapters and their SPA have additional private consumer tests; no enterprise
  implementation, credentials or business data belong in this public repository.
