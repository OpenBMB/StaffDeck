"""Publish existing authorized definitions once before enabling Runtime cutover."""

import argparse
import os

from sqlmodel import Session

from app.db import engine
from app.db.models import SopDefinitionRecord, SopDraftRecord, SopVersionRecord

from .sop_management import migrate_published


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--agent-id")
    args = parser.parse_args()
    if not os.environ.get("SOP_RUNTIME_ENDPOINT"):
        parser.error("SOP_RUNTIME_ENDPOINT is required")
    for model in (SopDefinitionRecord, SopVersionRecord, SopDraftRecord):
        model.__table__.create(engine, checkfirst=True)
    with Session(engine) as db:
        published = migrate_published(db, args.tenant_id, args.agent_id)
        print(f"Published {len(published)} immutable SOP definitions")


if __name__ == "__main__":
    main()
