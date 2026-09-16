# StaffDeck Python SDK and CLI

Independent, synchronous HTTP client for the existing StaffDeck Open API v1.
It lets partners integrate tools, configure SOPs and run digital employees without
importing the backend, accessing its database or modifying its core source code.
Python 3.11+ is required; the only runtime dependency is HTTPX 0.27–0.28.

This is **not** the proposed embedded Agent engine SDK. It requires a running
StaffDeck service. It does not introduce platform features, branding/theme APIs,
new permissions, or compatibility with other products such as PD.

## Install and authenticate

Install from the repository; no public package-index release is assumed:

```sh
python -m pip install ./sdk/python
staffdeck-api --help
# Equivalent entry point:
python -m staffdeck --help
```

The CLI is named `staffdeck-api` so it does not shadow the Linux desktop
launcher's existing `staffdeck setup` command. Prefer an isolated virtualenv.

Create an API key in the platform's API Keys screen and inject it as
`STAFFDECK_API_KEY` using your shell's hidden prompt or a secret manager; do not
put the key in command arguments, source control or logs. Set
`STAFFDECK_BASE_URL=http://localhost:5173/api/v1` for a local service. The CLI
does not read `.env` or persist credentials.

Use an **account key with the necessary role permissions** for tool/SOP
configuration. An employee-scoped key is runtime-only and cannot be used to
manage tools or SOPs, even if a caller requests broader scopes. The server
remains authoritative for tenant, employee boundaries and current user rights;
the SDK never elevates them. Do not send `tenant_id` in request bodies.

## Python quick start

```python
import os
from staffdeck import StaffDeck

with StaffDeck(
    base_url=os.environ["STAFFDECK_BASE_URL"],
    api_key=os.environ["STAFFDECK_API_KEY"],
) as client:
    response = client.agents.list()
    print(response.data)
    print(response.request_id)
```

Every resource method returns `APIResponse(data, status_code, request_id, etag)`
except `runs.events()`, which yields `RunEvent(id, event, data)`. Payloads remain
JSON dictionaries/lists rather than claiming complete generated schemas. This
preserves fields added by compatible servers. List methods return the server's
page; they do **not** silently fetch every resource.

| SDK resource | Supported methods (CLI uses hyphens instead of underscores) |
| --- | --- |
| `agents` | `list`, `get`, `create`, `update`, `capabilities`, `resources`, `set_resources` |
| `tools` | `list`, `create`, `update`, `test` |
| `mcp_servers` | `list`, `create`, `update`, `discover`, `sync` |
| `sops` | `list`, `create`, `get_draft`, `replace`, `patch`, `validate`, `publish`, `versions`, `rollback` |
| `sessions` | `list`, `create`, `get`, `update` |
| `runs` | `create`, `get`, `result`, `cancel`, `wait`, `events` |

For other existing JSON endpoints, use `client.request("GET", "relative/path")`
with `body`, `params`, `if_match` and `idempotency_key` as needed. There are no
specialized generation/rewrite, knowledge, gallery, scheduler, artifact, or
credential-management clients in this first version. Consult the running
server's `/api/v1/docs` and `/api/v1/openapi.json` for its actual contract.

## Tool → SOP → run example

[configure_and_run.py](examples/configure_and_run.py) creates an HTTP tool and
a validated private SOP draft on an **existing development employee**:

```sh
python sdk/python/examples/configure_and_run.py \
  --agent-id YOUR_DEVELOPMENT_AGENT --tool-url https://your-service.example/policy
```

Adding `--publish` explicitly publishes the draft, creates a session and starts
a run. This invokes your configured model/provider and may incur cost or call
the supplied tool. Without the flag it does not publish or run. Each invocation
creates new resources: retain emitted IDs and do not blindly restart the script
after an uncertain write. No sample credentials or deployed test server are used.

SOP `create` and `replace` take the **card itself**; the SDK adds the HTTP
`{"content": card}` wrapper. `patch` takes a JSON Patch array. Updates require
the latest response's `etag` (including quotes); a `412` means re-read and
reconcile, not force-overwrite. Validation is not publication; `rollback`
creates a private draft which must still be validated and explicitly published.

## Asynchronous runs and recovery

