"""Serve a parallel StaffDeck instance below a prefix, preserving the legacy ASGI app."""

import importlib
import os

import httpx
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, StreamingResponse

HOP_HEADERS = {b"connection", b"keep-alive", b"transfer-encoding", b"upgrade",
               b"proxy-authenticate", b"proxy-authorization", b"te", b"trailer"}


class ParallelGateway:
    def __init__(self, legacy, upstream, prefix="/dsh", client_factory=httpx.AsyncClient):
        self.legacy = legacy
        self.upstream = upstream.rstrip("/")
        self.prefix = prefix.rstrip("/")
        self.client_factory = client_factory

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        selected = path == self.prefix or path.startswith(self.prefix + "/")
        if scope["type"] != "http" or not selected:
            return await self.legacy(scope, receive, send)
        if path == self.prefix:
            return await RedirectResponse(self.prefix + "/", status_code=307)(scope, receive, send)
        request = Request(scope, receive)
        query = scope.get("query_string", b"").decode("ascii")
        target = self.upstream + path[len(self.prefix):] + ("?" + query if query else "")
        client = self.client_factory(timeout=httpx.Timeout(connect=10, read=None, write=600, pool=10))
        headers = [(k, v) for k, v in scope["headers"] if k.lower() not in HOP_HEADERS]
        headers.extend([(b"x-forwarded-prefix", self.prefix.encode()),
                        (b"x-forwarded-proto", scope.get("scheme", "http").encode())])
        try:
            response = await client.send(client.build_request(
                request.method, target, headers=headers, content=request.stream()
            ), stream=True)
        except httpx.HTTPError:
            await client.aclose()
            return await PlainTextResponse("Harness v3 temporarily unavailable", status_code=502)(scope, receive, send)

        async def body():
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()

        result = StreamingResponse(body(), status_code=response.status_code)
        result.raw_headers = []
        for key, value in response.headers.raw:
            key = key.lower()
            if key in HOP_HEADERS:
                continue
            if key == b"location" and value.startswith(b"/") and not value.startswith((b"//", self.prefix.encode() + b"/")):
                value = self.prefix.encode() + value
            if key == b"set-cookie":
                value = value.replace(b"Path=/;", b"Path=" + self.prefix.encode() + b"/;")
                if value.endswith(b"Path=/"):
                    value = value[:-1] + self.prefix.encode() + b"/"
            result.raw_headers.append((key, value))
        result.raw_headers.append((b"x-accel-buffering", b"no"))
        return await result(scope, receive, send)


def create_app():
    legacy = importlib.import_module("single_port_app").app
    return ParallelGateway(legacy, os.environ.get("STAFFDECK_DSH_UPSTREAM", "http://127.0.0.1:10186"))
