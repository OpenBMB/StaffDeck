from types import SimpleNamespace

import httpx
import json
import pytest

from app.db.models import Skill
from app.public_api import sops
from app.public_api.schemas import SOPRouteRequest
from app.session.session_schema import PlannedTaskFrame, TurnPlan


def _skill(skill_id: str, scope: str = "general") -> Skill:
    return Skill(
        id=f"skill-{skill_id}",
        tenant_id="tenant-1",
        skill_id=skill_id,
        name=skill_id,
        description=f"{skill_id} description",
        content_json={"trigger_intents": [skill_id], "capability_scope": scope, "nodes": [{"node_id": "start"}]},
        status="published",
    )


def test_public_sop_route_uses_visible_discoverable_skills_and_native_router(monkeypatch):
    visible = [_skill("purchase"), _skill("price_compare"), _skill("internal", "sop_specific")]
    monkeypatch.setattr(sops, "enforce_agent_access", lambda *args, **kwargs: None)
    monkeypatch.setattr(sops, "ensure_public_agent", lambda *args, **kwargs: None)
    monkeypatch.setattr(sops, "visible_published_skills", lambda *args, **kwargs: visible)
    monkeypatch.setattr(sops, "model_for_agent", lambda *args, **kwargs: object())

    captured = {}

    def fake_plan(self, message, session, available_skills, model_config, conversation_context=None, memory_context=None, task_frame_state=None, interaction_mode="normal", team_context=None):
        captured["skills"] = [skill.skill_id for skill in available_skills]
        return TurnPlan(
            decision="start_new_task",
            confidence=0.9,
            user_intent=message,
            task_frames=[PlannedTaskFrame(
                kind="sop",
                decision="start_new_task",
                target_skill_id="purchase",
                target_step_id="start",
            )],
        )

    monkeypatch.setattr(sops.TurnPlanner, "plan", fake_plan)
    principal = SimpleNamespace(tenant_id="tenant-1", actor_user=SimpleNamespace(id="user-1"))
    result = sops.route_sop(
        "agent-1",
        SOPRouteRequest(message="buy an item"),
        principal,
        SimpleNamespace(commit=lambda: None),
    )

    assert result["selected_sop_id"] == "purchase"
    assert result["candidate_sop_ids"] == ["purchase", "price_compare"]
    assert captured["skills"] == ["purchase", "price_compare"]


def test_public_sop_route_uses_bound_pd_host_without_sd_model_fallback(monkeypatch):
    from app.public_api import pilotdeck_domain_host
    visible = [_skill("purchase")]
    monkeypatch.setattr(sops, "enforce_agent_access", lambda *args, **kwargs: None)
    monkeypatch.setattr(sops, "ensure_public_agent", lambda *args, **kwargs: None)
    monkeypatch.setattr(sops, "visible_published_skills", lambda *args, **kwargs: visible)
    monkeypatch.setattr(sops, "model_for_agent", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("SD model read")))
    calls = []
    class Host:
        def plan_sop_route(self, **kwargs):
            calls.append(kwargs)
            return TurnPlan(decision="start_new_task", confidence=0.9, task_frames=[
                PlannedTaskFrame(kind="sop", decision="start_new_task", target_skill_id="purchase", target_step_id="start")])
    monkeypatch.setattr(pilotdeck_domain_host, "require_pilotdeck_domain_host", lambda: Host())
    principal = SimpleNamespace(tenant_id="tenant-1", actor_user=SimpleNamespace(id="user-1"))
    result = sops.route_sop("agent-1", SOPRouteRequest(message="buy", model_source="pilotdeck_host"),
                            principal, SimpleNamespace(commit=lambda: None))
    assert result["selected_sop_id"] == "purchase"
    assert [skill.skill_id for skill in calls[0]["routing_skills"]] == ["purchase"]
    assert calls[0]["actor_user_id"] == "user-1"


def test_public_route_to_bound_pd_port_preserves_visible_sop_and_pep(monkeypatch):
    from app.public_api import pilotdeck_domain_host as host
    from app.public_api.errors import PublicAPIError
    visible = [_skill("purchase"), _skill("hidden", "sop_specific")]
    checks = []
    monkeypatch.setattr(sops, "enforce_agent_access", lambda principal, agent: checks.append(("access", agent)))
    monkeypatch.setattr(sops, "ensure_public_agent", lambda db, principal, agent: checks.append(("agent", agent)))
    monkeypatch.setattr(sops, "visible_published_skills", lambda db, tenant, agent: visible)
    monkeypatch.setattr(sops, "model_for_agent", lambda *args: pytest.fail("PD route cannot read the SD model"))
    calls = []
    def gateway(request):
        body = json.loads(request.content)
        calls.append(body)
        assert request.headers["authorization"] == "Bearer bridge"
        assert body["principal"] == {"pilotDeckUserId": "pd-user", "tenantId": "tenant-1",
                                     "actorUserId": "user-1", "agentId": "agent-1"}
        if body["operation"] == "list_model_catalog":
            return httpx.Response(200, json={"defaultSelection": {"mode": "model", "provider": "p", "model": "m"},
                "data": [{"id": "p/m", "provider": "p", "model": "m", "available": True},
                         {"id": "other/model", "provider": "other", "model": "model", "available": True}]})
        assert body["operation"] == "model_stream"
        assert body["input"]["modelId"] == "p/m"
        return httpx.Response(200, headers={"content-type": "application/x-ndjson"}, text=
            '{"type":"text_delta","text":"{\\"decision\\":\\"start_new_task\\",\\"task_frames\\":[{\\"kind\\":\\"sop\\",\\"decision\\":\\"start_new_task\\",\\"target_skill_id\\":\\"purchase\\"}]}"}\n'
            '{"type":"message_end","finishReason":"stop"}\n')
    previous = host._bound_client
    principal = SimpleNamespace(tenant_id="tenant-1", actor_user=SimpleNamespace(id="user-1"))
    try:
        host.bind_pilotdeck_domain_host(host.PilotDeckDomainHostClient(
            "http://pd", "bridge", "pd-user", httpx.MockTransport(gateway)))
        result = sops.route_sop("agent-1", SOPRouteRequest(message="buy", model_source="pilotdeck_host"),
                                principal, SimpleNamespace(commit=lambda: None))
        assert result["selected_sop_id"] == "purchase"
        assert result["candidate_sop_ids"] == ["purchase"]
        assert checks == [("access", "agent-1"), ("agent", "agent-1")]
        assert [call["operation"] for call in calls] == ["list_model_catalog", "model_stream"]
        host._bound_client = None
        with pytest.raises(PublicAPIError) as missing:
            sops.route_sop("agent-1", SOPRouteRequest(message="buy", model_source="pilotdeck_host"),
                           principal, SimpleNamespace(commit=lambda: None))
        assert (missing.value.status_code, missing.value.code) == (503, "PUBLIC_HOST_SOP_ROUTE_UNAVAILABLE")
        assert len(calls) == 2
    finally:
        host._bound_client = previous