```python
from contextlib import closing

# Within an open StaffDeck client; choose a stable key for this one intended run.
receipt = client.runs.create(
    agent_id, {"input": "Look up partner policy"}, idempotency_key="my-business-operation-123",
)
run_id = receipt.data["id"]  # Persist before waiting. HTTP 202 is not completion.
with closing(client.runs.events(run_id, last_event_id=saved_cursor)) as events:
    for event in events:
        process(event)       # Your application logic.
        save_cursor(event.id)  # Checkpoint only after processing; expect replay across crashes.
result = client.runs.wait(run_id, timeout=300, poll_interval=1)
```

`saved_cursor` is `None` on first connection; `agent_id`, `process` and
`save_cursor` are supplied by your application. Streaming is always GET on an
existing run: it never retries the POST that created it. It handles SSE comments,
UTF-8/multiline frames, numeric IDs, deduplication and bounded reconnection
(default two reconnects in total). Persisted `Last-Event-ID` allows continuation
in a later process. An incomplete frame is replayed; malformed protocol data
raises `StreamError` with `run_id` and the last delivered ID. Event frames are
limited to 1,048,576 decoded characters; unfinished lines are bounded while
receiving chunks too. Stop early with `closing` or `.close()`.

Stream EOF is checked against job status and `GET /runs/{id}`'s authoritative
`final_event_id`, not treated as success. Even events named `run.failed` or
`run.cancelled` can be process events, and an empty reconnect can be another proxy
cutoff. Completion requires terminal status and a delivered/resumed cursor equal
to `final_event_id`. A complete initial stream needs no reconnect. Missing events
trigger bounded reconnection; exhaustion raises `StreamError` with the cursor.
Older servers without `final_event_id` (or with a null value) cannot certify
completion and likewise raise `StreamError`, even if all visible events arrived;
upgrade the server to use automatic stream completion. Replay is limited by the
server's event retention window; this check does not restore expired history.
A fully consumed **failed** run is still a valid event stream: check terminal status or
call `wait()` to assert success. `wait()` obtains `/result` only after
`succeeded`; `failed` or `cancelled` raises `RunFailedError`. `WaitTimeout` and
local interruption do not cancel the remote job. Cancellation is explicit and
asynchronous (`runs.cancel` then inspect status).

`wait` defaults to a 300-second polling budget and a one-second interval. Each
request is bounded by the remaining budget, but HTTPX timeouts apply separately
to connect/read/write/pool operations, not a strict wall-clock deadline. Waiting
does not retry an individual failed status request; callers can resume waiting
on the same ID. For SSE, the HTTP read timeout is idle time, not total run time.

## CLI for scripts

Global options precede the resource name. Request JSON comes from a UTF-8 file
or stdin (`--json -`), not an inline command argument:

```sh
staffdeck-api --http-timeout 30 --max-retries 2 agents list --limit 20
staffdeck-api tools create --agent-id "$AGENT_ID" --json tool.json
staffdeck-api sops create --agent-id "$AGENT_ID" --json sop-card.json \
  --idempotency-key my-draft-123
staffdeck-api sops get-draft --agent-id "$AGENT_ID" --sop-id "$SOP_ID" --draft-id "$DRAFT_ID"
staffdeck-api sops patch --agent-id "$AGENT_ID" --sop-id "$SOP_ID" \
  --draft-id "$DRAFT_ID" --if-match "$ETAG" --json patch.json
staffdeck-api sops validate --agent-id "$AGENT_ID" --sop-id "$SOP_ID" --draft-id "$DRAFT_ID"
staffdeck-api sops publish --agent-id "$AGENT_ID" --sop-id "$SOP_ID" --draft-id "$DRAFT_ID"
printf '{"input":"Hello"}' | staffdeck-api runs create --agent-id "$AGENT_ID" --json -
staffdeck-api runs events --run-id "$RUN_ID" --last-event-id "$LAST_EVENT_ID"
staffdeck-api runs wait --run-id "$RUN_ID" --timeout 300 --poll-interval 1
```

