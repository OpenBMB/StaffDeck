import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select
from app.db.models import APIJob, APIJobEvent, APIClient, APICredential, User
from app.public_api import jobs
from app.public_api.runtime import check_client
from app.public_api.errors import PublicAPIError
from staffdeck_harness.runtime.services import LocalRuntimeServices, runtime_models


@pytest.fixture
def db(monkeypatch):
    from staffdeck_harness.modules import registry
    from staffdeck_harness.runtime import control_auth
    monkeypatch.setattr(registry,"peek_registry",lambda:None)
    monkeypatch.setattr(control_auth,"provider",lambda:None)
    engine=create_engine("sqlite://",poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_job_and_events_use_explicit_factory_not_global_database(db,monkeypatch):
    db.add(APIJob(id="job",tenant_id="tenant",credential_id="internal",kind="wiring-test",request_json={}))
    db.commit()
    services=LocalRuntimeServices(db)
    monkeypatch.setattr(jobs,"Session",lambda *a:pytest.fail("global session fallback"))
    monkeypatch.setitem(jobs._handlers,"wiring-test",lambda session,job:{"ok":True,"bound":session.info["staffdeck_runtime_services"] is services})
    jobs.run_job("job",data_services=services)
    db.expire_all()
    result=db.get(APIJob,"job")
    assert result.status=="succeeded" and result.result_json=={"ok":True,"bound":True}
    assert db.exec(select(APIJobEvent).where(APIJobEvent.job_id=="job")).all()


def test_revoked_credential_fails_before_job_handler(db,monkeypatch):
    db.add(User(id="user",tenant_id="tenant",username="owner",password_hash="x"))
    db.add(APIClient(id="client",tenant_id="tenant",name="client",created_by_user_id="user"))
    db.add(APICredential(id="key",tenant_id="tenant",client_id="client",name="key",key_prefix="prefix",key_digest="digest",status="revoked"))
    db.add(APIJob(id="job",tenant_id="tenant",credential_id="key",kind="run",request_json={}))
    db.commit()
    monkeypatch.setitem(jobs._handlers,"run",lambda *a:pytest.fail("revoked key executed"))
    jobs.run_job("job",data_services=LocalRuntimeServices(db))
    db.expire_all()
    result=db.get(APIJob,"job")
    assert result.status=="failed" and result.error_json["code"]=="API_KEY_REVOKED"
    assert not result.retryable


def test_credentials_cannot_cross_assembly_realm(db,monkeypatch):
    from staffdeck_harness.runtime import session_binding
    monkeypatch.setattr(session_binding,"current_binding",lambda db:{"namespace":"business-new","source.staff":"business.source.staff"})
    client=APIClient(tenant_id="tenant",name="client",metadata_json={"_runtime_binding":{"namespace":"oss-local"}})
    with pytest.raises(PublicAPIError) as error:
        check_client(db,client)
    assert error.value.code=="API_REALM_MISMATCH"


def test_runtime_model_mapping_includes_api_event_relay_and_delivery_state():
    assert {"APIJob","APIJobEvent","ExternalSessionBinding","WebhookDelivery"} <= runtime_models().keys()
