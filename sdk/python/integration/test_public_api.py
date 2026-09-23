from __future__ import annotations

import httpx
import pytest

from staffdeck import APIError, RunFailedError


@pytest.mark.parametrize("status", ["failed", "cancelled"])
@pytest.mark.parametrize("empty_reconnect", [False, True])
def test_process_event_at_proxy_eof_does_not_hide_persisted_terminal_event(
    api, monkeypatch, status, empty_reconnect,
):
    from app.public_api import jobs

    _, engine, _, sdk_factory = api
    monkeypatch.setattr(jobs, "engine", engine)
    monkeypatch.setattr("staffdeck.runs.time.sleep", lambda _: None)

    def execute(db, job):
        jobs.update_job(db, job, event_type=f"run.{status}", event_data={"process": True})
        if status == "cancelled":
            raise jobs.JobCancelled()
        raise RuntimeError("fixture failure")

    monkeypatch.setitem(jobs._handlers, "run", execute)
    with sdk_factory() as sdk:
        run_id = sdk.runs.create("agent_api", {"input": "failure fixture"}).data["id"]
        jobs.run_job(run_id)
        state = sdk.runs.get(run_id).data
        assert state["status"] == status
        assert state["final_event_id"] == "4"
        original = sdk._http._transport.handle_request
        cursors = []

        def proxy(request):
            response = original(request)
            if request.url.path.endswith("/events"):
                cursors.append(request.headers.get("last-event-id"))
                if len(cursors) == 1:
                    # The real backend persisted four events. Simulate a proxy's
                    # clean EOF after the third, terminal-named process event.
                    frames = response.content.split(b"\n\n")
                    assert len(frames) == 5
                    assert f"event: run.{status}".encode() in frames[2]
                    response.close()
                    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                          content=b"\n\n".join(frames[:3]) + b"\n\n")
                if empty_reconnect and len(cursors) == 2:
                    response.close()
                    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                          content=b"")
            return response

        monkeypatch.setattr(sdk._http._transport, "handle_request", proxy)
        events = list(sdk.runs.events(run_id))
        assert [event.id for event in events] == ["1", "2", "3", "4"]
        assert events[-1].event == f"run.{status}"
        assert cursors == ([None, "3", "3"] if empty_reconnect else [None, "3"])


