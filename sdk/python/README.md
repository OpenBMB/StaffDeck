# StaffDeck Python SDK

An independent HTTP client for an existing StaffDeck Open API v1 service.
Python 3.11+ is required. This package does not embed the Agent engine or access
the platform database. Install from this repository with:

```sh
python -m pip install ./sdk/python
```

```python
import os
from staffdeck import StaffDeck

with StaffDeck(
    base_url="http://localhost:5173/api/v1",
    api_key=os.environ["STAFFDECK_API_KEY"],
) as client:
    response = client.request("GET", "agents")
    print(response.data)
    print(response.request_id)
```

Use HTTPS outside local development. The client verifies TLS and does not follow
redirects. The base URL may include a reverse-proxy prefix but must end in
`/api/v1`; a bare server origin also works. API paths are relative to that prefix.

Responses retain the JSON payload, HTTP status, `ETag`, and `X-Request-ID`.
`APIError` retains the server's problem details without printing response bodies
or credentials in its exception message. `TransportError` means no usable HTTP
response was received; for writes, the outcome may be unknown.

Only GET requests are retried automatically (at most two retries by default).
All writes are sent once, **even with an idempotency key**: server-side resource
creation and idempotency recording are not universally atomic. Reconcile an
uncertain write before replaying it. Keys are supplied explicitly by the caller,
are scoped by credential/method/path, and expire (24 hours by default).

```sh
python -m pip install -e './sdk/python[dev]'
python -m pytest sdk/python/tests
python -m ruff check sdk/python
```
