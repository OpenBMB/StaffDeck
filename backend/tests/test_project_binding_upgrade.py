from sqlmodel import SQLModel, Session, create_engine, select
from sqlalchemy import inspect

from app.db.models import Tenant, User, AgentProfile, PilotDeckProjectBinding
from app.db import database


def test_old_database_adds_binding_table_and_preserves_rows(tmp_path, monkeypatch):
    path = tmp_path / 'legacy.sqlite'
    engine = create_engine(f'sqlite:///{path}')
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([Tenant(id='existing', name='Existing'),
                    User(id='existing-user', tenant_id='existing', username='existing', password_hash='unchanged'),
                    AgentProfile(id='existing-agent', tenant_id='existing', name='Existing agent')])
        db.commit()
    PilotDeckProjectBinding.__table__.drop(engine)
    assert 'pilotdeck_project_bindings' not in inspect(engine).get_table_names()
    monkeypatch.setattr(database, 'engine', engine)
    monkeypatch.setattr(database, 'database_url', f'sqlite:///{path}')
    monkeypatch.setattr(database.settings, 'startup_orphan_cleanup_enabled', False)
    database.init_db()
    database.init_db()
    assert 'pilotdeck_project_bindings' in inspect(engine).get_table_names()
    with Session(engine) as db:
        assert db.get(User, 'existing-user').password_hash == 'unchanged'
        assert db.get(AgentProfile, 'existing-agent').name == 'Existing agent'
        assert db.exec(select(PilotDeckProjectBinding)).all() == []
    engine.dispose()
