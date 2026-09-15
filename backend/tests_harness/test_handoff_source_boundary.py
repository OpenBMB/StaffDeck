"""Handoff uses selected employee/identity facts, without a shadow AgentProfile."""
from types import SimpleNamespace

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.core.human_handoff_service import HumanHandoffService
from app.db.models import AgentProfile, ChatSession, HumanHandoffRequest, Tenant, User
from app.session.session_schema import StepAgentResult
from staffdeck_harness.contracts.errors import ModuleSdkError, PermissionDenied
from staffdeck_harness.contracts.manifest import ModuleKind, SlotName
from staffdeck_harness.contracts.security import ResourceRef, SecurityContext, SecurityProfile
from staffdeck_harness.contracts.staff import StaffProfile
from staffdeck_harness.handoff.core import DefaultAssignment, HandoffCore, HandoffTransitionError, WebInboxNotifier
from staffdeck_harness.modules.registry import ModuleRegistry, manifest
from staffdeck_harness.runtime.staff_ownership import handoff_context
from staffdeck_harness.security.oss_local import LocalPep
from staffdeck_harness.security.profile import Guard


class InternalIdentity:
    def is_internal_user(self, user):
        return user.tenant_id == "tenant" and user.source == "external_identity"

    def from_user(self, user, *, channel=None):
        if not self.is_internal_user(user):
            raise PermissionDenied("unverified identity")
        return SecurityContext(user.id, user.tenant_id, provider="external_identity", channel=channel)

    def from_service(self, *args, **kwargs):
        raise PermissionDenied("arbitrary service identity is forbidden")


class EmployeeSource:
    failure = None
    owner = "owner"

    def reference(self, context):
        return ResourceRef("agent", context.staff_id, context.tenant_id)

    def profile(self, context):
        if self.failure:
            raise self.failure
        return StaffProfile(context.staff_id, context.tenant_id, "Remote employee", "active",
            ResourceRef("agent", context.staff_id, context.tenant_id,
                        {"owner_user_id": self.owner} if self.owner else {}),
            metadata_json={"ownership_resolved": True})

    def resolve(self, context):
        raise AssertionError("ownership must not assemble the AgentLoop")

    def model(self, *args):
        raise AssertionError("ownership must not load model credentials")


