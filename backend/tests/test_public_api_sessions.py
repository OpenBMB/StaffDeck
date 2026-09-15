from test_public_api_v1 import _client, _tenant_key


def test_session_update_uses_database_dependency_and_preserves_etag_guards(monkeypatch):
    client, engine, token = _client(monkeypatch)
    key = _tenant_key(client, token, ["sessions:read", "sessions:write"])
    auth = {"Authorization": f"Bearer {key}"}
    with client:
        created = client.post(
            "/agents/agent_api/sessions", headers=auth,
            json={"external_session_id": "session-update-regression", "title": "before"},
        )
        assert created.status_code == 201
        path = f"/agents/agent_api/sessions/{created.json()['id']}"
        fetched = client.get(path, headers=auth)
        assert fetched.status_code == 200
        etag = fetched.headers["etag"]
        assert client.patch(path, headers=auth, json={"title": "after"}).status_code == 428
        updated = client.patch(
            path, headers={**auth, "If-Match": etag},
            json={"title": "after", "metadata": {"integration": "sdk"}},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["title"] == "after"
        assert updated.json()["metadata"] == {"integration": "sdk"}
        assert updated.headers["etag"] != etag
        assert client.patch(
            path, headers={**auth, "If-Match": etag}, json={"title": "stale"},
        ).status_code == 412
        # The DB alias must not weaken the employee/session boundary.
        assert client.patch(
            path.replace("agent_api", "agent_other"),
            headers={**auth, "If-Match": updated.headers["etag"]}, json={"title": "other"},
        ).status_code == 404
    engine.dispose()
