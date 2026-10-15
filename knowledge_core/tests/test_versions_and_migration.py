import base64
import json

from fastapi.testclient import TestClient
from knowledge_core.full_api import create_app as create_legacy_fixture_app
from knowledge_core.api import create_app
from knowledge_core.migration import compare, migrate, preflight
from knowledge_core.models import KnowledgeChunk
from knowledge_core.full_runtime import Runtime as LegacyRuntime
from support import full_runtime as Runtime
from sqlalchemy import text
from sqlmodel import select
from test_independent_workflow import wait_job


def test_legacy_branches_visibility_history_and_publication_receipts(tmp_path):
    from knowledge_core.migration import SPACE_TABLES
    from knowledge_core.models import KnowledgeDiscoverySuggestion, Publication, VersionAccess
    from knowledge_core.publication import publish_suggestion
    from sqlalchemy import create_engine

    source = tmp_path / "legacy.sqlite"
    runtime = LegacyRuntime(f"sqlite:///{source}")
    with TestClient(create_legacy_fixture_app(runtime)) as client:
        kb = client.post("/api/km/v1/knowledge-bases", json={"name": "Historical"}).json()["id"]
        first = client.get(f"/api/km/v1/knowledge-bases/{kb}").json()["branch"]["head_version_id"]
        second = client.post(f"/api/km/v1/knowledge-bases/{kb}/versions/copy", json={}).json()["head_version_id"]
        versions = {row["id"]: row["version"] for row in client.get(f"/api/km/v1/knowledge-bases/{kb}/versions").json()}
        with runtime.session() as db:
            db.add(KnowledgeDiscoverySuggestion(id="known", tenant_id="local", knowledge_base_id=kb,
                knowledge_base_version_id=first, document_id="", suggestion_type="procedure", title="Known", status="confirmed"))
            db.commit()
    engine = create_engine(f"sqlite:///{source}")
    timestamp = "2026-10-13 00:00:00"
    with engine.begin() as connection:
        for name in SPACE_TABLES:
            connection.execute(text(f'DROP TABLE "{name}"'))
        connection.execute(text("CREATE TABLE agent_profiles (id TEXT PRIMARY KEY, tenant_id TEXT, name TEXT, is_overall BOOLEAN, created_at DATETIME, updated_at DATETIME)"))
        connection.execute(text("CREATE TABLE agent_resource_bindings (id TEXT PRIMARY KEY, tenant_id TEXT, agent_id TEXT, resource_type TEXT, resource_id TEXT, status TEXT, metadata_json JSON, created_at DATETIME, updated_at DATETIME)"))
        connection.execute(text("CREATE TABLE agent_knowledge_branches (id TEXT PRIMARY KEY, tenant_id TEXT, agent_id TEXT, knowledge_base_id TEXT, head_version TEXT, base_version TEXT, status TEXT, sync_state TEXT, created_at DATETIME, updated_at DATETIME)"))
        connection.execute(text("CREATE TABLE knowledge_publication_receipts (id TEXT PRIMARY KEY, tenant_id TEXT, suggestion_id TEXT, idempotency_key TEXT, target_reference_json JSON, created_at DATETIME)"))
        for employee in ("owner", "reader", "hidden"):
            connection.execute(text("INSERT INTO agent_profiles VALUES (:id,'local',:id,0,:time,:time)"), {"id": employee, "time": timestamp})
            connection.execute(text("INSERT INTO agent_resource_bindings VALUES (:id,'local',:agent,'knowledge_base',:kb,:status,'{}',:time,:time)"),
                {"id": "binding-" + employee, "agent": employee, "kb": kb, "status": "deleted" if employee == "hidden" else "active", "time": timestamp})
        connection.execute(text("UPDATE knowledge_bases SET metadata_json=:metadata WHERE id=:kb"),
            {"kb": kb, "metadata": json.dumps({"owner_agent_id": "owner", "current_version": versions[first]})})
        connection.execute(text("UPDATE knowledge_base_versions SET metadata_json=:metadata WHERE id=:id"),
            {"id": second, "metadata": json.dumps({"owner_agent_id": "owner"})})
        connection.execute(text("INSERT INTO agent_knowledge_branches VALUES ('owner-branch','local','owner',:kb,:head,:base,'active','modified',:time,:time)"),
            {"kb": kb, "head": versions[second], "base": versions[first], "time": timestamp})
        connection.execute(text("INSERT INTO knowledge_publication_receipts VALUES ('receipt','local','known','km:local:known',:reference,:time)"),
            {"reference": json.dumps({"skill_id": "existing-host-resource"}), "time": timestamp})
    engine.dispose()
    target = f"sqlite:///{tmp_path / 'target.sqlite'}"
    assert preflight(source)["ok"], preflight(source)
    assert migrate(source, target)["ok"]
    assert migrate(source, target)["ok"]
    assert compare(source, target)["ok"]
    runtime = Runtime(target)
    with TestClient(create_app(runtime)) as client:
        owner = client.get(f"/api/km/v1/knowledge-bases/{kb}?space_id=sd:local:owner").json()["branch"]
        assert owner["head_version_id"] == second
        assert owner["base_version_id"] == first
        assert owner["created_at"].startswith("2026-10-13")
        reader = client.get(f"/api/km/v1/knowledge-bases/{kb}?space_id=sd:local:reader").json()["branch"]
        assert reader["head_version_id"] == first
        assert client.get("/api/km/v1/knowledge-bases?space_id=sd:local:hidden").json() == []
        from sqlalchemy import select as table_select
        with runtime.engine.connect() as db:
            access = runtime.plugins['spaces'].version_access
            assert db.execute(table_select(access).where(access.c.space_id == 'sd:local:owner', access.c.version_id == second)).first()
            assert db.execute(table_select(access).where(access.c.space_id == 'sd:local:reader', access.c.version_id == second)).first() is None
            receipt = db.execute(table_select(runtime.plugins['publication'].receipts)).mappings().one()
            assert receipt['reference'] == {"skill_id": "existing-host-resource"}