def test_sdk_tool_sop_session_run_round_trip(api, skill_card, monkeypatch):
    from app.public_api import jobs

    _, engine, _, sdk_factory = api
    monkeypatch.setattr(jobs, "engine", engine)

    def deterministic_run(db, job):
        jobs.update_job(
            db, job, event_type="run.output.delta", event_data={"content": "fixture reply"}
        )
        return {"reply": "fixture reply", "run_id": job.id, "session_id": job.request_json["session_id"]}

    # Only external Agent execution is stubbed; the durable job worker is real.
    monkeypatch.setitem(jobs._handlers, "run", deterministic_run)
    with sdk_factory() as sdk:
        assert sdk.agents.get("agent_api").etag
        tool = sdk.tools.create("agent_api", {
            "name": "sdk_policy", "method": "GET", "url": "https://example.com/policy",
            "headers": {"Authorization": "Bearer private-tool-credential"},
            "capability_scope": "general",
        })
        assert tool.status_code == 201
        assert "private-tool-credential" not in str(tool.data)
        tools = sdk.tools.list("agent_api").data["data"]
        assert any(t["id"] == tool.data["id"] for t in tools)

        draft = sdk.sops.create("agent_api", skill_card, idempotency_key="draft-1")
        draft_id = draft.data["id"]
        sop_id = draft.data["sop_id"]
        replay = sdk.sops.create("agent_api", skill_card, idempotency_key="draft-1")
        assert replay.data["id"] == draft_id
        fetched = sdk.sops.get_draft("agent_api", sop_id, draft_id)
        assert fetched.etag == draft.etag
        changed = sdk.sops.patch(
            "agent_api", sop_id, [{"op": "replace", "path": "/description", "value": "SDK SOP"}],
            draft_id=draft_id, if_match=fetched.etag,
        )
        assert changed.data["content"]["description"] == "SDK SOP"
        with pytest.raises(APIError) as stale:
            sdk.sops.replace("agent_api", sop_id, skill_card, draft_id=draft_id, if_match=fetched.etag)
        assert stale.value.status_code == 412
        assert sdk.sops.validate("agent_api", sop_id, draft_id).data["valid"]
        assert sdk.sops.publish("agent_api", sop_id, draft_id).data["draft"]["status"] == "published"
        assert sdk.sops.versions("agent_api", sop_id).data["data"]

        session = sdk.sessions.create("agent_api", {"external_session_id": "sdk-conversation"})
        updated = sdk.sessions.update(
            "agent_api", session.data["id"], {"title": "SDK session"}, if_match=session.etag
        )
        assert updated.data["title"] == "SDK session"
        body = {"input": "policy", "session_id": session.data["id"], "session_mode": "stateful"}
        run = sdk.runs.create("agent_api", body, idempotency_key="run-1")
        assert run.status_code == 202
        run_id = run.data["id"]
        assert sdk.runs.create("agent_api", body, idempotency_key="run-1").data["id"] == run_id
        with pytest.raises(APIError) as conflict:
            sdk.runs.create("agent_api", {"input": "different"}, idempotency_key="run-1")
        assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
        with pytest.raises(APIError) as queued:
            sdk.runs.result(run_id)
        assert queued.value.code == "RUN_NOT_SUCCEEDED"
        jobs.run_job(run_id)
        assert sdk.runs.wait(run_id).data["reply"] == "fixture reply"
        events = list(sdk.runs.events(run_id))
        assert any(e.event == "run.output.delta" for e in events)
        assert events[-1].event == "run.succeeded"
        assert list(sdk.runs.events(run_id, last_event_id=events[-1].id)) == []


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_unicode_run_output_round_trips_through_persisted_sse(api, monkeypatch, separator):
    from app.public_api import jobs

    _, engine, _, sdk_factory = api
    monkeypatch.setattr(jobs, "engine", engine)
    content = f"before{separator}after"

    def execute(db, job):
        jobs.update_job(db, job, event_type="run.output.delta", event_data={"content": content})
        return {"reply": content}

    monkeypatch.setitem(jobs._handlers, "run", execute)
    with sdk_factory() as sdk:
        run_id = sdk.runs.create("agent_api", {"input": "unicode fixture"}).data["id"]
        jobs.run_job(run_id)
        events = list(sdk.runs.events(run_id))
        assert [e.data["content"] for e in events if e.event == "run.output.delta"] == [content]
        assert events[-1].event == "run.succeeded"
        assert sdk.runs.wait(run_id).data["reply"] == content


def test_runtime_credential_cannot_manage_tools_or_other_agents(api):
    server, _, token, sdk_factory = api
    clients = server.get("/api-clients", headers={"Authorization": f"Bearer {token}"}).json()
    key = server.post(
        f"/api-clients/{clients[0]['id']}/credentials",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "runtime", "agent_id": "agent_api", "scopes": ["agents:read", "runs:read"]},
    ).json()["api_key"]
    with sdk_factory(key) as sdk:
        assert sdk.agents.get("agent_api").status_code == 200
        with pytest.raises(APIError) as denied:
            sdk.agents.get("agent_other")
        assert denied.value.status_code == 403
        with pytest.raises(APIError) as denied:
            sdk.tools.create("agent_api", {"name": "forbidden"})
        assert denied.value.status_code == 403
        assert denied.value.request_id


def test_failed_worker_is_not_successful_wait(api, monkeypatch):
    from app.public_api import jobs

    _, engine, _, sdk_factory = api
    monkeypatch.setattr(jobs, "engine", engine)

    def fail(db, job):
        raise RuntimeError("simulated provider failure")

    monkeypatch.setitem(jobs._handlers, "run", fail)
    with sdk_factory() as sdk:
        run_id = sdk.runs.create("agent_api", {"input": "fail"}).data["id"]
        jobs.run_job(run_id)
        with pytest.raises(RunFailedError) as caught:
            sdk.runs.wait(run_id)
        assert caught.value.status == "failed"
