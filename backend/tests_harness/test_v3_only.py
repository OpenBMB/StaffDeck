from types import SimpleNamespace as NS
import pytest

from app.core.turn_coordinator import HarnessV2Engine
from app.db.models import AgentProfile
from app.session.session_schema import ChatTurnRequest
from staffdeck_harness.api.admin import StaffEngineUpdate, effective_engine_for
from staffdeck_harness.bridge.engine_host import EngineHost
from staffdeck_harness.bridge.model_gateway import _vision_messages
from staffdeck_harness.contracts.manifest import SlotName
from staffdeck_harness.modules.config import RuntimeOverrides
from staffdeck_harness.modules.registry import ModuleRegistry, discover_and_install


def test_no_legacy_configuration_or_staff_override_can_select_v2():
    settings = NS(harness_v3_enabled=False, harness_v3_staff_allowlist="other", harness_v3_fallback_to_v2=True,
                  security_profile="OSS_LOCAL", harness_disabled_modules="", harness_modules="")
    staff = AgentProfile(id="a", tenant_id="t", name="A", metadata_json={"execution_engine": "harness_v2"})
    assert effective_engine_for(settings, staff) == "harness_v3"
    assert EngineHost(settings).selects_harness_v3(ChatTurnRequest(tenant_id="t", message="hi"), "a")
    reg = discover_and_install(ModuleRegistry(), settings)
    assert reg.get("engine.harness_v2") is None
    assert reg.provider(SlotName.RUNTIME_ENGINE).manifest.module_id == "engine.harness_v3"
    assert RuntimeOverrides(engine="harness_v2").normalized().engine == "harness_v3"
    with pytest.raises(ValueError):
        StaffEngineUpdate(tenant_id="t", engine="harness_v2")
    with pytest.raises(RuntimeError, match="HARNESS_V2_RETIRED"):
        HarnessV2Engine(None)


def test_vision_projection_is_ephemeral_and_isolated_between_activations():
    from app.core.harness_attachments import ValidatedTaskImagePayload

    image = ValidatedTaskImagePayload("i", "test.png", "image/png", 3, "data:image/png;base64,AAA=")
    messages = [{"role": "user", "content": "look"}, {"role": "assistant", "content": "checking"}]
    projected = _vision_messages(messages, [image])
    assert messages[0]["content"] == "look"
    assert projected[0]["content"][-1] == {"type": "image_url", "image_url": {"url": image.data_url}}
    assert _vision_messages(messages, []) == messages
    assert _vision_messages(messages, [NS(data_url="unvalidated")]) == messages


@pytest.mark.parametrize("channel,entry", [
    ("web", "web"), ("skill_test", "web"), ("enterprise_debug", "web"),
    ("team", "web"), ("human_handoff_resume", "web"),
    ("public_api", "public_api"), ("scheduled_task", "scheduler"),
])
def test_internal_execution_and_test_channels_reuse_the_registered_ingress(channel, entry):
    from staffdeck_harness.runtime.ingress import accept

    seen = []
    provider = NS(accept=lambda request: seen.append(request) or request)
    registry = NS(providers=lambda slot: [NS(manifest=NS(metadata={"ingress": entry}, module_id=f"ingress.{entry}"), provider=provider)])
    request = ChatTurnRequest(tenant_id="t", user_id="u", message="test", channel=channel)
    assert accept(registry, request) is request
    assert seen == [request]
