"""User-scoped authorization-code OAuth for remote MCP servers.

The lock serializes refreshes in one application process. Deployments with multiple
workers must provide a shared refresh lease before enabling this feature.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit

import httpx
from sqlmodel import Session

from app.db.models import MCPServer
from app.tools.mcp_oauth_store import delete_credentials, load_credentials, save_credentials

_LOCK = threading.RLock()


class OAuthError(ValueError):
    pass


def secure_url(value: str, *, callback: bool = False) -> str:
    p = urlsplit(value)
    local = callback and p.hostname in {'127.0.0.1', 'localhost', '::1'}
    if (p.scheme != 'https' and not (local and p.scheme == 'http')) or not p.hostname:
        raise OAuthError('OAuth requires HTTPS (HTTP loopback callbacks are allowed)')
    if p.username or p.password or p.fragment:
        raise OAuthError('Invalid OAuth URL')
    return value


def _json(client: httpx.Client, method: str, url: str, **kwargs) -> dict:
    secure_url(url)
    try:
        response = client.request(method, url, **kwargs)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise TypeError('Invalid JSON object')
        return body
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        # Do not expose provider response bodies or URLs carrying secrets.
        raise OAuthError('OAuth provider request failed') from exc


def prepare(db: Session, tenant: str, user: str, server: str, resource: str,
            redirect: str, client: httpx.Client) -> str:
    load_credentials(db, tenant, user, server)
    secure_url(resource)
    secure_url(redirect, callback=True)
    p = urlsplit(resource)
    origin = f'{p.scheme}://{p.netloc}'
    discovery_url = origin + '/.well-known/oauth-protected-resource'
    try:
        prm = _json(client, 'GET', discovery_url + p.path)
    except OAuthError as exc:
        cause = exc.__cause__
        if not isinstance(cause, httpx.HTTPStatusError) or cause.response.status_code != 404:
            raise
        prm = _json(client, 'GET', discovery_url)
    if prm.get('resource') != resource:
        raise OAuthError('Protected resource metadata does not match MCP endpoint')
    issuers = prm.get('authorization_servers') or []
    if not issuers:
        raise OAuthError('MCP server did not advertise an authorization server')
    issuer = secure_url(issuers[0]).rstrip('/')
    ip = urlsplit(issuer)
    metadata_url = f'{ip.scheme}://{ip.netloc}/.well-known/oauth-authorization-server{ip.path}'
    metadata = _json(client, 'GET', metadata_url)
    if metadata.get('issuer', '').rstrip('/') != issuer:
        raise OAuthError('Authorization issuer mismatch')
    if 'S256' not in metadata.get('code_challenge_methods_supported', []):
        raise OAuthError('Authorization server must support PKCE S256')
    authorization = secure_url(metadata.get('authorization_endpoint', ''))
    token_endpoint = secure_url(metadata.get('token_endpoint', ''))
    registration = secure_url(metadata.get('registration_endpoint', ''))
    info = _json(client, 'POST', registration, json={
        'client_name': 'StaffDeck', 'redirect_uris': [redirect],
        'grant_types': ['authorization_code', 'refresh_token'],
        'response_types': ['code'], 'token_endpoint_auth_method': 'none',
    })
    if not info.get('client_id') or info.get('token_endpoint_auth_method', 'none') != 'none':
        raise OAuthError('Provider must support public PKCE clients')
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    with _LOCK:
        old = load_credentials(db, tenant, user, server) or {}
        old['pending'] = {
            'state': state, 'verifier': verifier, 'redirect': redirect,
            'deadline': time.time() + 600, 'client_id': info['client_id'],
            'token_endpoint': token_endpoint, 'issuer': issuer,
            'revocation_endpoint': metadata.get('revocation_endpoint'),
        }
        save_credentials(db, tenant, user, server, resource, old)
    return authorization + ('&' if '?' in authorization else '?') + urlencode({
        'response_type': 'code', 'client_id': info['client_id'], 'redirect_uri': redirect,
        'code_challenge': challenge, 'code_challenge_method': 'S256',
        'state': state, 'resource': resource,
        'scope': ' '.join(prm.get('scopes_supported', [])),
    })


def _tokens(payload: dict, previous: dict | None = None) -> dict:
    if not payload.get('access_token') or str(payload.get('token_type', '')).lower() != 'bearer':
        raise OAuthError('Invalid OAuth token response')
    result = dict(payload)
    if previous and not result.get('refresh_token'):
        result['refresh_token'] = previous.get('refresh_token')
    result['expires_at'] = time.time() + float(payload.get('expires_in', 3600))
    return result


def complete(db: Session, tenant: str, user: str, server: str, resource: str,
             state: str, code: str, client: httpx.Client, issuer: str | None = None) -> None:
    with _LOCK:
        data = load_credentials(db, tenant, user, server) or {}
        pending = data.get('pending', {})
        if not state or not secrets.compare_digest(state, pending.get('state', '')):
            raise OAuthError('Invalid authorization state')
        if pending.get('deadline', 0) < time.time() or not code:
            raise OAuthError('Authorization expired or cancelled')
        if issuer is not None and issuer.rstrip('/') != pending['issuer']:
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
        # Expire cached ORM objects before reading another thread's refreshed token.
        db.expire_all()
        row = db.get(MCPServer, server)
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
    with httpx.Client(timeout=15, follow_redirects=False) as client:
        token = bearer(db, tenant, user, server, str(config.get('url', '')), client)
    if token is None:
        return config
    result = dict(config)
    headers = {k: v for k, v in config.get('headers', {}).items()
               if k.lower() != 'authorization'}
    headers['Authorization'] = f'Bearer {token}'
    result['headers'] = headers
    return result


def disconnect(db: Session, tenant: str, user: str, server: str,
               client: httpx.Client) -> bool:
    """Always remove local credentials, even when remote revocation is unavailable."""
    with _LOCK:
        data = load_credentials(db, tenant, user, server) or {}
        confirmed = False
        endpoint = data.get('revocation_endpoint')
        tokens = data.get('tokens', {})
        token = tokens.get('refresh_token') or tokens.get('access_token')
        if endpoint and token:
            try:
                secure_url(endpoint)
                response = client.post(endpoint, data={
                    'token': token, 'client_id': data['client_id'],
                    'token_type_hint': 'refresh_token' if tokens.get('refresh_token') else 'access_token',
                })
                response.raise_for_status()
                confirmed = True
            except (httpx.HTTPError, OAuthError):
                pass  # Local disconnect must not depend on provider availability.
        delete_credentials(db, tenant, user, server)
        return confirmed
