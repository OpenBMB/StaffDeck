"""Seed a disposable host DB, clone it, and migrate through the real Runtime HTTP port."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import yaml
from app.agents.branching import (
    ensure_agent_skill_branch,
    ensure_private_resource_binding,
)
from app.api.auth import (
    AccountAPICredentialCreateRequest,
    create_account_api_credential,
)
from app.db import engine, init_db
from app.db.models import (
    AgentProfile,
    ModelConfig,
    Skill,
    Tenant,
    User,
)
from app.security.auth import create_access_token, hash_password
from app.security.encryption import encrypt_secret
from app.skills.sop_management import migrate_published
from sqlmodel import Session, select

root = Path(os.environ["SOP_ACCEPTANCE_ROOT"])
source = Path(os.environ["DATABASE_URL"].removeprefix("sqlite:///"))
if (
    source.resolve().parent != root.resolve().parent
    or source.name != "staffdeck.sqlite"
):
    raise SystemExit(
        "Use the isolated acceptance staffdeck.sqlite beside the evidence directory"
    )
root.mkdir(parents=True, exist_ok=True, mode=0o700)
init_db()
tenant, agent = "sop-acceptance", "sop-acceptance-agent"
card = {
    "skill_id": "release_acceptance",
    "name": "Release acceptance",
    "version": "1.0.0",
    "description": "Collect a release ticket, request operator approval, and report its decision.",
    "trigger_intents": ["release acceptance"],
    "required_info": ["ticket"],
    "start_node_id": "collect",
    "terminal_node_ids": ["done"],
    "nodes": [
        {
            "node_id": "collect",
            "name": "Ticket",
            "type": "collect_info",
            "expected_user_info": ["ticket"],
            "instruction": "Ask for a release ticket if absent. Store ticket in slotUpdates, then finish this node.",
        },
        {
            "node_id": "approval",
            "name": "Operator approval",
            "type": "handoff",
            "assignee_user_id": "sop-reviewer",
            "assignee_notify_channel": "web",
            "instruction": "Request human approval. After the operator replies, finish this node quoting their decision.",
        },
        {
            "node_id": "done",
            "name": "Receipt",
            "type": "respond",
            "instruction": "Reply RELEASE_ACCEPTANCE_COMPLETE with the ticket and human decision, then finish.",
        },
    ],
    "edges": [
        {"source_node_id": "collect", "next_node_id": "approval"},
        {"source_node_id": "approval", "next_node_id": "done"},
    ],
}
config = yaml.safe_load(
    Path(
        os.environ.get(
            "SOP_ACCEPTANCE_MODEL_CONFIG", "/Users/a1/.pilotdeck/pilotdeck.yaml"
        )
    ).read_text()
)
selected = os.environ.get("SOP_ACCEPTANCE_MODEL", "provider1/qwen3.6-flash-distill")
provider_id, model = selected.split("/", 1)
provider = config["model"]["providers"][provider_id]
credentials = {}
with Session(engine) as db:
    if not db.get(Tenant, tenant):
        db.add(Tenant(id=tenant, name="Disposable SOP acceptance"))
    for user_id in ["sop-actor", "sop-reviewer"]:
        user = db.get(User, user_id)
        if user is None:
            user = User(
                id=user_id,
                tenant_id=tenant,
                username=user_id,
                role="admin",
                password_hash=hash_password("sop-acceptance-local-only"),
            )
            db.add(user)
            db.commit()
        credentials[user_id] = {
            "token": create_access_token(user),
            "userId": user.id,
            "tenantId": tenant,
        }
    if db.get(AgentProfile, agent) is None:
        db.add(
            AgentProfile(
                id=agent,
                tenant_id=tenant,
                name="SOP acceptance employee",
                metadata_json={
                    "owner_user_id": "sop-actor",
                    "owner_username": "sop-actor",
                },
            )
        )
    if db.get(ModelConfig, "sop-real-model") is None:
        key = provider["apiKey"]
        if key.startswith("${") and key.endswith("}"):
            key = os.environ[key[2:-1]]
        db.add(
            ModelConfig(
                id="sop-real-model",
                tenant_id=tenant,
                name="Acceptance real model",
                base_url=provider["url"],
                model=model,
                api_key_encrypted=encrypt_secret(key),
                trust_status="legacy_trusted",
                enabled=True,
                is_default=True,
            )
        )
    skill = db.exec(select(Skill).where(Skill.tenant_id == tenant)).first()
    if skill is None:
        skill = Skill(
            tenant_id=tenant,
            skill_id=card["skill_id"],
            name=card["name"],
            content_json=card,
            status="published",
        )
        db.add(skill)
        db.flush()
    ensure_private_resource_binding(db, tenant, agent, "skill", skill.id)
    ensure_agent_skill_branch(db, tenant, agent, skill)
    db.commit()
    credential = create_account_api_credential(
        AccountAPICredentialCreateRequest(name="SOP acceptance"),
        db.get(User, "sop-actor"),
        db,
    )
    credentials["management"] = {
        "key": credential.api_key,
        "credentialId": credential.id,
    }
backup = root / "pre-migration.sqlite"
if not backup.exists():
    with sqlite3.connect(source) as src, sqlite3.connect(backup) as dst:
        src.backup(dst)
with Session(engine) as db:
    gallery = migrate_published(db, tenant, None)
    employee = migrate_published(db, tenant, agent)
credentials.update(
    {"agentId": agent, "modelConfigId": "sop-real-model", "selectedModel": selected}
)
(root / "credentials.json").write_text(json.dumps(credentials))
(root / "credentials.json").chmod(0o600)
(root / "release-definition.json").write_text(json.dumps(card))
print(
    json.dumps(
        {
            "status": "migrated",
            "gallery": len(gallery),
            "employee": len(employee),
            "backup": str(backup),
            "model": selected,
        }
    )
)
