"""Shared fixtures for the per-module test suite (one file per registered module).

Every test here is offline and deterministic: an in-memory SQLite database, a
sealed throwaway ModuleRegistry built from the built-in modules, the OSS_LOCAL
security profile, and fake hosts where a real host would need a running engine.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings
from app.db.models import AgentProfile, ModelConfig, Tenant, User, utc_now
from app.security.auth import hash_password
from app.security.encryption import encrypt_secret
from staffdeck_harness.contracts.invocation import InvocationContext, ModuleInvocation
from staffdeck_harness.contracts.manifest import SlotName
from staffdeck_harness.contracts.security import SecurityContext
from staffdeck_harness.modules.registry import ModuleRegistry, discover_and_install, reset_registry
from staffdeck_harness.modules.config import reset_env_snapshot as _reset_env_snapshot
from staffdeck_harness.security.profile import Guard, reset_profile
from staffdeck_harness.security.oss_local import build_oss_local_profile


class FakeSettings:
    """Minimal settings object accepted by discover_and_install / builders."""

    security_profile = "OSS_LOCAL"
    harness_v3_enabled = False
    harness_disabled_modules = ""
    harness_modules = ""
    harness_v3_root = ""
    harness_v3_home = ""
    base_authz_url = ""
    base_authz_decision_token = ""
    base_authz_control_token = ""
    base_authz_timeout_seconds = 3.0
    base_authz_pending_timeout_seconds = 3.0
    base_identity_internal_url = ""
    base_identity_runtime_client_id = ""
    base_identity_runtime_client_secret = ""
    base_workload_identity_audience = "staffdeck-gateway"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ULTRARAG_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HARNESS_V3_ENABLED", "false")
    monkeypatch.setenv("ULTRARAG_DOTENV", str(tmp_path / ".env"))
    get_settings.cache_clear()
    reset_registry()
    reset_profile()
    _reset_env_snapshot()
    yield
    get_settings.cache_clear()
    reset_registry()
    reset_profile()
    _reset_env_snapshot()


@pytest.fixture
def settings():
    return FakeSettings()


@pytest.fixture
def registry(settings):
    """Built-in registry, every slot guarded, sealed — the same shape the runtime builds at start."""

    reg = discover_and_install(ModuleRegistry(), settings)
    for slot in SlotName:
        reg.mark_guarded(slot)
    reg.seal()
    return reg


@pytest.fixture
def module(registry):
    """``module("knowledge.local")`` → the Installed entry (manifest + provider)."""

    def _get(module_id: str):
        item = registry.get(module_id)
        assert item is not None, f"{module_id} is not registered"
        return item

    return _get


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Tenant(id="t1", name="T1"))
        s.add(User(id="u1", tenant_id="t1", username="alice", role="member", password_hash=hash_password("x")))
        s.add(User(id="admin", tenant_id="t1", username="admin", role="admin", password_hash=hash_password("x")))
        s.add(AgentProfile(id="a1", tenant_id="t1", name="Agent One", status="active", metadata_json={"owner_user_id": "u1"}))
        s.add(ModelConfig(id="m1", tenant_id="t1", name="GLM", provider="openai_compatible", base_url="http://x/v1", api_key_encrypted=encrypt_secret("k"), model="glm", is_default=True, enabled=True, created_at=utc_now(), updated_at=utc_now()))
        s.commit()
        yield s


@pytest.fixture
def profile():
    return build_oss_local_profile()


@pytest.fixture
def guard(profile):
    def _for(module_id: str = "test") -> Guard:
        return Guard(module_id, profile)

    return _for


@pytest.fixture
def security_ctx():
    def _ctx(**kw) -> SecurityContext:
        base = dict(principal_id="u1", tenant_id="t1", principal_type="user", tenant_role="member")
        base.update(kw)
        return SecurityContext(**base)

    return _ctx


@pytest.fixture
def invocation():
    def _inv(operation: str = "tool.invoke/v1", *, module_id: str | None = None, arguments: dict | None = None, binding_id: str | None = None, side_effecting: bool = False, **ctx_kw) -> ModuleInvocation:
        base = dict(tenant_id="t1", agent_id="a1", user_id="u1", session_id="s1", turn_id="turn1", channel="web", task_frame_id="tf1", step_id="n1", run_id="run1")
        base.update(ctx_kw)
        return ModuleInvocation(invocation_id="inv1", module_id=module_id or operation.split(".", 1)[0], operation=operation, arguments=arguments or {}, context=InvocationContext(**base), binding_id=binding_id, side_effecting=side_effecting)

    return _inv


def pytest_configure(config):
    # keep the live dev server's DSH out of these tests even if a developer exports HARNESS_V3_ENABLED
    os.environ.setdefault("HARNESS_V3_ENABLED", "false")
