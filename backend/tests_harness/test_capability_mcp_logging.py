"""The in-process capability MCP server must not reconfigure the host process's logging.

``uvicorn.Config`` applies ``log_config``/``log_level``/``access_log`` to the process-global
``uvicorn.*`` loggers. Starting the capability server used to silence the main API server's
access log and startup lines for the rest of the process's life.
"""

from __future__ import annotations

import logging
import threading

from staffdeck_harness.bridge.capability_mcp import ActivationRegistry, CapabilityMcpServer


def _uvicorn_logging_state():
    out = {}
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi"):
        lg = logging.getLogger(name)
        out[name] = (lg.level, list(lg.handlers), lg.propagate)
    return out


def test_starting_the_capability_server_leaves_global_uvicorn_logging_alone():
    access = logging.getLogger("uvicorn.access")
    marker = logging.StreamHandler()
    access.addHandler(marker)  # stands in for the main server's access handler
    try:
        before = _uvicorn_logging_state()
        server = CapabilityMcpServer(ActivationRegistry())
        server.start()
        try:
            assert _uvicorn_logging_state() == before, "levels, handlers and propagation are untouched"
            assert marker in access.handlers and access.propagate is before["uvicorn.access"][2]
            flt = server._access_filter
            assert flt in access.filters, "the server's own request lines are filtered, not the logger reconfigured"
            own = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "%s", ("x",), None)
            own.thread = server._thread.ident
            other = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "%s", ("y",), None)
            other.thread = threading.get_ident()
            assert flt.filter(own) is False and flt.filter(other) is True
        finally:
            server.stop()
        assert server._access_filter is None and all(f is not flt for f in access.filters), "stop() removes the filter"
        assert _uvicorn_logging_state() == before
    finally:
        access.removeHandler(marker)
