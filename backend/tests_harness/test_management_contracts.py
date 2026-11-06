import pytest
from fastapi import HTTPException
from staffdeck_harness.runtime.management_contracts import resolve_operation
from staffdeck_harness.contracts.runtime_services import ServiceResponse
from staffdeck_harness.runtime.management import ManagementRequest


@pytest.mark.parametrize('method,path', [
    ('POST', '/api/enterprise/tools/t/test'),
    ('POST', '/api/enterprise/general-skills/s/run/stream'),
    ('GET', '/api/enterprise/agents/gallery'),
    ('GET', '/api/enterprise/tools/t/new-execution-feature'),
])
def test_execution_and_unknown_routes_cannot_be_captured_by_resource_adapters(method, path):
    assert resolve_operation(method, path) is None


def test_contract_uses_public_response_model_and_does_not_leak_bad_payload():
    op = resolve_operation('GET', '/api/enterprise/tools/t')
    assert op.domain == 'tool'
    with pytest.raises(HTTPException) as exc:
        op.validate_response(ServiceResponse(200, {}, b'{"private":"secret"}'))
    assert exc.value.status_code == 502
    assert exc.value.detail['code'] == 'MANAGEMENT_RESPONSE_INVALID'
    assert 'secret' not in str(exc.value.detail)


def test_invalid_body_rejected_before_adapter_mutation():
    op = resolve_operation('POST', '/api/enterprise/tools')
    with pytest.raises(HTTPException) as exc:
        op.validate_request(ManagementRequest('POST', '/api/enterprise/tools', (),
            b'{"tenant_id":"tenant"}', {'content-type': 'application/json'}, None))
    assert exc.value.status_code == 422


def test_static_operation_precedes_resource_identifier():
    assert resolve_operation('GET', '/api/enterprise/tools/buckets').id == 'tool.list_tool_buckets/v1'
