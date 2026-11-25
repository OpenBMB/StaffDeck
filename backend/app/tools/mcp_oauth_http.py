"""Outbound HTTP boundary for personal MCP OAuth."""
from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable

import httpx


def _public(ip: str) -> bool:
    address = ipaddress.ip_address(ip)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


def _origin(url: httpx.URL) -> tuple[str, str, int]:
    return url.scheme, url.host, url.port or 443


def _pin_public_endpoint(request: httpx.Request, *, resource: str | None) -> None:
    url = request.url
    if url.scheme != "https" or not url.host or url.userinfo or url.fragment:
        raise httpx.ConnectError("MCP OAuth requires a public HTTPS endpoint", request=request)
    if resource and _origin(url) != _origin(httpx.URL(resource)):
        raise httpx.ConnectError("MCP authorization cannot cross resource origins", request=request)
    hostname = url.raw_host.decode("ascii")
    try:
        addresses = socket.getaddrinfo(hostname, url.port or 443, type=socket.SOCK_STREAM)
        ips = [info[4][0] for info in addresses]
        if not ips or not all(_public(ip) for ip in ips):
            raise ValueError("non-public peer")
    except (OSError, ValueError) as exc:
        raise httpx.ConnectError("MCP OAuth could not resolve a public endpoint", request=request) from exc
    # Dial the validated address, while preserving the HTTPS identity and Host.
    # A second DNS lookup between validation and connect would bypass the check.
    request.url = url.copy_with(host=ips[0])
    request.headers["Host"] = url.netloc.decode("ascii")
    request.extensions["sni_hostname"] = hostname


def oauth_http_client(
    *, timeout: float | httpx.Timeout = 15, resource: str | None = None,
    admit: Callable[[], None] | None = None,
) -> httpx.Client:
    def check(request: httpx.Request) -> None:
        if admit is not None:
            admit()
        _pin_public_endpoint(request, resource=resource)

    return httpx.Client(
        timeout=timeout, follow_redirects=False, trust_env=False,
        # An IP can serve more than one HTTPS origin. Avoid reusing a TLS
        # connection after replacing the connection-pool key with that IP.
        limits=httpx.Limits(max_keepalive_connections=0),
        event_hooks={"request": [check]},
    )