@pytest.fixture
def environment(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    source = EmployeeSource()
    registry = ModuleRegistry()
    registry.install(manifest("source.staff.test", "Employee facts", kind=ModuleKind.TRUSTED,
        slots=[SlotName.STAFF_SOURCE]), SimpleNamespace(build=lambda db: source), slot=SlotName.STAFF_SOURCE)
    identity = InternalIdentity()
    profile = SecurityProfile("OSS_LOCAL", identity, LocalPep(), None)
    registry.security_profile = profile
    identity_source = SimpleNamespace(resolve=lambda context, adapter:
        SecurityContext(context.user_id, context.tenant_id, provider="external_identity"))
    registry.install(manifest("source.identity.test", "Identity facts", kind=ModuleKind.TRUSTED,
        slots=[SlotName.IDENTITY_SOURCE]), SimpleNamespace(build=lambda db: identity_source), slot=SlotName.IDENTITY_SOURCE)
    monkeypatch.setattr("staffdeck_harness.modules.registry._active", None)
    with Session(engine) as db:
        db.info["staffdeck_registry"] = registry
        db.add_all([Tenant(id="tenant", name="Test"), Tenant(id="foreign", name="Other"),
            User(id="requester", tenant_id="tenant", username="requester", password_hash="x", source="external_identity"),
            User(id="owner", tenant_id="tenant", username="owner", password_hash="x", source="external_identity"),
            User(id="handler", tenant_id="tenant", username="handler", password_hash="x", source="external_identity"),
            User(id="guest", tenant_id="tenant", username="guest", password_hash="x", source="feishu"),
            User(id="foreign-user", tenant_id="foreign", username="foreign", password_hash="x", source="external_identity"),
            User(id="local-admin", tenant_id="tenant", username="local-admin", role="admin", password_hash="x"),
            User(id="external-admin", tenant_id="tenant", username="external-admin", role="admin", password_hash="x", source="external_identity"),
            ChatSession(id="session", tenant_id="tenant", agent_id="remote", user_id="requester", channel="web")])
        db.commit()
        core = HandoffCore(db, Guard("handoff", profile), DefaultAssignment(), {"web": WebInboxNotifier()}, {})
        yield db, source, core
    engine.dispose()


def handoff(**kwargs):
    return HumanHandoffRequest(tenant_id="tenant", session_id="session", agent_id="remote",
        requester_user_id="requester", **kwargs)


def test_owner_and_handoff_permission_facts_use_selected_source_without_shadow_row(environment):
    db, source, core = environment
    assert db.get(AgentProfile, "remote") is None
    row = handoff()
    core.assign(SecurityContext("requester", "tenant"), row, actor_is_system=True)
    assert row.assignee_user_id == "owner" and row.status == "assigned"
    assert core._ref(row).attributes["agent_owner_user_id"] == "owner"
    core.guard.require(SecurityContext("owner", "tenant"), "handoff.assign/v1", core._ref(row))


def test_service_resolves_selected_owner_and_does_not_pick_local_admin(environment):
    db, source, core = environment
    service = HumanHandoffService(db, None)
    assert service.assignee_user_id("tenant", "remote", "requester",
        tenant_admin_resolver=lambda tenant: pytest.fail("owner must win")) == "owner"
    assert service.tenant_admin_user_id("tenant") == "external-admin"


@pytest.mark.parametrize("failure", [ModuleSdkError("gone", code="STAFF_NOT_FOUND"),
    ModuleSdkError("offline", code="SOURCE_UNAVAILABLE")])
def test_unavailable_source_never_becomes_admin_fallback(environment, failure):
    db, source, core = environment
    source.failure = failure
    db.add(AgentProfile(id="remote", tenant_id="tenant", name="stale shadow", metadata_json={"owner_user_id":"local-admin"}))
    db.commit()
    with pytest.raises(ModuleSdkError, match="无法确认") as error:
        core.propose(handoff())
    assert error.value.code == "HANDOFF_STAFF_UNAVAILABLE"


def test_known_owner_without_trusted_mapping_is_not_reassigned_to_admin(environment):
    db, source, core = environment
    source.owner = "not-projected-yet"
    with pytest.raises(ModuleSdkError) as error:
        core.propose(handoff())
    assert error.value.code == "HANDOFF_OWNER_UNAVAILABLE"
    assert core.propose(handoff(metadata_json={"step_assignee_user_id":"handler"})) == "handler"


@pytest.mark.parametrize("metadata", [
    {"step_assignee_user_id":"handler", "binding_default_assignee_user_id":"owner"},
    {"binding_default_assignee_user_id":"handler"},
])
def test_explicit_internal_handler_precedes_source_owner(environment, metadata):
    db, source, core = environment
    assert core.propose(handoff(metadata_json=metadata)) == "handler"


@pytest.mark.parametrize("candidate", ["guest", "foreign-user", "local-admin"])
def test_explicit_assignment_rejects_guest_other_tenant_and_other_identity_realm(environment, candidate):
    db, source, core = environment
    with pytest.raises(HandoffTransitionError):
        core.assign(SecurityContext("requester", "tenant"), handoff(),
                    assignee_user_id=candidate, actor_is_system=True)


def test_notification_uses_request_actor_when_service_identity_is_not_supported(environment):
    db, source, core = environment
    session = db.get(ChatSession, "session")
    ctx = handoff_context(db, session, core.guard.profile)
    row = core.create(ctx, session, pending_question="Review", context_summary="Context")
    assert row.assignee_user_id == "owner" and row.status == "assigned"


def test_service_does_not_run_legacy_admin_resolver_before_selected_assignment(environment, monkeypatch):
    db, source, core = environment
    monkeypatch.setattr("staffdeck_harness.handoff.core.for_session", lambda db, session: core)
    service = HumanHandoffService(db, SimpleNamespace(record=lambda *args: None))
    row = service.create("tenant", db.get(ChatSession, "session"), StepAgentResult(reply="Review", handoff=True),
        current_step_resolver=lambda: {},
        assignee_resolver=lambda *args: pytest.fail("must not call the local fallback before module assignment"),
        context_summary=lambda session: "Context", pending_question=lambda *args: "Review")
    assert row.assignee_user_id == "owner"
    assert "legacy_assignee_user_id" not in row.metadata_json


def test_request_identity_mismatch_is_rejected(environment):
    db, source, core = environment
    identity_source = db.info["staffdeck_registry"].provider(SlotName.IDENTITY_SOURCE).provider.build(db)
    identity_source.resolve = lambda context, adapter: SecurityContext("foreign-user", "foreign")
    with pytest.raises(PermissionDenied):
        handoff_context(db, db.get(ChatSession, "session"), core.guard.profile)
