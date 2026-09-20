from fastapi.testclient import TestClient

from staffdeck_sop_runtime.api import create_app
from staffdeck_sop_runtime.original_runtime import prepare, submit


CLIENT = TestClient(create_app())

SIMPLE_BUNDLE = {
    "sops": [{
        "id": "approval",
        "content": {
            "start_node_id": "ask",
            "nodes": [{"node_id": "ask", "instruction": "Request approval."}],
        },
    }],
}

REQUIRING_BUNDLE = {
    "sops": [{
        "id": "collect",
        "content": {
            "start_node_id": "collect",
            "nodes": [{
                "node_id": "collect",
                "expected_user_info": ["name"],
                "allowed_actions": ["call_tool:read_file"],
            }],
        },
    }],
}


def envelope(payload, request_id="request-1"):
    return {
        "protocolVersion": "2.0",
        "runId": "run-1",
        "operationId": "operation-1",
        "requestId": request_id,
        "sessionId": "session-1",
        "turnId": "turn-1",
        "idempotencyKey": "operation-1",
        "expectedRevision": 1,
        "payload": payload,
    }


def test_health_advertises_the_v2_sop_module_manifest():
    response = CLIENT.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "status": "ok",
        "protocolVersion": "2.0",
        "moduleId": "sop.runtime",
        "contract": "sop.lifecycle/v2",
        "operations": ["prepare", "submit"],
        "descriptorVersion": "1.0",
        "implementationId": "staffdeck.portable-sop",
        "implementationVersion": "0.1.0",
        "transport": "sop-http-v2",
        "capabilities": ["handoff", "external_wait"],
        "state": {"ownership": "host", "schema": "sop.lifecycle/v2", "scope": "session"},
        "requires": {"hostCapabilities": ["sop.host-resume/v1"]},
    }


def test_prepare_returns_the_v2_completion_envelope_and_preserves_request_identity():
    response = CLIENT.post("/v1/sop/prepare", json=envelope({
        "bundle": SIMPLE_BUNDLE,
        "state": {"selected_skill_id": "approval"},
    }, request_id="prepare-42"))

    assert response.status_code == 200
    body = response.json()
    assert body["protocolVersion"] == "2.0"
    assert body["requestId"] == "prepare-42"
    assert body["ok"] is True
    assert body["outcome"] == "completed"
    assert body["payload"]["state"]["active_step_id"] == "ask"


def test_http_transport_preserves_the_portable_adapter_prepare_and_submit_payloads():
    initial_state = {"selected_skill_id": "approval"}
    expected_prepare = prepare(SIMPLE_BUNDLE, initial_state)
    prepared = CLIENT.post("/v1/sop/prepare", json=envelope({
        "bundle": SIMPLE_BUNDLE,
        "state": initial_state,
    }, request_id="parity-prepare"))

    assert prepared.status_code == 200
    assert prepared.json()["payload"] == expected_prepare

    proposal = {"status": "completed", "replyFragment": "Approved."}
    expected_submit = submit(SIMPLE_BUNDLE, expected_prepare["state"], proposal, [])
    submitted = CLIENT.post("/v1/sop/submit", json=envelope({
        "bundle": SIMPLE_BUNDLE,
        "state": expected_prepare["state"],
        "proposal": proposal,
        "successfulToolNames": [],
    }, request_id="parity-submit"))

    assert submitted.status_code == 200
    assert submitted.json()["payload"] == expected_submit


def test_submit_preserves_staffdeck_rejections_in_a_v2_failure_envelope():
    response = CLIENT.post("/v1/sop/submit", json=envelope({
        "bundle": REQUIRING_BUNDLE,
        "state": {"selected_skill_id": "collect"},
        "proposal": {"status": "completed", "replyFragment": "Approved."},
        "successfulToolNames": [],
    }, request_id="submit-42"))

    assert response.status_code == 422
    body = response.json()
    assert body["requestId"] == "submit-42"
    assert body["ok"] is False
    assert body["outcome"] == "failed"
    assert body["error"]["code"] in {"REQUIRED_SLOT_MISSING", "REQUIRED_CAPABILITY_NOT_INVOKED"}
    assert body["error"]["retryability"] == "unsafe"


def test_malformed_envelope_uses_a_structured_protocol_error():
    response = CLIENT.post("/v1/sop/prepare", json={"requestId": "broken", "payload": {}})

    assert response.status_code == 400
    body = response.json()
    assert body["protocolVersion"] == "2.0"
    assert body["requestId"] == "broken"
    assert body["ok"] is False
    assert body["outcome"] == "failed"
    assert body["error"]["code"] == "SOP_RUNTIME_PROTOCOL"
    assert body["error"]["retryability"] == "unsafe"


def test_malformed_operation_payload_uses_the_same_structured_protocol_error():
    response = CLIENT.post("/v1/sop/prepare", json=envelope({"bundle": SIMPLE_BUNDLE}, request_id="broken-payload"))

    assert response.status_code == 400
    body = response.json()
    assert body["requestId"] == "broken-payload"
    assert body["ok"] is False
    assert body["error"]["code"] == "SOP_RUNTIME_PROTOCOL"
