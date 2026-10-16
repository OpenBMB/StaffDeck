"""Publish existing authorized definitions once before enabling Runtime cutover."""

import argparse
import json
import os
from pathlib import Path

from sqlmodel import Session, select

from app.db import engine
from app.db.models import (
    AgentProfile,
    SopDefinitionRecord,
    SopDraftRecord,
    SopVersionRecord,
    Tenant,
)

from .sop_management import SopManagementService, migrate_published


def main():
    parser = argparse.ArgumentParser()
    tenants = parser.add_mutually_exclusive_group(required=True)
    tenants.add_argument("--tenant-id")
    tenants.add_argument("--all-tenants", action="store_true")
    parser.add_argument("--agent-id")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not os.environ.get("SOP_RUNTIME_ENDPOINT"):
        parser.error("SOP_RUNTIME_ENDPOINT is required")
    if args.all_tenants and args.agent_id:
        parser.error("--agent-id requires --tenant-id")
    for model in (SopDefinitionRecord, SopVersionRecord, SopDraftRecord):
        model.__table__.create(engine, checkfirst=True)
    with Session(engine) as db:
        tenant_ids = (
            sorted(db.exec(select(Tenant.id)).all()) if args.all_tenants else [args.tenant_id]
        )
        report = {"status": "running", "scopes": []}
        total = 0
        try:
            for tenant_id in tenant_ids:
                agents = (
                    db.exec(
                        select(AgentProfile)
                        .where(
                            AgentProfile.tenant_id == tenant_id, AgentProfile.status != "archived"
                        )
                        .order_by(AgentProfile.id)
                    ).all()
                    if args.all_tenants
                    else []
                )
                scopes = (
                    [None, *[agent.id for agent in agents]] if args.all_tenants else [args.agent_id]
                )
                for agent_id in scopes:
                    published = migrate_published(db, tenant_id, agent_id)
                    total += len(published)
                    management_scope = SopManagementService.scope(db, tenant_id, agent_id)
                    counts = {}
                    for label, model in (
                        ("definitions", SopDefinitionRecord),
                        ("versions", SopVersionRecord),
                        ("drafts", SopDraftRecord),
                    ):
                        # Drafts retain the original agent ID, including the overall agent.
                        owner = agent_id if model is SopDraftRecord else management_scope
                        counts[label] = len(
                            db.exec(
                                select(model).where(
                                    model.tenant_id == tenant_id, model.agent_id == owner
                                )
                            ).all()
                        )
                    report["scopes"].append(
                        {
                            "tenant_id": tenant_id,
                            "agent_id": agent_id,
                            "published": len(published),
                            **counts,
                        }
                    )
            report["status"] = "passed"
            print(f"Published {total} immutable SOP definitions")
        except Exception:
            report["status"] = "failed"
            raise
        finally:
            if args.report:
                args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
