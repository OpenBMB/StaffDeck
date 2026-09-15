from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace as NS
import logging
import pytest
from fastapi.testclient import TestClient
import channel_scope_service as service


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    monkeypatch.setattr(service, '_fingerprint', None)
    monkeypatch.setattr(service, 'applied_fingerprint', lambda: 'applied')
    monkeypatch.setattr(service, 'peek_registry', lambda: None)


def test_rebuild_failure_is_classified_and_does_not_expose_exception_secrets(monkeypatch, caplog):
    def fail(*_):
        raise ValueError('credential-test-value')
    monkeypatch.setattr(service, 'install_connector_assembly', fail)
    with caplog.at_level(logging.ERROR):
        result = service._load_applied_assembly()
    assert result['code'] == 'CHANNEL_SCOPE_ASSEMBLY_UNAVAILABLE'
    assert result['cause_code'] == 'ValueError' and result['error_id']
    assert 'credential-test-value' not in str(result) + caplog.text
    assert service._fingerprint is None


def test_health_returns_structured_failure(monkeypatch):
    def fail():
        raise OSError('private-config-value')
    monkeypatch.setattr(service, 'applied_fingerprint', fail)
    client = TestClient(service.app)
    response = client.get('/internal/runtime/channel-scope/health')
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'CHANNEL_SCOPE_ASSEMBLY_UNAVAILABLE'
    assert 'private-config-value' not in response.text
    client.close()


def test_inflight_requests_prevent_replacement(monkeypatch):
    monkeypatch.setattr(service, 'peek_registry', lambda: NS(live_turns=1))
    monkeypatch.setattr(service, 'install_connector_assembly', lambda *_: pytest.fail('must drain first'))
    assert service._load_applied_assembly()['code'] == 'CHANNEL_SCOPE_DRAINING'


def test_concurrent_health_requests_build_one_registry(monkeypatch):
    calls = []
    monkeypatch.setattr(service, 'install_connector_assembly', lambda value: calls.append(value))
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda _: service._load_applied_assembly(), range(12))) == [None] * 12
    assert calls == ['applied']


def test_failure_recovers_after_environment_is_corrected(monkeypatch):
    monkeypatch.setattr(service, 'install_connector_assembly', lambda *_: (_ for _ in ()).throw(RuntimeError('bad')))
    assert service._load_applied_assembly()['code'] == 'CHANNEL_SCOPE_ASSEMBLY_UNAVAILABLE'
    monkeypatch.setattr(service, 'install_connector_assembly', lambda *_: None)
    assert service._load_applied_assembly() is None
    assert service._fingerprint == 'applied'