def test_existing_km_schema_upgrade_preserves_binding(tmp_path):
    from knowledge_core.database import initialize_schema
    from sqlalchemy import create_engine

    path = tmp_path / "old-km.sqlite"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE knowledge_spaces (id TEXT PRIMARY KEY, tenant_id TEXT, name TEXT, owner_id TEXT, owner_name TEXT, status TEXT, created_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE knowledge_bindings (id TEXT PRIMARY KEY, tenant_id TEXT, space_id TEXT, knowledge_base_id TEXT, hidden BOOLEAN, created_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_spaces VALUES ('s','local','S','local','Local','active','2026-10-13 00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_bindings VALUES ('b','local','s','kb',0,'2026-10-13 00:00:00')"
            )
        )
    initialize_schema(engine)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA user_version")).scalar_one() == 2
        binding = (
            connection.execute(text("SELECT * FROM knowledge_bindings"))
            .mappings()
            .one()
        )
        assert binding["metadata_json"] == "{}"
        assert binding["updated_at"] == binding["created_at"]
    engine.dispose()


def test_spaces_copy_sync_publish_rollback_and_reference_remapping(tmp_path):
    runtime = Runtime(f"sqlite:///{tmp_path / 'versions.sqlite'}")
    with TestClient(create_app(runtime)) as client:
        kb_id = client.post(
            "/api/km/v1/knowledge-bases", json={"name": "Policy"}
        ).json()["id"]
        remote = client.post("/api/km/v1/spaces", json={"name": "Remote"}).json()["id"]
        bound = client.post(
            f"/api/km/v1/knowledge-bases/{kb_id}/bindings", json={"space_id": remote}
        )
        assert bound.status_code == 200
        uploaded = client.post(
            f"/api/km/v1/knowledge-bases/{kb_id}/documents",
            json={
                "filename": "policy.md",
                "content_base64": base64.b64encode(
                    b"# Expense policy\n\nExpense policy requires a receipt."
                ).decode(),
            },
        ).json()
        job = wait_job(client, uploaded["id"])
        assert job["status"] == "succeeded", job
        assert (
            client.get(
                f"/api/km/v1/knowledge-bases/{kb_id}/documents?space_id={remote}"
            ).json()
            == []
        )
        original_document_id = job["document_id"]
        original = client.get(f"/api/km/v1/documents/{original_document_id}").json()
        original_chunks = client.get(
            f"/api/km/v1/documents/{original_document_id}/chunks"
        ).json()
        source_version_id = original["knowledge_base_version_id"]
        assert (
            client.get(
                f"/api/km/v1/documents/{original_document_id}?space_id={remote}"
            ).status_code
            == 404
        )
        assert (
            client.get(
                f"/api/km/v1/citations/{original_chunks[0]['id']}?space_id={remote}"
            ).status_code
            == 404
        )
        published = client.post(
            f"/api/km/v1/knowledge-bases/{kb_id}/versions/publish", json={}
        )
        assert published.status_code == 200
        assert (
            client.put(
                f"/api/km/v1/documents/{original_document_id}",
                json={
                    "expected_updated_at": original["updated_at"],
                    "content_md": "Published edit",
                },
            ).status_code
            == 409
        )
        copied = client.post(
            f"/api/km/v1/knowledge-bases/{kb_id}/versions/copy", json={}
        )
        assert copied.status_code == 200
        target_version_id = copied.json()["head_version_id"]
        assert target_version_id != source_version_id
        documents = client.get(f"/api/km/v1/knowledge-bases/{kb_id}/documents").json()
        assert len(documents) == 1
        assert documents[0]["id"] != original_document_id
        chunks = client.get(f"/api/km/v1/documents/{documents[0]['id']}/chunks").json()
        assert all(row["document_id"] == documents[0]["id"] for row in chunks)
        assert not {row["id"] for row in chunks} & {
            row["id"] for row in original_chunks
        }
        updated = client.put(
            f"/api/km/v1/documents/{documents[0]['id']}",
            json={
                "expected_updated_at": documents[0]["updated_at"],
                "content_md": "# Expense policy\n\nExpense policy requires manager approval.",
            },
        )
        assert updated.status_code == 200, updated.text
        assert (
            client.get(f"/api/km/v1/citations/{original_chunks[0]['id']}").status_code
            == 200
        )
        rolled_back = client.post(
            f"/api/km/v1/knowledge-bases/{kb_id}/versions/rollback",
            json={"version_id": source_version_id},
        )
        assert rolled_back.status_code == 200
        synced = client.post(
            f"/api/km/v1/knowledge-bases/{kb_id}/versions/sync?space_id={remote}",
            json={},
        )
        assert synced.status_code == 200
        assert synced.json()["head_version_id"] == source_version_id
        assert (
            client.delete(f"/api/km/v1/knowledge-bases/{kb_id}").json()["status"]
            == "hidden"
        )
        assert client.get("/api/km/v1/knowledge-bases").json() == []
        # Other space remains bound, and unhide is explicit.
        assert (
            len(client.get(f"/api/km/v1/knowledge-bases?space_id={remote}").json()) == 1
        )
        assert (
            client.post(
                f"/api/km/v1/knowledge-bases/{kb_id}/bindings?source_space_id={remote}",
                json={"space_id": "local", "hidden": False},
            ).status_code
            == 200
        )
        assert (
            client.delete(
                f"/api/km/v1/knowledge-bases/{kb_id}?permanent=true"
            ).status_code
            == 200
        )
        assert (
            client.get(f"/api/km/v1/citations/{original_chunks[0]['id']}").status_code
            == 404
        )


def test_read_only_repeatable_migration_and_missing_link_report(tmp_path):
    source_path = tmp_path / "source.sqlite"
    source_url = f"sqlite:///{source_path}"
    runtime = LegacyRuntime(source_url)
    with TestClient(create_legacy_fixture_app(runtime)) as client:
        kb_id = client.post(
            "/api/km/v1/knowledge-bases", json={"name": "Migrated"}
        ).json()["id"]
        upload = client.post(
            f"/api/km/v1/knowledge-bases/{kb_id}/documents",
            json={
                "filename": "policy.md",
                "content_base64": base64.b64encode(
                    b"# Travel policy\n\nTravel expenses require receipts."
                ).decode(),
            },
        ).json()
        assert wait_job(client, upload["id"])["status"] == "succeeded"
    target_url = f"sqlite:///{tmp_path / 'target.sqlite'}"
    assert preflight(source_path)["ok"]
    first = migrate(source_path, target_url)
    assert first["ok"], first
    from sqlalchemy import create_engine, inspect
    target_engine = create_engine(target_url)
    assert all(name.startswith('km_') for name in inspect(target_engine).get_table_names())
    target_engine.dispose()
    assert migrate(source_path, target_url)["ok"]
    assert compare(source_path, target_url)["ok"]
    with TestClient(create_app(Runtime(target_url))) as migrated:
        assert migrated.get("/api/km/v1/knowledge-bases").json()[0]["id"] == kb_id
        assert migrated.post(
            "/api/km/v1/query", json={"query": "Travel expenses receipts"}
        ).json()["chunks"]
        document = migrated.get('/api/km/v1/knowledge-bases/' + kb_id + '/documents').json()[0]
        assert migrated.put('/api/km/v1/documents/' + document['id'],
            json={'title': 'New target write', 'expected_updated_at': document['updated_at']}).status_code == 200
    assert not migrate(source_path, target_url)['ok']
    runtime = LegacyRuntime(source_url)
    with runtime.session() as db:
        chunk = db.exec(select(KnowledgeChunk)).first()
        chunk.document_id = "missing"
        db.add(chunk)
        db.commit()
    runtime.close()
    report = preflight(source_path)
    assert not report["ok"]
    assert any("missing document_id" in message for message in report["errors"])
    assert not migrate(source_path, target_url)["ok"]


def test_legacy_unbound_gallery_migrates_to_visible_space_and_repeats(tmp_path):
    source_path = tmp_path / "staffdeck-copy.sqlite"
    runtime = LegacyRuntime(f"sqlite:///{source_path}")
    with TestClient(create_legacy_fixture_app(runtime)) as client:
        kb_id = client.post(
            "/api/km/v1/knowledge-bases", json={"name": "Gallery policy"}
        ).json()["id"]
        upload = client.post(
            f"/api/km/v1/knowledge-bases/{kb_id}/documents",
            json={
                "filename": "policy.md",
                "content_base64": base64.b64encode(
                    b"# Travel policy\n\nTravel expenses require receipts."
                ).decode(),
            },
        ).json()
        assert wait_job(client, upload["id"])["status"] == "succeeded"
        chunk_id = client.post(
            "/api/km/v1/query", json={"query": "Travel expenses"}
        ).json()["chunks"][0]["id"]
        head = client.get(f"/api/km/v1/knowledge-bases/{kb_id}").json()["branch"][
            "head_version_id"
        ]
        head_version = next(
            row["version"]
            for row in client.get(f"/api/km/v1/knowledge-bases/{kb_id}/versions").json()
            if row["id"] == head
        )
    from knowledge_core.migration import SPACE_TABLES
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{source_path}")
    with engine.begin() as connection:
        for name in SPACE_TABLES:
            connection.execute(text(f'DROP TABLE "{name}"'))
        connection.execute(
            text(
                "CREATE TABLE agent_profiles (id TEXT PRIMARY KEY, tenant_id TEXT, name TEXT, is_overall BOOLEAN, created_at DATETIME, updated_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO agent_profiles VALUES ('overall', 'local', 'Overall', 1, '2026-10-13 00:00:00', '2026-10-13 00:00:00')"
            )
        )
        import json

        connection.execute(
            text("UPDATE knowledge_bases SET metadata_json=:metadata WHERE id=:id"),
            {
                "metadata": json.dumps({"current_version": head_version}),
                "id": kb_id,
            },
        )
    engine.dispose()
    target_url = f"sqlite:///{tmp_path / 'knowledge.sqlite'}"
    assert preflight(source_path)["ok"]
    assert migrate(source_path, target_url)["ok"]
    assert migrate(source_path, target_url)["ok"]
    assert compare(source_path, target_url)["ok"]
    with TestClient(create_app(Runtime(target_url))) as migrated:
        scope = "sd:local:overall"
        assert (
            migrated.get(
                "/api/km/v1/knowledge-bases", params={"space_id": scope}
            ).json()[0]["id"]
            == kb_id
        )
        assert migrated.post(
            "/api/km/v1/query", json={"space_id": scope, "query": "Travel expenses"}
        ).json()["chunks"]
        assert (
            migrated.get(
                f"/api/km/v1/citations/{chunk_id}", params={"space_id": scope}
            ).status_code
            == 200
        )
