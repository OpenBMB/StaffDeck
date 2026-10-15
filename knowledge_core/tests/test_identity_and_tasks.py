import base64

from fastapi.testclient import TestClient
from knowledge_core.api import create_app
from knowledge_core.identity import Principal, StaticBearerIdentity
from knowledge_core.kernel import storage as s
from sqlalchemy import select
from support import full_runtime as Runtime
from test_independent_workflow import wait_job


def test_bearer_permissions_and_namespace_isolation(tmp_path):
    identity = StaticBearerIdentity(
        {
            "writer": Principal("writer", "team-a", spaces=frozenset({"space-a"})),
            "reader": Principal(
                "reader", "team-a", spaces=frozenset({"space-a"}), permissions=frozenset({"read"})
            ),
            "other": Principal("other", "team-b", spaces=frozenset({"space-a"})),
            "outside": Principal("outside", "team-a", spaces=frozenset({"space-b"})),
        }
    )
    runtime = Runtime(f"sqlite:///{tmp_path / 'identity.sqlite'}", identity=identity)
    with TestClient(create_app(runtime)) as client:
        with runtime.engine.begin() as db:
            db.execute(runtime.plugins['spaces'].spaces.insert().values(id='space-a', tenant_id='team-a', name='A', owner_id='writer'))
        assert client.get("/api/km/v1/spaces").status_code == 401
        writer = {"Authorization": "Bearer writer"}
        reader = {"Authorization": "Bearer reader"}
        response = client.post(
            "/api/km/v1/knowledge-bases", json={"space_id": "space-a", "name": "A"}, headers=writer
        )
        assert response.status_code == 201, response.text
        kb_id = response.json()["id"]
        assert (
            client.get(
                f"/api/km/v1/knowledge-bases/{kb_id}?space_id=space-a", headers=reader
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/km/v1/knowledge-bases",
                json={"space_id": "space-a", "name": "Denied"},
                headers=reader,
            ).status_code
            == 403
        )
        assert (
            client.get(
                f"/api/km/v1/knowledge-bases/{kb_id}?space_id=space-a",
                headers={"Authorization": "Bearer other"},
            ).status_code
            == 404
        )
        assert (
            client.get(
                f"/api/km/v1/knowledge-bases/{kb_id}?space_id=space-a",
                headers={"Authorization": "Bearer outside"},
            ).status_code
            == 403
        )
        # A body actor cannot manufacture a trusted identity.
        assert (
            client.post(
                "/api/km/v1/knowledge-bases",
                json={"space_id": "space-a", "name": "A", "actor": "writer"},
                headers=writer,
            ).status_code
            == 422
        )


def test_identity_failure_does_not_fall_back_to_local(tmp_path):
    class FailedIdentity:
        def authenticate(self, authorization):
            raise RuntimeError("identity unavailable")

    runtime = Runtime(f"sqlite:///{tmp_path / 'identity.sqlite'}", identity=FailedIdentity())
    with TestClient(create_app(runtime)) as client:
        assert client.get("/api/km/v1/spaces").status_code == 503
        assert client.get("/health/live").status_code == 200


def test_restart_marks_unfinished_jobs_interrupted_and_clears_payload(tmp_path):
    url = f"sqlite:///{tmp_path / 'restart.sqlite'}"
    runtime = Runtime(url)
    runtime.initialize()
    with runtime.engine.begin() as db:
        db.execute(s.jobs.insert().values(id='job', filename='policy.md', status='running', stage='parsing', progress=0,
            payload={'content_base64': 'c2VjcmV0'}, context={}, principal={}, created_at=s.now(), updated_at=s.now()))
    runtime.close()
    second = Runtime(url)
    second.initialize()
    with second.engine.connect() as db:
        row = db.execute(select(s.jobs)).mappings().one()
        assert row['status'] == 'failed'
        assert row['error'] == 'PROCESS_RESTART_INTERRUPTED'
        assert row['finished_at'] is not None and row['payload'] is None
    second.close()


def test_parser_failure_persists_terminal_state(tmp_path):
    runtime = Runtime(f"sqlite:///{tmp_path / 'parse.sqlite'}")
    with TestClient(create_app(runtime)) as client:
        kb = client.post("/api/km/v1/knowledge-bases", json={"name": "Failure"}).json()
        response = client.post(
            f"/api/km/v1/knowledge-bases/{kb['id']}/documents",
            json={
                "filename": "unsupported.exe",
                "content_base64": base64.b64encode(b"data").decode(),
            },
        )
        job = wait_job(client, response.json()["id"])
        assert job["status"] == "failed"
        assert job["finished_at"]
        assert "content_base64" not in job["metadata"]
