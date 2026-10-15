from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fastapi import HTTPException


@dataclass(frozen=True)
class Principal:
    id: str
    tenant_id: str = "local"
    display_name: str = "Local"
    spaces: frozenset[str] = frozenset({"*"})
    permissions: frozenset[str] = frozenset({"read", "write"})


class IdentityProvider(Protocol):
    def authenticate(self, authorization: str | None) -> Principal: ...


class AuthorizationProvider(Protocol):
    def authorize(self, principal: Principal, space_id: str, action: str) -> None: ...


class LocalIdentity:
    def authenticate(self, authorization: str | None) -> Principal:
        return Principal("local")


class StaticBearerIdentity:
    def __init__(self, tokens: dict[str, Principal]):
        self.tokens = tokens

    def authenticate(self, authorization: str | None) -> Principal:
        token = (authorization or "").removeprefix("Bearer ")
        if not authorization or not authorization.startswith("Bearer ") or token not in self.tokens:
            raise HTTPException(401, "Invalid bearer token")
        return self.tokens[token]


class SpaceAuthorization:
    def authorize(self, principal: Principal, space_id: str, action: str) -> None:
        if action not in principal.permissions or (
            "*" not in principal.spaces and space_id not in principal.spaces
        ):
            raise HTTPException(403, "Space permission denied")
