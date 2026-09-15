from __future__ import annotations

import io
import json

from staffdeck import cli


def test_cli_real_api_session_etag_and_errors(api, monkeypatch, capsys):
    _, _, _, sdk = api
    monkeypatch.setenv("STAFFDECK_BASE_URL", "http://testserver/api/v1")
    monkeypatch.setenv("STAFFDECK_API_KEY", "test-placeholder")
    monkeypatch.setattr(cli, "StaffDeck", lambda **kwargs: sdk())
    monkeypatch.setattr("sys.stdin", io.StringIO('{"name":"CLI agent"}'))
    assert cli.main(["agents", "create", "--json", "-"]) == 0
    agent = json.loads(capsys.readouterr().out)["data"]["id"]
    assert cli.main(["sessions", "create", "--agent-id", agent]) == 0
    response = json.loads(capsys.readouterr().out)
    session = response["data"]["id"]
    monkeypatch.setattr("sys.stdin", io.StringIO('{"title":"CLI session"}'))
    assert cli.main([
        "sessions", "update", "--agent-id", agent, "--session-id", session,
        "--json", "-", "--if-match", response["etag"],
    ]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["title"] == "CLI session"
    monkeypatch.setattr("sys.stdin", io.StringIO('{"title":"Stale write"}'))
    assert cli.main([
        "sessions", "update", "--agent-id", agent, "--session-id", session,
        "--json", "-", "--if-match", response["etag"],
    ]) == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["status_code"] == 412
    assert error["request_id"]
