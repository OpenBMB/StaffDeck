from __future__ import annotations

import runpy
from pathlib import Path

import pytest


@pytest.mark.parametrize("publish", [False, True])
def test_partner_example_against_real_api(api, monkeypatch, capsys, publish):
    from app.public_api import jobs

    _, engine, _, sdk_factory = api
    monkeypatch.setattr(jobs, "engine", engine)
    monkeypatch.setitem(jobs._handlers, "run", lambda db, job: {"reply": "example fixture"})
    monkeypatch.setenv("STAFFDECK_BASE_URL", "http://testserver/api/v1")
    monkeypatch.setenv("STAFFDECK_API_KEY", "test-placeholder")
    path = Path(__file__).resolve().parents[1] / "examples" / "configure_and_run.py"
    main = runpy.run_path(str(path))["main"]

    def client(**kwargs):
        sdk = sdk_factory()
        create = sdk.runs.create

        def create_and_finish(*args, **kwargs):
            receipt = create(*args, **kwargs)
            jobs.run_job(receipt.data["id"])
            return receipt

        monkeypatch.setattr(sdk.runs, "create", create_and_finish)
        return sdk

    monkeypatch.setitem(main.__globals__, "StaffDeck", client)
    monkeypatch.setattr("sys.argv", [
        str(path), "--agent-id", "agent_api", "--tool-url", "https://example.com/policy",
        *(["--publish"] if publish else []),
    ])
    main()
    output = capsys.readouterr().out
    assert '"tool_id"' in output and '"draft_id"' in output
    if publish:
        assert '"run_id"' in output and "example fixture" in output
    else:
        assert "NOT published" in output and '"run_id"' not in output
