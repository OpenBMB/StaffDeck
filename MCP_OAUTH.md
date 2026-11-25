# Personal OAuth for saved remote MCP servers

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
On an employee’s Tools page, open **Test** for a saved remote MCP tool to
authorize as that employee’s current operator. Members can authorize servers
used by tools they can access in the selected employee scope. Each user
authorizes separately.

The implementation supports protected resource discovery, public dynamic client
registration, authorization code with PKCE S256, exact resource binding, a
single-use state with a ten-minute deadline, encrypted persistent token storage,
expiry-based refresh, and optional provider revocation on disconnect. The OAuth
client name is `StaffDeck`. Metadata, authorization, registration and token
endpoints require direct public HTTPS connectivity. The client checks resolved
addresses, dials a validated public address and verifies TLS for the original
hostname. Personal credentials stay within the configured MCP origin,
including the SSE message endpoint. Static pre-registered client configuration is not yet
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
Disconnect removes local tokens first and records the disconnected user's
state so later calls require fresh authorization. StaffDeck reports whether
the provider confirmed revocation. Providers may keep already-issued access
tokens alive.

## Validation boundary

Automated tests cover encryption, tenant/user isolation, resource changes, state
replay, refresh rotation, concurrent refresh, persistent recovery, local disconnect,
provider errors and existing MCP transports. React tests cover visible link,
failure and disconnect states. No real SMS authorization, paid query, desktop
installation, or model conversation is claimed by these tests. In particular,
unexpected provider-side token invalidation (401 before expiry) requires a fresh
authorization; business tool calls are not automatically replayed.

The two-account regression completes independent administrator and member
OAuth flows against a local provider fixture, then invokes the tools and unlinks
one account. For a new deployment, verify the browser-reachable callback origin
and proxy log redaction.
