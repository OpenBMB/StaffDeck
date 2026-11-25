"""User-scoped authorization-code OAuth for remote MCP servers.

The lock serializes refreshes in one application process. Deployments with multiple
workers must provide a shared refresh lease before enabling this feature.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit

import httpx
from sqlmodel import Session

from app.db.models import MCPServer
from app.tools.mcp_oauth_http import oauth_http_client
from app.tools.mcp_oauth_store import (
    OAuthOwnershipError,
    mark_disconnected,
    load_credentials,
    save_credentials,
)

_LOCK = threading.RLock()


class OAuthError(ValueError):
    pass


def secure_url(value: str, *, callback: bool = False) -> str:
    try:
        if not isinstance(value, str):
            raise ValueError("not a URL")
        p = urlsplit(value)
        _ = p.port
    except ValueError as exc:
        raise OAuthError('Invalid OAuth URL') from exc
    local = callback and p.hostname in {'127.0.0.1', 'localhost', '::1'}
    if (p.scheme != 'https' and not (local and p.scheme == 'http')) or not p.hostname:
        raise OAuthError('OAuth requires HTTPS (HTTP loopback callbacks are allowed)')
    if p.username or p.password or p.fragment:
        raise OAuthError('Invalid OAuth URL')
    return value


def _json(client: httpx.Client, method: str, url: str, **kwargs) -> dict:
    secure_url(url)
    try:
        chunks = bytearray()
        with client.stream(method, url, **kwargs) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                if len(chunks) + len(chunk) > 1_048_576:
                    raise ValueError('OAuth response exceeds size limit')
                chunks.extend(chunk)
        body = json.loads(chunks)
        if not isinstance(body, dict):
            raise TypeError('Invalid JSON object')
        return body
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        # Do not expose provider response bodies or URLs carrying secrets.
        raise OAuthError('OAuth provider request failed') from exc


def prepare(db: Session, tenant: str, user: str, server: str, resource: str,
            redirect: str, client: httpx.Client) -> str:
    secure_url(resource)
    secure_url(redirect, callback=True)
    p = urlsplit(resource)
    origin = f'{p.scheme}://{p.netloc}'
    discovery_url = origin + '/.well-known/oauth-protected-resource'
    # Prefer the resource's actual challenge: deployments may publish metadata
    # below a mounted application prefix, not at the origin root.
    try:
        with client.stream('GET', resource, headers={'Accept': 'application/json, text/event-stream'}) as probe:
            challenge = probe.headers.get('www-authenticate', '')
    except httpx.HTTPError as exc:
        raise OAuthError('MCP resource discovery failed') from exc
    advertised = re.search(r'\bresource_metadata="([^"]+)"', challenge, re.IGNORECASE)
    metadata_url = secure_url(advertised.group(1)) if advertised else discovery_url + p.path
    try:
        prm = _json(client, 'GET', metadata_url)
    except OAuthError as exc:
        cause = exc.__cause__
        if not isinstance(cause, httpx.HTTPStatusError) or cause.response.status_code != 404:
            raise
        prm = _json(client, 'GET', discovery_url)
    if prm.get('resource') != resource:
        raise OAuthError('Protected resource metadata does not match MCP endpoint')
    issuers = prm.get('authorization_servers')
    if not isinstance(issuers, list) or not issuers:
        raise OAuthError('MCP server did not advertise an authorization server')
    issuer = secure_url(issuers[0])
    ip = urlsplit(issuer)
    if ip.query:
        raise OAuthError('Authorization issuer must not have a query')
    metadata_url = f'{ip.scheme}://{ip.netloc}/.well-known/oauth-authorization-server{ip.path}'
    metadata = _json(client, 'GET', metadata_url)
    if metadata.get('issuer') != issuer:
        raise OAuthError('Authorization issuer mismatch')
    methods = metadata.get('code_challenge_methods_supported') or []
    if not isinstance(methods, list) or 'S256' not in methods:
        raise OAuthError('Authorization server must support PKCE S256')
    authorization = secure_url(metadata.get('authorization_endpoint', ''))
    token_endpoint = secure_url(metadata.get('token_endpoint', ''))
    registration = secure_url(metadata.get('registration_endpoint', ''))
    info = _json(client, 'POST', registration, json={
        'client_name': 'StaffDeck', 'redirect_uris': [redirect],
        'grant_types': ['authorization_code', 'refresh_token'],
        'response_types': ['code'], 'token_endpoint_auth_method': 'none',
    })
    if (not isinstance(info.get('client_id'), str) or not info['client_id']
            or info.get('token_endpoint_auth_method', 'none') != 'none'):
        raise OAuthError('Provider must support public PKCE clients')
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    pkce_challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    with _LOCK:
        try:
            old = load_credentials(db, tenant, user, server) or {}
        except OAuthOwnershipError:
            # Replace stale endpoint credentials only once discovery succeeds.
            old = {}
        old['pending'] = {
            'state': state, 'verifier': verifier, 'redirect': redirect,
            'deadline': time.time() + 600, 'client_id': info['client_id'],
            'token_endpoint': token_endpoint, 'issuer': issuer,
            'revocation_endpoint': secure_url(metadata['revocation_endpoint'])
            if metadata.get('revocation_endpoint') else None,
            'require_issuer': metadata.get('authorization_response_iss_parameter_supported') is True,
        }
        save_credentials(db, tenant, user, server, resource, old)
    # Request the scopes demanded by the challenge, or let the provider choose
    # its defaults. scopes_supported is a capability list, not a request.
    scopes = re.search(r'\bscope="([^"]+)"', challenge, re.IGNORECASE)
    return authorization + ('&' if '?' in authorization else '?') + urlencode({
        'response_type': 'code', 'client_id': info['client_id'], 'redirect_uri': redirect,
        'code_challenge': pkce_challenge, 'code_challenge_method': 'S256',
        'state': state, 'resource': resource,
        **({'scope': scopes.group(1)} if scopes else {}),
    })


def _tokens(payload: dict, previous: dict | None = None) -> dict:
    access = payload.get('access_token')
    refresh = payload.get('refresh_token') or (previous or {}).get('refresh_token')
    if (not isinstance(access, str) or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", access)
            or str(payload.get('token_type', '')).lower() != 'bearer'
            or (refresh is not None and not isinstance(refresh, str))):
        raise OAuthError('Invalid OAuth token response')
    try:
        duration = float(payload.get('expires_in', 3600))
        if not math.isfinite(duration) or duration < 0:
            raise ValueError('invalid expiry')
    except (ValueError, TypeError, OverflowError) as exc:
        raise OAuthError('Invalid OAuth token expiry') from exc
    return {'access_token': access, 'refresh_token': refresh, 'expires_at': time.time() + duration}


def complete(db: Session, tenant: str, user: str, server: str, resource: str,
             state: str, code: str, client: httpx.Client, issuer: str | None = None) -> None:
    with _LOCK:
        data = load_credentials(db, tenant, user, server) or {}
        pending = data.get('pending', {})
        if not state or not secrets.compare_digest(state.encode(), pending.get('state', '').encode()):
            raise OAuthError('Invalid authorization state')
        if pending.get('deadline', 0) < time.time() or not code:
            raise OAuthError('Authorization expired or cancelled')
        if (issuer is not None or pending.get('require_issuer')) and issuer != pending['issuer']:
            raise OAuthError('Authorization response issuer mismatch')
        # Consume state before exchanging the one-use code, including failure paths.
        data.pop('pending', None)
        save_credentials(db, tenant, user, server, resource, data)
        tokens = _json(client, 'POST', pending['token_endpoint'], data={
            'grant_type': 'authorization_code', 'code': code,
            'code_verifier': pending['verifier'], 'redirect_uri': pending['redirect'],
            'client_id': pending['client_id'], 'resource': resource,
        })
        data = {k: v for k, v in pending.items() if k in {
            'client_id', 'token_endpoint', 'issuer', 'revocation_endpoint',
        }}
        data['tokens'] = _tokens(tokens)
        save_credentials(db, tenant, user, server, resource, data)


def bearer(db: Session, tenant: str, user: str, server: str, resource: str,
           client: httpx.Client) -> str | None:
    with _LOCK:
        row = db.get(MCPServer, server, populate_existing=True)
        if row is None or row.url != resource:
            raise OAuthError('MCP resource mismatch; credentials were not attached')
        data = load_credentials(db, tenant, user, server)
        if data is None:
            return None
        tokens = data.get('tokens')
        if not tokens:
            raise OAuthError('MCP authorization required')
        if tokens.get('expires_at', 0) <= time.time() + 30:
            if not tokens.get('refresh_token'):
                raise OAuthError('MCP authorization expired; authorize again')
            result = _json(client, 'POST', data['token_endpoint'], data={
                'grant_type': 'refresh_token', 'refresh_token': tokens['refresh_token'],
                'client_id': data['client_id'], 'resource': resource,
            })
            tokens = _tokens(result, tokens)
            data['tokens'] = tokens
            save_credentials(db, tenant, user, server, resource, data)
        return str(tokens['access_token'])


def authorized_config(db: Session, tenant: str, user: str | None, server: str,
                      config: dict) -> dict:
    """Attach credentials only to a trusted user's original resource endpoint."""
    if not user or config.get("transport") not in {"streamable_http", "sse"}:
        return config
    resource = str(config.get('url', ''))
    with oauth_http_client() as client:
        token = bearer(db, tenant, user, server, resource, client)
    if token is None:
        return config
    result = dict(config)
    headers = {k: v for k, v in config.get('headers', {}).items()
               if k.lower() != 'authorization'}
    headers['Authorization'] = f'Bearer {token}'
    result['headers'] = headers

    def admit() -> None:
        # Authorization may be removed between opening a stream and invoking a
        # tool. Recheck before every new outbound request.
        with _LOCK:
            current = load_credentials(db, tenant, user, server) or {}
            if (current.get('tokens') or {}).get('access_token') != token:
                raise OAuthError('MCP authorization changed; authorize again')

    result['_oauth_admit'] = admit
    return result


def disconnect(db: Session, tenant: str, user: str, server: str,
               client: httpx.Client) -> bool:
    """Always remove local credentials, even when remote revocation is unavailable."""
    with _LOCK:
        try:
            data = load_credentials(db, tenant, user, server) or {}
        except OAuthOwnershipError:
            data = {}
        # Retain only a marker so this user cannot fall back to shared headers.
        mark_disconnected(db, tenant, user, server)
    endpoint = data.get('revocation_endpoint')
    tokens = data.get('tokens', {})
    token = tokens.get('refresh_token') or tokens.get('access_token')
    if not endpoint or not token:
        return False
    try:
        secure_url(endpoint)
        with client.stream('POST', endpoint, data={
            'token': token, 'client_id': data['client_id'],
            'token_type_hint': 'refresh_token' if tokens.get('refresh_token') else 'access_token',
        }) as response:
            response.raise_for_status()
    except (httpx.HTTPError, OAuthError):
        return False
    return True
