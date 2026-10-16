"""JSON-only HTTP boundary to the independent Runtime."""
from __future__ import annotations
import json
from urllib.request import Request, urlopen
from urllib.error import HTTPError


class RuntimeError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class RuntimeClient:
    def __init__(self, endpoint, *, publish_token=None, timeout=30, transport=None):
        self.endpoint, self.publish_token, self.timeout = endpoint.rstrip("/"), publish_token, timeout
        self.transport = transport

    def request(self, path, payload=None):
        headers = {"Content-Type": "application/json"}
        if path == "/v1/definitions" and self.publish_token:
            headers["Authorization"] = f"Bearer {self.publish_token}"
        if self.transport:
            response = self.transport(path, payload, headers)
        else:
            request = Request(self.endpoint + path, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
            try:
                with urlopen(request, timeout=self.timeout) as result:
                    response = json.load(result)
            except HTTPError as error:
                response = json.load(error)
        if response.get("ok") is False:
            error = response.get("error") or {}
            raise RuntimeError(error.get("code", "SOP_RUNTIME_ERROR"), error.get("message", "Runtime rejected request"))
        return response.get("payload", response)

    def session(self, session_id):
        from urllib.parse import quote
        return self.request(f"/v1/sessions/{quote(session_id, safe='')}")

    def operation(self, session_id, operation, *, request_id, turn_id, revision=None, **payload):
        from urllib.parse import quote
        return self.request(f"/v1/sessions/{quote(session_id, safe='')}/{operation}", {
            "protocolVersion": "3.0", "requestId": request_id, "operationId": f"sop.{operation}:{request_id}",
            "turnId": turn_id, **({"expectedRevision": revision} if revision is not None else {}), **payload})
