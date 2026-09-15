from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from staffdeck import APIError, ProtocolError, StaffDeck, TransportError


def client(handler, **kwargs):
    return StaffDeck(
        base_url="https://staffdeck.test/proxy/api/v1/",
        api_key="sd_test_secret",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_json_response_metadata_and_base_prefix():
    def handle(request):
        assert str(request.url) == "https://staffdeck.test/proxy/api/v1/agents?limit=2"
        assert request.headers["authorization"] == "Bearer sd_test_secret"
        return httpx.Response(
            200, json={"data": [], "future_field": 1},
            headers={"etag": '"v1"', "x-request-id": "req_1"},
        )

    with client(handle) as sdk:
        result = sdk.request("GET", "agents", params={"limit": 2})
        assert result.data == {"data": [], "future_field": 1}
        assert result.etag == '"v1"'
        assert result.request_id == "req_1"
        assert result.status_code == 200
    assert sdk._http.is_closed


@pytest.mark.parametrize("path", [
    "https://evil.test/agents", "//evil.test", "/agents", "../auth", "%2e%2e/auth",
    "agents/../../auth", "agents/%252e%252e/auth", "agents\\other", "agents?q=secret",
    "agents#fragment", "https:evil.test", "agents/%0aevil",
])
def test_rejects_unsafe_paths_before_sending_credentials(path):
    with client(lambda r: pytest.fail("must not send")) as sdk, pytest.raises(ValueError):
        sdk.request("GET", path)


@pytest.mark.parametrize("url", [
    "file:///tmp/test", "https://user:password@staffdeck.test/api/v1",
    "https://staffdeck.test/api/v1?key=secret", "https://staffdeck.test/api/v1#x",
    "https://staffdeck.test/api/enterprise", "relative",
])
def test_rejects_invalid_base_url(url):
    with pytest.raises(ValueError):
        StaffDeck(base_url=url, api_key="test")


def test_bare_origin_and_no_redirects():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://evil.test"})

    with StaffDeck(
        base_url="https://staffdeck.test", api_key="test", transport=httpx.MockTransport(handle)
    ) as sdk:
        with pytest.raises(APIError) as caught:
            sdk.request("GET", "agents")
        assert caught.value.status_code == 307
    assert len(requests) == 1
    assert requests[0].url.path == "/api/v1/agents"


@pytest.mark.parametrize("status", [401, 403, 409, 412, 422, 428, 500])
def test_problem_details_are_preserved_but_not_logged(status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, json={
            "code": "SOME_ERROR", "detail": "sd_test_secret", "errors": [{"path": "name"}],
            "request_id": "req_body",
        }, headers={"X-Request-ID": "req_header"})

    with client(handle) as sdk, pytest.raises(APIError) as caught:
        sdk.request("GET", "agents")
    error = caught.value
    assert error.code == "SOME_ERROR"
    assert error.detail == "sd_test_secret"
    assert error.errors == [{"path": "name"}]
    assert error.request_id == "req_header"
    assert "sd_test_secret" not in str(error)
    assert len(calls) == 1


def test_non_json_errors_and_invalid_success_json():
    with client(lambda r: httpx.Response(502, text="private proxy config"), max_retries=0) as sdk:
        with pytest.raises(APIError) as caught:
            sdk.request("GET", "agents")
        assert caught.value.problem == {}
        assert "private" not in str(caught.value)
    with (
        client(lambda r: httpx.Response(200, text="not json")) as sdk,
        pytest.raises(ProtocolError),
    ):
        sdk.request("GET", "agents")


def test_get_retries_transient_status_and_transport_errors(monkeypatch):
    sleeps = []
    monkeypatch.setattr("staffdeck.client.time.sleep", sleeps.append)
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("secret request URL")
        if len(calls) == 2:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"ok": True})

    with client(handle) as sdk:
        assert sdk.request("GET", "agents").data == {"ok": True}
    assert sleeps == [0.5, 2.0]
    assert len(calls) == 3


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("failure", ["status", "transport"])
def test_mutations_are_never_automatically_retried(method, failure):
    calls = []

    def handle(request):
        calls.append(request)
        assert request.headers["idempotency-key"] == "order-1"
        assert request.headers["if-match"] == '"v1"'
        if failure == "transport":
            raise httpx.ReadError("sd_test_secret")
        return httpx.Response(503)

    with client(handle) as sdk, pytest.raises((APIError, TransportError)) as caught:
        sdk.request(method, "agents", body={"name": "x"},
                    idempotency_key="order-1", if_match='"v1"')
    assert len(calls) == 1
    assert "sd_test_secret" not in str(caught.value)


def test_retry_budget_and_long_retry_after(monkeypatch):
    sleeps = []
    monkeypatch.setattr("staffdeck.client.time.sleep", sleeps.append)
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ConnectError("secret")

    with client(handle) as sdk, pytest.raises(TransportError):
        sdk.request("GET", "agents")
    assert len(calls) == 3
    assert sleeps == [0.5, 1.0]
    sleeps.clear()
    with client(lambda r: httpx.Response(429, headers={"Retry-After": "3600"})) as sdk:
        with pytest.raises(APIError) as caught:
            sdk.request("GET", "agents")
        assert caught.value.retry_after == "3600"
    assert sleeps == []
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=10))
    assert 8 < StaffDeck._retry_delay(0, future) <= 10
    assert StaffDeck._retry_delay(0, "invalid") == 0.5
    assert StaffDeck._retry_delay(0, "nan") is None


@pytest.mark.parametrize("kwargs", [
    {"body": {"tenant_id": "other"}}, {"idempotency_key": "a\nb"},
    {"if_match": ""}, {"idempotency_key": "x" * 201}, {"timeout": 0},
])
def test_rejects_bad_request_options(kwargs):
    with client(lambda r: pytest.fail("must not send")) as sdk, pytest.raises(ValueError):
        sdk.request("POST", "agents", **kwargs)


@pytest.mark.parametrize("kwargs", [{"timeout": float("nan")}, {"max_retries": -1}])
def test_rejects_invalid_client_options(kwargs):
    with pytest.raises(ValueError):
        client(lambda r: pytest.fail("must not send"), **kwargs)
