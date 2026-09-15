from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy.pool import StaticPool

from app.core.agent_loop import AgentLoop
from app.db.models import ChatSession


def test_conversation_compaction_preserves_session_realm_and_sop_pins():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        binding = {"namespace": "business-test", "source.staff": "business.source.staff"}
        pins = {"task-1": {"definition_hash": "immutable-version"}}
        session = ChatSession(id="test-session", tenant_id="tenant", context_state_json={
            "runtime_binding": binding, "sop_module_pins": pins, "custom_module_state": {"version": 2}})
        db.add(session)
        db.commit()
        AgentLoop(db)._conversation_context(session)
        db.commit()
        db.refresh(session)
        assert session.context_state_json["runtime_binding"] == binding
        assert session.context_state_json["sop_module_pins"] == pins
        assert session.context_state_json["custom_module_state"] == {"version": 2}
    engine.dispose()