Use `staffdeck-api RESOURCE COMMAND --help` for required identifiers and flags.
For `sops create/replace`, `--json` is the card object; for `sops patch` it is
the operation array, and for `agents set-resources` it is the resource array.
Other `--json` arguments are the endpoint's request object. Omitting session
creation's optional `--json` sends `{}`.

Successful commands write one JSON envelope to stdout:

```json
{"data":{"id":"run_example","status":"queued"},"status_code":202,"request_id":"req_example","etag":null}
```

`runs events` instead flushes one `{"id":"1","event":"run.queued","data":{...}}`
object per line (NDJSON). On failure, already emitted events remain on stdout
and one `{"error":{"kind":...,"message":...}}` object goes to stderr.
API errors include HTTP status, a bounded server code and request ID, but not
problem details or raw bodies. There are no tracebacks for expected errors.
Successful payloads may contain business-sensitive data; protect output files.

| Exit code | Meaning |
| --- | --- |
| 0 | Command/stream completed; run creation only means accepted, not succeeded |
| 1 | API rejected the request (includes 401/403/409/412/428) |
| 2 | Invalid arguments, configuration or JSON input |
| 3 | Transport failure; a write may already have been applied |
| 4 | `runs wait` observed failed/cancelled |
| 5 | Local wait budget expired; remote run not cancelled |
| 6 | Protocol or stream error; use the emitted cursor to resume |
| 130 | Local Ctrl-C; remote run not cancelled |

## Transport and compatibility guarantees

Use HTTPS outside local development. The client verifies TLS and does not follow
redirects. The base URL may include a reverse-proxy prefix but must end in
`/api/v1`; a bare server origin also works. API paths are relative to that prefix.

Responses retain the JSON payload, HTTP status, `ETag`, and `X-Request-ID`.
`APIError` retains the server's problem details without printing response bodies
or credentials in its exception message. `TransportError` means no usable HTTP
response was received; for writes, the outcome may be unknown.

Inspect `APIError.code`, `.detail`, `.errors`, `.problem`, `.status_code` and
`.request_id` in trusted application logic. Do not blindly log `.problem` or
`RunFailedError.job`: upstream error details may contain sensitive data.

Only GET requests are retried automatically (at most two retries by default).
Retryable failures are transport errors and HTTP 429/502/503/504, with backoff
and `Retry-After`. Delays exceeding 30 seconds are returned to the caller rather
than retried too early or waited on indefinitely. Authentication/permission
failures, conflicts and validation failures are not retried.
All writes are sent once, **even with an idempotency key**: server-side resource
creation and idempotency recording are not universally atomic. Reconcile an
uncertain write before replaying it. Keys are supplied explicitly by the caller,
are scoped by credential/method/path, and expire (24 hours by default).

The package version is independent of the server version. The tested baseline
is this repository's Open API v1, including the session dependency fix delivered
with this SDK. No compatibility with every historical desktop release is
claimed. Before deploying against another version, run the contract suite and
confirm its routes, key profiles, ETag and event semantics. Branding APIs and
atomic write-idempotency improvements remain core-platform work, not hidden SDK
features. Licensing follows the repository's AGPL-3.0-only license.

## Development and verification

```sh
python -m pip install -e './sdk/python[dev]'
python -m pytest sdk/python/tests
python -m ruff check sdk/python
python -m build sdk/python

# Real FastAPI/auth/storage/worker tests, including local TCP and CLI subprocesses:
python -m pip install -e './backend[dev]'
python -m pytest sdk/python/integration
ULTRARAG_DOTENV=/dev/null DATABASE_URL=sqlite:// \
  python -m pytest backend/tests/test_public_api_v1.py backend/tests/test_public_api_sessions.py
```

Integration fixtures use temporary in-memory SQLite, generated test credentials
and deterministic worker handlers rather than paid/external LLMs. They do not
prove production model/tool behavior. To test a clean wheel installation,
install `sdk/python/dist/*.whl` in a separate virtualenv and set
`STAFFDECK_TEST_PYTHON` to that environment's **absolute** Python path when
running the integration suite. The subprocess then runs without source-path
injection. CI defines Linux/macOS/Windows unit checks on Python 3.11 and 3.14
with HTTPX 0.27.2 and 0.28.1, plus backend and wheel integration checks on Linux.
The workflow validates packages; it does not publish to a registry.
