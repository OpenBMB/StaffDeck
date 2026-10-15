import base64
from types import SimpleNamespace
import httpx
import pytest
from fastapi import HTTPException
from app.api import knowledge_capabilities as capabilities
from app.public_api import pilotdeck_domain_host as host


def test_public_file_parse_uses_bound_pd_port_and_rechecks_origin(monkeypatch):
    from app.public_api import auth, knowledge_pep
    calls = []
    def handle(request):
        body = __import__("json").loads(request.content)
        calls.append(body)
        assert request.headers["authorization"] == "Bearer service"
        assert body["principal"] == {"pilotDeckUserId": "pd-user", "tenantId": "tenant", "actorUserId": "actor", "agentId": "target"}
        return httpx.Response(200, json={"text": "unique fact", "metadata": {"fileType": "md"}})
    client = host.PilotDeckDomainHostClient("http://pd", "service", "pd-user", httpx.MockTransport(handle))
    monkeypatch.setattr(host, "_bound_client", client)
    principal = SimpleNamespace(tenant_id="tenant", actor_user=SimpleNamespace(id="actor"), can=lambda scope: scope == "knowledge:write")
    checks = []
    monkeypatch.setattr(auth, "principal_for_credential", lambda db, credential: principal if credential == "credential" else pytest.fail("Wrong credential"))
    monkeypatch.setattr(auth, "enforce_agent_access", lambda p, agent, write: checks.append((agent, write)))
    monkeypatch.setattr(knowledge_pep, "enforce_public_knowledge_pep", lambda *args, **kwargs: checks.append("pep"))
    monkeypatch.setenv("STAFFDECK_KM_CAPABILITIES_BEARER", "km-service")
    body = capabilities.CapabilityRequest(tenant_id="tenant", space_id="sd:tenant:target", filename="unique.md",
        content_base64=base64.b64encode(b"unique fact").decode(), context={"public_origin": {"credential_id": "credential", "agent_id": "target", "actor_user_id": "actor"}})
    assert capabilities.parse(body, "Bearer km-service", object()) == {"text": "unique fact", "file_type": "md"}
    assert checks == [("target", True), "pep"]
    assert len(calls) == 1
    principal.actor_user.id = "forged"
    with pytest.raises(HTTPException) as error:
        capabilities.parse(body, "Bearer km-service", object())
    assert error.value.status_code == 403
    assert len(calls) == 1
    principal.actor_user.id = "actor"
    monkeypatch.setattr(host, "_bound_client", None)
    with pytest.raises(HTTPException) as error:
        capabilities.parse(body, "Bearer km-service", object())
    assert error.value.status_code == 503


def test_public_file_parse_rejects_host_failure():
    client = host.PilotDeckDomainHostClient("http://pd", "service", "pd-user",
        httpx.MockTransport(lambda request: httpx.Response(415, json={"code": "PUBLIC_FILE_TYPE_UNSUPPORTED"})))
    with pytest.raises(RuntimeError, match="PUBLIC_HOST_FILE_PARSE_FAILED: 415"):
        client.file_parse(tenant_id="tenant", actor_user_id="actor", agent_id="target", filename="fact.pdf", content_base64="YQ==", media_type="application/pdf")


def test_public_knowledge_retrieval_does_not_use_sd_default_model(monkeypatch):
    from app.api import knowledge
    from app.knowledge.public_host_selection import use_public_host_retrieval
    sentinel = object()
    monkeypatch.setattr(knowledge, "_get_default_model", lambda db, tenant: sentinel)
    assert knowledge._get_request_model(None, "tenant") is sentinel
    with use_public_host_retrieval():
        assert knowledge._get_request_model(None, "tenant") is None
        with pytest.raises(HTTPException):
            knowledge._get_request_model(None, "tenant", "sd-model")
