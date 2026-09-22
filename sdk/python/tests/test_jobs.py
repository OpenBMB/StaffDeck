from __future__ import annotations

import json

import httpx
import pytest

from staffdeck import RunFailedError, StaffDeck, WaitTimeout, cli


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
def test_wait_uses_job_endpoint_and_preserves_result_envelope(status):
    calls = []
    result = {"job": {"status": status}, "result": {"draft": {"id": "draft"}}, "error": {}}

    def handle(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/result"):
            return httpx.Response(200, json=result)
        assert request.url.path == "/api/v1/jobs/job"
        return httpx.Response(200, json={"status": status})

    with StaffDeck(base_url="https://test", api_key="key", transport=httpx.MockTransport(handle)) as sdk:
        if status == "succeeded":
            assert sdk.jobs.wait("job").data == result
            assert calls == ["/api/v1/jobs/job", "/api/v1/jobs/job/result"]
        else:
            with pytest.raises(RunFailedError) as caught:
                sdk.jobs.wait("job")
            assert caught.value.run_id == "job"
            assert caught.value.status == status
            assert len(calls) == 1


def test_wait_timeout_never_cancels(monkeypatch):
    ticks = iter([0, 0, 0, 2])
    monkeypatch.setattr("staffdeck.runs.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("staffdeck.runs.time.sleep", lambda _: None)

    def handle(request):
        assert request.method == "GET" and request.url.path == "/api/v1/jobs/job"
        return httpx.Response(200, json={"status": "queued"})

    with (
        StaffDeck(base_url="https://test", api_key="key", transport=httpx.MockTransport(handle)) as sdk,
        pytest.raises(WaitTimeout),
    ):
        sdk.jobs.wait("job", timeout=1)


@pytest.mark.parametrize("error,exit_code,kind", [
    (RunFailedError("job", {"status": "failed"}), 4, "job_failed"),
    (WaitTimeout("job"), 5, "wait_timeout"),
])
def test_cli_job_failure_identifiers(monkeypatch, capsys, error, exit_code, kind):
    monkeypatch.setenv("STAFFDECK_API_KEY", "test")
    monkeypatch.setenv("STAFFDECK_BASE_URL", "https://test")

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr("staffdeck.jobs.Jobs.wait", fail)
    assert cli.main(["jobs", "wait", "--job-id", "job"]) == exit_code
    output = capsys.readouterr()
    assert not output.out
    payload = json.loads(output.err)["error"]
    assert payload["kind"] == kind and payload["job_id"] == "job"
    assert "run_id" not in payload
