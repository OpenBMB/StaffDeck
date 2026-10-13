from types import SimpleNamespace

import pytest

from app.public_api import pilotdeck_domain_binding as binding


def test_domain_binding_uses_normal_gateway_credential_and_explicit_pd_user(tmp_path, monkeypatch):
    token_path = tmp_path / "server-token"
    token_path.write_text("gateway-service\n", encoding="utf-8")
    env = {
        "PILOTDECK_DOMAIN_HOST_ENABLED": "true",
        "PILOTDECK_GATEWAY_URL": "ws://127.0.0.1:16411/ws",
        "PILOTDECK_GATEWAY_TOKEN_PATH": str(token_path),
        "PILOTDECK_USER_ID": "original-pd-user",
        "STAFFDECK_COPY_TENANT_ID": "original-tenant",
        "STAFFDECK_COPY_ACTOR_USER_ID": "original-sd-actor",
        "STAFFDECK_COPY_TARGET_AGENT_ID": "original-target",
    }
    bound = []
    monkeypatch.setattr(binding, "bind_pilotdeck_domain_host", bound.append)
    app = SimpleNamespace(state=SimpleNamespace())
    binding.bind_pilotdeck_domain_client(app, env)
    client = bound[0]
    assert app.state.pilotdeck_domain_host is client
    assert client.origin == "http://127.0.0.1:16411"
    assert client.pilotdeck_user_id == "original-pd-user"
    assert client.bridge_token == "gateway-service"
    with pytest.raises(RuntimeError, match="PUBLIC_HOST_FIXED_IDENTITY_MISMATCH"):
        client.file_parse(tenant_id="other", actor_user_id="original-sd-actor",
                          agent_id="original-target", filename="facts.md",
                          content_base64="", media_type=None)
    with pytest.raises(RuntimeError, match="PUBLIC_HOST_FIXED_IDENTITY_MISMATCH"):
        client.plan_sop_route(
            tenant_id="original-tenant", actor_user_id="other",
            agent_id="original-target", message="route", session=None,
            routing_skills=[], conversation_context=None,
        )


def test_disabled_domain_binding_is_inert_and_enabled_missing_identity_fails(monkeypatch):
    bound = []
    monkeypatch.setattr(binding, "bind_pilotdeck_domain_host", bound.append)
    app = SimpleNamespace(state=SimpleNamespace())
    binding.bind_pilotdeck_domain_client(app, {})
    assert not bound
    with pytest.raises(RuntimeError, match="PD_DOMAIN_HOST_CONFIG_REQUIRED"):
        binding.bind_pilotdeck_domain_client(app, {"PILOTDECK_DOMAIN_HOST_ENABLED": "true"})
    assert not bound


def test_project_mode_allows_agent_routing_only_within_configured_actor():
    client = binding.FixedPilotDeckDomainHostClient("http://pd", "token", "pd-user",
        tenant_id="tenant", actor_user_id="actor", project_bindings=True)
    assert client.permits("tenant", "actor", "agent-a")
    assert client.permits("tenant", "actor", "agent-b")
    assert not client.permits("other", "actor", "agent-a")
    assert not client.permits("tenant", "other", "agent-a")
    assert not client.permits("tenant", "actor", "")
