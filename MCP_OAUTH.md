# Remote MCP OAuth (review draft)

This change adds personal authorization to saved Streamable HTTP/SSE MCP servers.
It does not change static header authentication or stdio/builtin transports.

## Setup and use

Set a strong, stable `APP_SECRET` for encrypted SQLite storage. Set `TOOL_BASE_URL`
to the browser-reachable StaffDeck origin (HTTPS in deployments, or an HTTP
loopback URL for local installations). The callback is under
`/api/enterprise/mcp-oauth/callback`; reverse proxies must route this path to the
backend and must not record callback query strings in access logs.

Save a remote server in the tools page, choose **Prepare OAuth authorization**, then
use the visible authorization link in the system browser. A copy-link fallback is
provided for desktop webviews; no automatic popup is required. Return to StaffDeck
to discover/synchronize the tools. Authorization controls are also in server edit.
Each user authorizes separately; sharing a server/tool never shares its token.

The implementation supports protected resource discovery, public dynamic client
registration, authorization code with PKCE S256, exact resource binding, a
single-use state with a ten-minute deadline, encrypted persistent token storage,
expiry-based refresh, and optional provider revocation on disconnect. The OAuth
client name is `StaffDeck`. Metadata, authorization, registration and token
endpoints must use HTTPS. Static pre-registered client configuration is not yet
supported. Providers without DCR or S256 return a configuration error.

Stored credentials are keyed by tenant, user and server. Changing the resource
URL fails closed. No access/refresh token is returned by the status API or written
into shared server headers. Saved discovery, tool sync, MCP Apps resource reads
and tool execution use personal credentials. Harness execution derives the user
from its persisted chat session. Deleting a server removes its local credentials;
remote grants may still need revoking at the provider.

Refresh serialization is **single-process only**. StaffDeck's SQLite runtime lock
already enforces one application process per database. This implementation is not
a distributed refresh lease; multi-worker deployment requires additional work.
Disconnect always removes local credentials and reports whether the provider
confirmed revocation. Providers may keep already-issued access tokens alive.

## Validation boundary

Automated tests cover encryption, tenant/user isolation, resource changes, state
replay, refresh rotation, concurrent refresh, persistent recovery, local disconnect,
provider errors and existing MCP transports. React tests cover visible link,
failure and disconnect states. No real SMS authorization, paid query, desktop
installation, or model conversation is claimed by these tests. In particular,
unexpected provider-side token invalidation (401 before expiry) requires a fresh
authorization; business tool calls are not automatically replayed.

Before leaving draft: validate the tools route with administrator/member roles,
configured callback origin, real provider authorization, and a Harness conversation
in the packaged product. Verify proxy log redaction in the target deployment.
