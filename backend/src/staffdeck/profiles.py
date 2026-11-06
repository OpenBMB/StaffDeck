"""Local account login; passwords and browser tokens are never persisted."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from .client import StaffDeck, _base_url
from .errors import APIError, ProtocolError, TransportError


def connection_url(value: str) -> str:
    url = _base_url(value)
    if url.scheme == "http":
        try:
            local = ipaddress.ip_address(url.host).is_loopback
        except ValueError:
            local = url.host == "localhost"
        if not local:
            raise ValueError("Remote login requires HTTPS.")
    return str(url).rstrip("/")


class ProfileStore:
    def __init__(self, name: str = "local") -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", name):
            raise ValueError("Invalid profile name.")
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        self.root = Path(os.environ.get("STAFFDECK_CONFIG_DIR", str(base / "staffdeck/cli")))
        self.path = self.root / f"{name}.json"

    def _directory(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or (
            os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077)
        ):
            raise ValueError("CLI config directory must be private and user-owned.")

    def load(self) -> dict | None:
        if not self.root.exists():
            return None
        self._directory()
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "r", encoding="utf-8") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or (
                os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077)
            ):
                raise ValueError("CLI profile must be a private user-owned file.")
            value = json.load(source)
        if not isinstance(value, dict) or any(
            not isinstance(value.get(key), str) or not value[key]
            for key in ("base_url", "api_key", "credential_id")
        ):
            raise ValueError("Invalid CLI profile.")
        value["base_url"] = connection_url(value["base_url"])
        return value

    def save_new(self, value: dict) -> None:
        self._directory()
        fd, temp = tempfile.mkstemp(prefix=".login-", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as target:
                json.dump(value, target, ensure_ascii=False)
                target.flush()
                os.fsync(target.fileno())
            # Atomic publication without overwriting a concurrent login/profile.
            os.link(temp, self.path)
        finally:
            os.unlink(temp)

    def remove(self) -> None:
        self.path.unlink(missing_ok=True)


def local_login(
    base_url: str, tenant_id: str, username: str, password: str,
    *, timeout: float = 30, transport: httpx.BaseTransport | None = None,
) -> dict:
    base_url = connection_url(base_url)
    if not tenant_id or not username or not password:
        raise ValueError("Tenant, username and password are required.")
    # Preserve a reverse-proxy prefix, e.g. /test/api/v1 -> /test/api/auth/.
    auth_url = base_url.removesuffix("/api/v1") + "/api/auth/"
    try:
        with httpx.Client(base_url=auth_url, timeout=timeout, follow_redirects=False,
                          transport=transport) as client:
            result = client.post("login", json={
                "tenant_id": tenant_id, "username": username, "password": password,
            })
            StaffDeck._check_response(result)
            login = result.json()
            token = login.get("token")
            if not isinstance(token, str) or not token:
                raise ProtocolError("Complete the required account password change in the web UI.")
            result = client.post(
                "me/api-credentials", headers={"Authorization": f"Bearer {token}"},
                json={"name": "StaffDeck CLI", "expires_at": (
                    datetime.now(UTC) + timedelta(days=30)
                ).isoformat()},
            )
            StaffDeck._check_response(result)
            credential = result.json()
    except httpx.TransportError:
        raise TransportError() from None
    except (ValueError, KeyError, TypeError):
        raise ProtocolError("Invalid account authentication response.") from None
    if not isinstance(credential.get("api_key"), str) or not credential.get("id"):
        raise ProtocolError("Account authentication did not issue a CLI credential.")
    return {
        "base_url": base_url, "api_key": credential["api_key"],
        "credential_id": credential["id"], "expires_at": credential.get("expires_at"),
        "user": login.get("user", {}),
    }


def revoke_profile(profile: dict) -> None:
    try:
        with StaffDeck(base_url=profile["base_url"], api_key=profile["api_key"]) as client:
            client.request("POST", "auth/logout")
    except APIError as exc:
        if exc.status_code != 401:
            raise
