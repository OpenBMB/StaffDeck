from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine, select

from knowledge_core.database import initialize_schema
from knowledge_core.models import (KnowledgeModel, KnowledgeBinding, KnowledgeBranch, KnowledgeBase, KnowledgeBaseVersion, KnowledgeDocument)


def test_current_multidocument_and_edit_versions_survive_schema_restart(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'knowledge.sqlite'}")
    KnowledgeModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(KnowledgeBase(id="base", tenant_id="tenant", name="Private sources",
                             metadata_json={"private": True}))
        for version in ("1.0.0", "1.0.1"):
            db.add(KnowledgeBaseVersion(id=f"version-{version}", tenant_id="tenant",
                                       knowledge_base_id="base", version=version,
                                       name="Private sources"))
        for index, version in enumerate(("1.0.0", "1.0.0", "1.0.1")):
            db.add(KnowledgeDocument(id=f"doc-{index}", tenant_id="tenant",
                                    knowledge_base_id="base",
                                    knowledge_base_version_id=f"version-{version}",
                                    filename=f"source-{index}.md", file_type="md",
                                    model_config_id="model", status="ready"))
        db.add(KnowledgeBranch(id="branch", tenant_id="tenant", space_id="owner",
                                    knowledge_base_id="base", head_version_id="version-1.0.1", base_version_id="version-1.0.0"))
        db.add(KnowledgeBinding(id="binding", tenant_id="tenant", space_id="owner",
                                   knowledge_base_id="base",
                                   metadata_json={"private": True}))
        db.commit()
    for _ in range(2):
        initialize_schema(engine)
        with Session(engine) as db:
            docs = db.exec(select(KnowledgeDocument).order_by(KnowledgeDocument.id)).all()
            assert [(row.knowledge_base_id, row.knowledge_base_version_id) for row in docs] == [
                ("base", "version-1.0.0"), ("base", "version-1.0.0"), ("base", "version-1.0.1"),
            ]
            assert db.get(KnowledgeBranch, "branch").head_version_id == "version-1.0.1"
            binding = db.get(KnowledgeBinding, "binding")
            assert (binding.knowledge_base_id, binding.metadata_json) == ("base", {"private": True})
            assert not db.exec(select(KnowledgeBase).where(KnowledgeBase.id.like("kb_doc_%"))).all()
    engine.dispose()
