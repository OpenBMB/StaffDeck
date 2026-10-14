"""Create disposable real host capability resources, without replacing their implementations."""

import json
import os
from pathlib import Path

from app.agents.branching import (
    ensure_agent_private_knowledge_branch,
    ensure_private_resource_binding,
)
from app.db import engine
from app.db.models import (
    GeneralSkill,
    KnowledgeBase,
    KnowledgeBaseVersion,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeDocument,
    Tool,
)
from sqlmodel import Session

root = Path(os.environ["SOP_ACCEPTANCE_ROOT"])
database = Path(os.environ["DATABASE_URL"].removeprefix("sqlite:///"))
if (
    database.resolve().parent != root.resolve().parent
    or database.name != "staffdeck.sqlite"
):
    raise SystemExit(
        "Use the isolated acceptance staffdeck.sqlite beside the evidence directory"
    )
tenant, agent = "sop-acceptance", "sop-acceptance-agent"
with Session(engine) as db:
    skill = db.get(GeneralSkill, "acceptance-guide")
    if skill is None:
        skill = GeneralSkill(
            id="acceptance-guide",
            tenant_id=tenant,
            slug="approval-guide",
            name="Approval guide",
            status="published",
            skill_markdown="# Release guide\n\nRead release policy and request owner approval before release.",
        )
        db.add(skill)
    tool = db.get(Tool, "acceptance-tool")
    if tool is None:
        tool = Tool(
            id="acceptance-tool",
            tenant_id=tenant,
            name="release_ticket_lookup",
            method="GET",
            url="http://127.0.0.1:26395/ticket",
            input_schema={"type": "object", "properties": {}},
        )
        db.add(tool)
    base = db.get(KnowledgeBase, "acceptance-policy")
    if base is None:
        base = KnowledgeBase(
            id="acceptance-policy", tenant_id=tenant, name="Acceptance release policy"
        )
        db.add(base)
        db.flush()
        version = KnowledgeBaseVersion(
            id="acceptance-policy-v1",
            tenant_id=tenant,
            knowledge_base_id=base.id,
            name=base.name,
            version="1.0.0",
        )
        db.add(version)
        common = {
            "tenant_id": tenant,
            "knowledge_base_id": base.id,
            "knowledge_base_version_id": version.id,
        }
        db.add(
            KnowledgeDocument(
                id="acceptance-policy-doc",
                **common,
                filename="release.md",
                file_type="md",
                title="Release policy",
                status="ready",
            )
        )
        db.add(
            KnowledgeBucket(
                id="acceptance-policy-bucket",
                **common,
                document_id="acceptance-policy-doc",
                bucket_key="release",
                title="Release approval",
                summary="Owner approval verification code RELEASE_OWNER_1014 is required.",
            )
        )
        db.add(
            KnowledgeChunk(
                id="acceptance-policy-chunk",
                **common,
                document_id="acceptance-policy-doc",
                bucket_id="acceptance-policy-bucket",
                chunk_index=0,
                content="The owner approval verification code is RELEASE_OWNER_1014. Release ticket SOP-1014 requires owner approval.",
            )
        )
    db.flush()
    ensure_private_resource_binding(db, tenant, agent, "general_skill", skill.id)
    ensure_private_resource_binding(db, tenant, agent, "tool", tool.id)
    ensure_agent_private_knowledge_branch(db, tenant, agent, base)
    identifiers = {"skill": skill.id, "tool": tool.id, "knowledge": base.id}
    db.commit()
(root / "capabilities.json").write_text(json.dumps(identifiers))
print("Seeded disposable Tool, Skill, and Knowledge resources")
