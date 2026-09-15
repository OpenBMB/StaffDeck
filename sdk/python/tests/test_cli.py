from __future__ import annotations

import inspect
import io
import json
from unittest.mock import MagicMock

import pytest

from staffdeck import (
    APIError,
    APIResponse,
    ProtocolError,
    RunEvent,
    RunFailedError,
    StaffDeck,
    StreamError,
    TransportError,
    WaitTimeout,
    cli,
)


@pytest.fixture
def factory(monkeypatch):
    monkeypatch.setenv("STAFFDECK_API_KEY", "test-secret")
    monkeypatch.setenv("STAFFDECK_BASE_URL", "https://example.test/api/v1")
    factory = MagicMock()
    factory.return_value.__enter__.return_value = factory.return_value
    monkeypatch.setattr(cli, "StaffDeck", factory)
    return factory


@pytest.mark.parametrize("resource,method,fields", [
    (resource, method, fields.split())
    for resource, commands in cli._COMMANDS.items() for method, fields in commands.items()
])
def test_commands_delegate_to_sdk(factory, monkeypatch, capsys, resource, method, fields):
    args = [resource.replace("_", "-"), method.replace("_", "-")]
    kwargs = {}
    for field in fields:
        name = field.rstrip("?")
        if name in cli._JSON_TYPES:
            value = {} if cli._JSON_TYPES[name] is dict else [{}]
            monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(value)))
            args.extend(["--json", "-"])
        else:
            value = 2 if name in cli._NUMERIC_TYPES else "value"
            args.extend(["--" + name.replace("_", "-"), str(value)])
        kwargs[name] = value
    # Check the mapping against actual SDK signatures, not just against mocks.
    with StaffDeck(base_url="https://example.test", api_key="test") as real:
        inspect.signature(getattr(getattr(real, resource), method)).bind(**kwargs)
    call = getattr(getattr(factory.return_value, resource), method)
    if method == "events":
        call.return_value = (event for event in [RunEvent("1", "run.queued", {"status": "queued"})])
    else:
        call.return_value = APIResponse({"id": "a"}, 202, "request-1", '"etag"')
    assert cli.main(args) == 0
    call.assert_called_once_with(**kwargs)
    factory.return_value.__exit__.assert_called_once()
    out = capsys.readouterr()
    assert not out.err
    result = json.loads(out.out)
    if method == "events":
        assert result == {"id": "1", "event": "run.queued", "data": {"status": "queued"}}
    else:
        assert result == {
            "data": {"id": "a"}, "status_code": 202, "request_id": "request-1", "etag": '"etag"',
        }


def test_file_input_and_defaults(factory, tmp_path):
    source = tmp_path / "body.json"
    source.write_text('{"title":"Example"}')
    factory.return_value.sessions.create.return_value = APIResponse({}, 201)
    assert cli.main(["sessions", "create", "--agent-id", "a", "--json", str(source)]) == 0
    factory.return_value.sessions.create.assert_called_once_with(agent_id="a", body={"title": "Example"})
    factory.assert_called_once_with(
        base_url="https://example.test/api/v1", api_key="test-secret", timeout=30.0, max_retries=2,
    )


@pytest.mark.parametrize("args,text", [
    ([], ""),
    (["agents", "update", "--agent-id", "a", "--json", "-"], "{}"),
    (["agents", "create", "--json", "-"], "test-secret"),
    (["agents", "create", "--json", "-"], "[]"),
    (["agents", "create", "--json", "/nonexistent-test-file"], ""),
    (["agents", "set-resources", "--agent-id", "a", "--json", "-"], '["not-an-object"]'),
    (["agents", "list", "--limit", "test-secret"], ""),
    (["--api-key", "test-secret", "agents", "list"], ""),
])
def test_usage_failures_do_not_call_api(factory, monkeypatch, capsys, args, text):
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    assert cli.main(args) == 2
    factory.assert_not_called()
    out = capsys.readouterr()
    assert not out.out and "test-secret" not in out.err
    assert json.loads(out.err)["error"]["kind"] == "usage"


def test_missing_credentials(factory, monkeypatch, capsys):
    monkeypatch.delenv("STAFFDECK_API_KEY")
    assert cli.main(["agents", "list"]) == 2
    factory.assert_not_called()
    assert json.loads(capsys.readouterr().err)["error"]["kind"] == "usage"


@pytest.mark.parametrize("error,exit_code,kind", [
    (APIError(403, problem={"detail": "test-secret", "code": "FORBIDDEN"},
              request_id="test-secret"), 1, "api"),
    (TransportError(), 3, "transport"),
    (RunFailedError("r", {"status": "failed", "error": "test-secret"}), 4, "run_failed"),
    (WaitTimeout("r"), 5, "wait_timeout"),
    (StreamError("r", "12", "Interrupted."), 6, "stream"),
    (ProtocolError("Invalid stream."), 6, "protocol"),
    (ValueError("test-secret"), 2, "usage"),
    (KeyboardInterrupt(), 130, "interrupted"),
])
def test_errors(factory, capsys, error, exit_code, kind):
    factory.return_value.runs.get.side_effect = error
    assert cli.main(["runs", "get", "--run-id", "r"]) == exit_code
    out = capsys.readouterr()
    assert not out.out and "test-secret" not in out.err
    assert json.loads(out.err)["error"]["kind"] == kind


def test_stream_keeps_processed_events_and_cursor_on_error(factory, capsys):
    closed = []

    def events():
        try:
            yield RunEvent("12", "run.delta", {"text": "hello"})
            raise StreamError("r", "12", "Interrupted.")
        finally:
            closed.append(True)

    factory.return_value.runs.events.return_value = events()
    assert cli.main(["runs", "events", "--run-id", "r", "--last-event-id", "11"]) == 6
    out = capsys.readouterr()
    assert json.loads(out.out)["id"] == "12"
    assert json.loads(out.err)["error"]["last_event_id"] == "12"
    assert closed == [True]


def test_help_does_not_require_credentials(monkeypatch, capsys):
    monkeypatch.delenv("STAFFDECK_API_KEY", raising=False)
    with pytest.raises(SystemExit) as result:
        cli.main(["sops", "publish", "--help"])
    assert result.value.code == 0
    assert "--draft-id" in capsys.readouterr().out
