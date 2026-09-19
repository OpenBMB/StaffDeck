"""Shape translations between the business SPA contract and this framework's routes.

Every function here is pure: it receives already-decoded JSON and returns JSON. The
business SPA (staffdeck_business/staffdeck-frontend) and the releases SPA mostly agree
on entity structure; the differences are field names (camelCase knowledge, TeamHub
envelope for general skills), a handful of derived fields (allowed_actions,
published_to_gallery lifted out of metadata) and the SOP resource type spelling.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Actor:
    user_id: str
    tenant_id: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


# --------------------------------------------------------------------------- helpers


def _meta(row: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def envelope(data: Any, total: int | None = None) -> dict[str, Any]:
    """TeamHub-style `{code,msg,data,meta}` that the business skill page unwraps."""
    body: dict[str, Any] = {"code": 0, "msg": "ok", "data": data}
    if total is not None:
        body["meta"] = {"total": total}
    return body


def collection_page(items: list[Any]) -> dict[str, Any]:
    return {"items": items, "next_cursor": None, "revision": "0"}


# --------------------------------------------------------------------------- agents

_MANAGE_ACTIONS = ["edit", "manage", "share", "delete"]


def resources_to_business(rows: Any) -> Any:
    if not isinstance(rows, list):
        return rows
    return [
        {**row, "resource_type": "sop"} if isinstance(row, dict) and row.get("resource_type") == "skill" else row
        for row in rows
    ]


def resources_to_oss(rows: Any) -> Any:
    if not isinstance(rows, list):
        return rows
    return [
        {**row, "resource_type": "skill"} if isinstance(row, dict) and row.get("resource_type") == "sop" else row
        for row in rows
    ]


def agent_to_business(row: Any, actor: Actor | None) -> Any:
    if isinstance(row, list):
        return [agent_to_business(item, actor) for item in row]
    if not isinstance(row, dict) or "id" not in row:
        return row
    meta = _meta(row)
    access = meta.get('directory_access') or {}
    actions = row.get('allowed_actions')
    if not isinstance(actions, list):
        actions = access.get('allowed_actions')
    if not isinstance(actions, list):
        actions = [action for action in ('view', 'use', 'manage') if access.get('can_' + action) is True]
    out = {
        **row,
        "published_to_gallery": row.get('published_to_gallery', meta.get("published_to_gallery") is True),
        "allowed_actions": actions,
        "owner_user_id": row.get('owner_user_id', meta.get("owner_user_id")),
        "owner_user_name": row.get('owner_user_name', meta.get("owner_display_name") or meta.get("owner_username")),
        "creator_user_id": row.get('creator_user_id', meta.get("created_by_user_id") or meta.get("created_by")),
        "creator_name": row.get('creator_name', meta.get("created_by_display_name") or meta.get("created_by_username") or meta.get("creator_name")),
        "chat_count": row.get("chat_count", (meta.get('directory_statistics') or {}).get('chat_count', meta.get("chat_count", 0))),
    }
    if isinstance(row.get("resources"), list):
        out["resources"] = resources_to_business(row["resources"])
    return out


def agent_write_to_oss(body: Any, tenant_id: str | None) -> Any:
    if not isinstance(body, dict):
        return body
    out = dict(body)
    if tenant_id and "tenant_id" not in out:
        out["tenant_id"] = tenant_id
    if isinstance(out.get("resources"), list):
        out["resources"] = resources_to_oss(out["resources"])
    return out


def agent_shares_page(agent_id: str) -> dict[str, Any]:
    return {"agent_id": agent_id, "items": [], "revision": "0", "next_cursor": None}


# --------------------------------------------------------------------------- generic bodies


def with_tenant(body: Any, tenant_id: str | None) -> Any:
    if isinstance(body, dict) and tenant_id and "tenant_id" not in body:
        return {**body, "tenant_id": tenant_id}
    return body


# --------------------------------------------------------------------------- models


def model_configs_to_chat_models(rows: Any) -> dict[str, Any]:
    items = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        items.append(
            {
                "id": row.get("id"),
                "name": row.get("name"),
                "model": row.get("model"),
                "provider": row.get("provider"),
                "is_default": row.get("is_default") is True,
                "enabled": row.get("enabled") is not False,
            }
        )
    return {"items": items}


# --------------------------------------------------------------------------- SOPs


def sops_available_page(rows: Any) -> dict[str, Any]:
    published = [row for row in rows if isinstance(row, dict) and row.get("status") == "published"] if isinstance(rows, list) else []
    return collection_page(published)
