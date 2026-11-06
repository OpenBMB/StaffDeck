"""Credential-free cause classification for SDK-wrapped connection failures."""
from __future__ import annotations

import errno
import ssl


def connection_diagnostic(exc: BaseException) -> dict[str, object]:
    chain, seen, codes = [], set(), []
    current = exc
    category = "MODEL_CONNECTION_FAILED"
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        name = type(current).__name__
        chain.append(name)
        number = getattr(current, "errno", None)
        if isinstance(number, int):
            codes.append(number)
        if isinstance(current, ssl.SSLError):
            category = "MODEL_TLS_FAILED"
        elif name == "gaierror":
            category = "MODEL_DNS_FAILED"
        elif name == "ProxyError":
            category = "MODEL_PROXY_FAILED"
        elif number == errno.ECONNREFUSED:
            category = "MODEL_CONNECTION_REFUSED"
        elif number in {errno.EMFILE, errno.ENFILE}:
            category = "MODEL_TRANSPORT_RESOURCES_EXHAUSTED"
        elif "Timeout" in name:
            category = "MODEL_CONNECTION_TIMEOUT"
        current = current.__cause__ or current.__context__
    # Only class names and errno values are exposed, never URLs, headers or exception bodies.
    return {"connection_code": category, "cause_types": chain, "errno": codes}


def provider_error_message(exc: Exception) -> str:
    from app.llm.protocol_drivers import _redact_sensitive_text
    message = _redact_sensitive_text(str(exc))[:350]
    info = connection_diagnostic(exc)
    if any("Connect" in name or "Timeout" in name or "SSL" in name or name == "ProxyError"
           for name in info["cause_types"]):
        message += f" [{info['connection_code']}; causes={' -> '.join(info['cause_types'])}]"
    return message
