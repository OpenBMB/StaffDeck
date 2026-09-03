"""CapabilityCallbackPort realized as a local MCP server.

DSH's SDK protocol has no client-callable request channel (server→client
requests are a "dead capability" in 0.1.2), but DSH mounts external MCP
servers natively (``@deepseek-ai/dsh-mcp-client``) and their tools appear to
the model like any other. So the Bridge exposes the ``CapabilityHost`` proxy
tools **as an MCP server over Streamable HTTP**, and the DSH profile patch
mounts it. Every model tool call therefore travels:

    DSH tools/pre-execute → mcp__staffdeck__<proxy> → this server → CapabilityHost
        → activation fence → ledger → Guarded Facade (PEP) → legacy service

The server is multi-tenant by *activation*: each turn registers an
``ActivationSlot`` under an opaque token, DSH is told the token through the
MCP connection headers, and every tool call resolves its slot from that
header. A call without a live token is refused (``ACTIVATION_FENCED``).

The server runs in-process on a background thread (uvicorn on 127.0.0.1) so
no extra deployable is introduced for OSS; a Business deployment may run it
as its own service because it depends only on the Module SDK, the security
profile and the legacy ORM.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import mcp.types as mt
import uvicorn
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from starlette.requests import Request

from staffdeck_dsh.capabilities.host import CapabilityHost
from staffdeck_dsh.contracts.invocation import InvocationContext

logger = logging.getLogger(__name__)

ACTIVATION_HEADER = "x-staffdeck-activation"
SERVER_NAME = "staffdeck"


@dataclass
class Activation:
    token: str
    host: CapabilityHost
    context_factory: Callable[[str], InvocationContext]   # trace_id -> ctx
    created_at: float = field(default_factory=time.monotonic)
    calls: int = 0


class ActivationRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, Activation] = {}

    def register(self, host: CapabilityHost, context_factory: Callable[[str], InvocationContext]) -> Activation:
        token = secrets.token_urlsafe(24)
        act = Activation(token=token, host=host, context_factory=context_factory)
        with self._lock:
            self._items[token] = act
        return act

    def release(self, token: str) -> None:
        with self._lock:
            self._items.pop(token, None)

    def get(self, token: str | None) -> Activation | None:
        if not token:
            return None
        with self._lock:
            return self._items.get(token)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


def _text(payload: Any) -> list[mt.TextContent]:
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    return [mt.TextContent(type="text", text=text)]


class CapabilityMcpServer:
    def __init__(self, registry: ActivationRegistry, *, host: str = "127.0.0.1", port: int = 0):
        self.registry = registry
        self.host = host
        self.port = port
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self.mcp = Server(SERVER_NAME, on_list_tools=self._list_tools, on_call_tool=self._call_tool)

    # -- MCP handlers -------------------------------------------------------------

    def _activation(self, ctx: ServerRequestContext) -> Activation | None:
        request = ctx.request
        token = None
        if isinstance(request, Request):
            token = request.headers.get(ACTIVATION_HEADER)
        if token is None and ctx.meta is not None:
            extra = getattr(ctx.meta, "model_extra", None) or {}
            token = extra.get(ACTIVATION_HEADER)
        return self.registry.get(token)

    async def _list_tools(self, ctx: ServerRequestContext, params: Any) -> mt.ListToolsResult:
        act = self._activation(ctx)
        if act is None:
            return mt.ListToolsResult(tools=[])
        tools = [mt.Tool(name=s["name"], description=s["description"], inputSchema=s["parameters"]) for s in act.host.tool_schemas()]
        return mt.ListToolsResult(tools=tools)

    async def _call_tool(self, ctx: ServerRequestContext, params: mt.CallToolRequestParams) -> mt.CallToolResult:
        act = self._activation(ctx)
        if act is None:
            return mt.CallToolResult(content=_text({"success": False, "error": {"code": "ACTIVATION_FENCED", "message": "no live activation for this connection"}}), isError=True)
        act.calls += 1
        trace_id = f"dshcall_{act.calls}_{secrets.token_hex(4)}"
        inv_ctx = act.context_factory(trace_id)
        loop = asyncio.get_running_loop()
        # The CapabilityHost is synchronous (SQLModel session); keep it off the event loop.
        result, receipt = await loop.run_in_executor(None, act.host.invoke_proxy, params.name, dict(params.arguments or {}), inv_ctx)
        payload: dict[str, Any] = {"success": result.success}
        if result.success:
            payload["data"] = result.data
        else:
            payload["error"] = result.error
        if result.citations:
            payload["citations"] = list(result.citations)
        if result.artifacts:
            payload["artifacts"] = list(result.artifacts)
        if receipt is not None:
            payload["receipt"] = {"invocation_id": receipt.invocation_id, "status": receipt.status, "replayed_from": receipt.replayed_from}
        return mt.CallToolResult(content=_text(payload), isError=not result.success, structuredContent=payload if isinstance(payload, dict) else None)

    # -- lifecycle -----------------------------------------------------------------

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/mcp"

    @property
    def model_base_url(self) -> str:
        """OpenAI-compatible base for the subprocess (``/chat/completions`` is appended by DSH)."""

        return f"http://{self.host}:{self.port}/v1"

    def start(self) -> None:
        if self._thread is not None:
            return
        app = self.mcp.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True, host=self.host)
        # Model calls from the DSH subprocess come back here too: same port, activation token as API key.
        from staffdeck_dsh.bridge.model_gateway import ModelGateway

        ModelGateway(self.registry).mount(app)
        config = uvicorn.Config(app, host=self.host, port=self.port, log_level="warning", access_log=False)
        self._server = uvicorn.Server(config)

        def run() -> None:
            asyncio.run(self._serve())

        self._thread = threading.Thread(target=run, name="staffdeck-dsh-capability-mcp", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=15):
            raise RuntimeError("capability MCP server did not start")

    async def _serve(self) -> None:
        assert self._server is not None
        server = self._server
        config = server.config
        config.load()
        server.lifespan = config.lifespan_class(config)
        await server.startup()
        for s in server.servers:
            for sock in s.sockets:
                self.port = sock.getsockname()[1]
        self._ready.set()
        await server.main_loop()
        await server.shutdown()

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None
