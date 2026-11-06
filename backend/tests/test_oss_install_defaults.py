import json

from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    AgentSkillBranch,
    GeneralSkill,
    KnowledgeBase,
    ModelConfig,
    Skill,
    Tool,
    User,
)
from app.security.auth import verify_password
from staffdeck_harness.distribution.bootstrap import configure_environment, initialize_defaults
from staffdeck_harness.distribution.manifest import make_manifest
from staffdeck_harness.distribution.schema import PackageRequest
from staffdeck_harness.modules.config import RuntimeOverrides


def test_fresh_oss_admin_and_five_owned_employees_are_initialized_once(tmp_path, monkeypatch):
    manifest = make_manifest(PackageRequest(tenant_id='local', kind='source'), RuntimeOverrides())
    assert len(manifest['installation']['default_staff']) == 5
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest))
    monkeypatch.setenv('STAFFDECK_DISTRIBUTION_MANIFEST', str(path))
    monkeypatch.setenv('ULTRARAG_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('ULTRARAG_DOTENV', str(tmp_path / 'missing.env'))
    monkeypatch.delenv('STAFFDECK_INITIAL_ADMIN_PASSWORD', raising=False)
    monkeypatch.delenv('APP_SECRET', raising=False)
    monkeypatch.delenv('DATABASE_URL', raising=False)
    configure_environment()
    assert len((tmp_path / 'data/.installation-seed').read_bytes()) == 32
    assert not (tmp_path / 'data/installation-secrets.json').exists()
    engine = create_engine('sqlite://')
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        initialize_defaults(db, manifest)
        user = db.exec(select(User)).one()
        assert user.username == 'admin' and verify_password('admin', user.password_hash)
        employees = db.exec(select(AgentProfile)).all()
        assert {row.name for row in employees} == {'IT', '人事', '行政', '财务', '法务'}
        assert all(row.metadata_json['owner_user_id'] == user.id
                   and row.metadata_json['owner_username'] == 'admin' for row in employees)
        assert len(db.exec(select(Skill)).all()) == 15
        assert len(db.exec(select(KnowledgeBase)).all()) == 14
        assert len(db.exec(select(GeneralSkill)).all()) == 9
        assert len(db.exec(select(Tool)).all()) == 11
        assert not db.exec(select(ModelConfig)).all()
        for employee in employees:
            assert db.exec(select(AgentResourceBinding).where(
                AgentResourceBinding.agent_id == employee.id,
                AgentResourceBinding.status == 'active')).all()
            assert db.exec(select(AgentSkillBranch).where(
                AgentSkillBranch.agent_id == employee.id)).all()
        for model in (AgentResourceBinding, AgentSkillBranch, Skill, KnowledgeBase, Tool):
            assert all(row.tenant_id == 'local' for row in db.exec(select(model)).all())
        employees[0].name = '用户改名'
        db.add(employees[0]); db.commit()
        initialize_defaults(db, manifest)
        assert len(db.exec(select(AgentProfile)).all()) == 5
        assert db.get(AgentProfile, employees[0].id).name == '用户改名'
    engine.dispose()
