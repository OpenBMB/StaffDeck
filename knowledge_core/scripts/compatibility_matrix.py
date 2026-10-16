"""Generate a bounded compatibility matrix for KM entry points.

This is an executable inventory plus representative response snapshot. It
never claims historical legacy wire compatibility without caller fixtures.
"""

from __future__ import annotations

import json
import sys
import base64
import re
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from knowledge_core.api import create_app
from knowledge_core.runtime import Runtime
from knowledge_core.configuration import Configuration
from knowledge_core.identity import Principal, StaticBearerIdentity


LEGACY_SDK_FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "legacy_sdk_response_e39.json"
LEGACY_SDK_COMMIT = "e39ec532"


def route_inventory(app) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for route in app.routes:
        if not hasattr(route, "methods"):
            continue
        methods = sorted(method for method in (route.methods or set()) if method not in {"HEAD", "OPTIONS"})
        if methods:
            rows.append({"path": route.path, "methods": methods})
    return sorted(rows, key=lambda row: str(row["path"]))


def stable(value):
    """Keep contract-relevant shape while removing run-specific identifiers."""
    if isinstance(value, dict):
        return {key: stable(item) for key, item in sorted(value.items())
                if key not in {"id", "created_at", "updated_at", "started_at", "finished_at"}}
    if isinstance(value, list):
        return [stable(item) for item in value]
    if isinstance(value, str):
        if re.fullmatch(r"(?:space|kb|kbver|kdoc|kbucket|kchunk|job|binding|branch|suggestion|publication)_[0-9a-f]+", value):
            return "<id>"
        try:
            datetime.fromisoformat(value)
            return "<timestamp>"
        except ValueError:
            return value
    return value


def response_shape(response):
    try:
        payload = response.json()
    except ValueError:
        payload = response.text
    return {"status": response.status_code, "body": stable(payload)}


def load_legacy_sdk_fixture() -> dict[str, object]:
    """Load the checked-in replay from the historical SDK checkout.

    The fixture is intentionally data, rather than a hand-written mock.  Keep
    the source commit and observed wire paths in the matrix so a later update
    cannot silently replace historical evidence with a current response.
    """
    payload = json.loads(LEGACY_SDK_FIXTURE.read_text(encoding="utf-8"))
    if payload.get("source_commit") != LEGACY_SDK_COMMIT:
        raise RuntimeError("legacy SDK fixture source commit drifted")
    if payload.get("method_count") != len(LEGACY_SDK_WIRE["methods"]):
        raise RuntimeError("legacy SDK fixture method count drifted")
    responses = payload.get("responses")
    if not isinstance(responses, list) or len(responses) < len(LEGACY_SDK_WIRE["methods"]):
        raise RuntimeError("legacy SDK fixture has too few responses")
    observed_paths = set(payload.get("wire_paths_observed") or [])
    def path_pattern(path: str) -> re.Pattern[str]:
        # The fixture records the SDK transport path after the client's
        # ``/agents/{agent_id}/knowledge-bases`` prefix was projected away.
        path = path.replace("/agents/{agent_id}/knowledge-bases", "")
        escaped = re.escape(path)
        escaped = escaped.replace(r"\{agent_id\}", r"[^/]+")
        escaped = escaped.replace(r"\{knowledge_base_id\}", r"[^/]+")
        escaped = escaped.replace(r"\{document_id\}", r"[^/]+")
        return re.compile(r"^" + escaped + r"(?::archive)?$")

    def observed_path(path: str) -> str:
        return re.sub(r"^/agents/[^/]+/knowledge-bases", "", path)

    required_paths = [item["path"] for item in LEGACY_SDK_WIRE["methods"].values()]
    if not all(any(path_pattern(path).match(observed) for observed in observed_paths) for path in required_paths):
        raise RuntimeError("legacy SDK fixture does not cover every method path")
    for item in responses:
        if not isinstance(item, dict) or item.get("method") not in {"GET", "POST", "PATCH"}:
            raise RuntimeError("legacy SDK fixture contains an invalid response record")
        if not isinstance(item.get("status"), int) or not isinstance(item.get("body"), (dict, list, str, type(None))):
            raise RuntimeError("legacy SDK fixture contains an invalid response shape")
    return payload


def legacy_sdk_response_contract(fixture: dict[str, object]) -> dict[str, object]:
    """Summarize stable historical response contracts by old SDK operation."""
    responses = fixture["responses"]
    assert isinstance(responses, list)
    methods = LEGACY_SDK_WIRE["methods"]
    result: dict[str, object] = {}
    def path_pattern(path: str) -> re.Pattern[str]:
        path = path.replace("/agents/{agent_id}/knowledge-bases", "")
        escaped = re.escape(path)
        escaped = escaped.replace(r"\{agent_id\}", r"[^/]+")
        escaped = escaped.replace(r"\{knowledge_base_id\}", r"[^/]+")
        escaped = escaped.replace(r"\{document_id\}", r"[^/]+")
        return re.compile(r"^" + escaped + r"(?::archive)?$")

    def observed_path(path: str) -> str:
        return re.sub(r"^/agents/[^/]+/knowledge-bases", "", path)

    for name, contract in methods.items():
        path = str(contract["path"])
        matching = [row for row in responses
                    if row.get("method") == contract["http_method"]
                    and path_pattern(path).match(observed_path(str(row.get("path", ""))))]
        result[name] = {
            "path": path,
            "http_method": contract["http_method"],
            "statuses": sorted({row["status"] for row in matching}),
            "response_count": len(matching),
            "body_keys": sorted({key for row in matching for key in (row.get("body") or {}).keys()})
            if matching and all(isinstance(row.get("body"), dict) for row in matching) else [],
            "stable_body_keys": sorted({key for row in matching for key in stable(row.get("body") or {}).keys()})
            if matching and all(isinstance(row.get("body"), dict) for row in matching) else [],
        }
    return result


def core_snapshot(client):
    """Exercise the independent API's representative compatibility surface."""
    rows = []

    def record(name, response):
        rows.append({"operation": name, **response_shape(response)})
        return response

    record("spaces.list.default", client.get("/api/km/v1/spaces"))
    record("spaces.create", client.post("/api/km/v1/spaces", json={"id": "matrix-space", "name": "Matrix"}))
    record("bases.create.invalid", client.post("/api/km/v1/knowledge-bases", params={"space_id": "matrix-space"}, json={"space_id": "matrix-space", "name": ""}))
    created = record("bases.create", client.post("/api/km/v1/knowledge-bases", json={"space_id": "matrix-space", "name": "Matrix Base", "description": "snapshot"}))
    if created.status_code != 201:
        return rows
    kb = created.json()["id"]
    record("bases.create.duplicate", client.post("/api/km/v1/knowledge-bases", json={"space_id": "matrix-space", "name": "Matrix Base"}))
    current = record("bases.get", client.get(f"/api/km/v1/knowledge-bases/{kb}", params={"space_id": "matrix-space"}))
    stale = record("bases.update.conflict", client.put(f"/api/km/v1/knowledge-bases/{kb}", params={"space_id": "matrix-space"}, json={"expected_updated_at": "2000-01-01T00:00:00", "name": "stale"}))
    record("bases.versions.list", client.get(f"/api/km/v1/knowledge-bases/{kb}/versions", params={"space_id": "matrix-space"}))
    record("documents.invalid", client.post(
        f"/api/km/v1/knowledge-bases/{kb}/documents",
        params={"space_id": "matrix-space"},
        json={"space_id": "matrix-space", "filename": "bad.md", "content_base64": "!"},
    ))
    record("bases.hide", client.delete(f"/api/km/v1/knowledge-bases/{kb}", params={"space_id": "matrix-space"}))
    record("bases.hidden.list", client.get("/api/km/v1/knowledge-bases", params={"space_id": "matrix-space"}))
    record("bases.restore.binding", client.put(f"/api/km/v1/knowledge-bases/{kb}/binding", params={"space_id": "matrix-space"}, json={"hidden": False}))
    record("bases.restored.list", client.get("/api/km/v1/knowledge-bases", params={"space_id": "matrix-space"}))
    record("query.invalid", client.post("/api/km/v1/query", json={"space_id": "matrix-space"}))
    return rows


LEGACY_SDK_WIRE = {
    "source": "e39ec532:sdk/python/src/staffdeck/knowledge.py",
    "tests": "e39ec532:sdk/python/tests/test_knowledge.py",
    "base_path": "/proxy/api/v1/agents/{agent_id}/knowledge-bases/{knowledge_base_id}",
    "methods": {
        "list": {"http_method": "GET", "path": "/agents/{agent_id}/knowledge-bases"},
        "create": {"http_method": "POST", "path": "/agents/{agent_id}/knowledge-bases", "body": "dict"},
        "update": {"http_method": "PATCH", "path": "/agents/{agent_id}/knowledge-bases/{knowledge_base_id}", "body": "dict"},
        "archive": {"http_method": "POST", "path": "/agents/{agent_id}/knowledge-bases/{knowledge_base_id}:archive"},
        "search": {"http_method": "POST", "path": "/agents/{agent_id}/knowledge-bases/{knowledge_base_id}:search", "body": "dict"},
        "upsert_entries": {
            "http_method": "POST", "path": "/agents/{agent_id}/knowledge-bases/{knowledge_base_id}/entries",
            "body": "dict", "optional_headers": ["idempotency-key"],
        },
        "upload_document": {
            "http_method": "POST", "path": "/agents/{agent_id}/knowledge-bases/{knowledge_base_id}/documents",
            "multipart": {"file": "filename/content/media_type", "title": "form"},
            "client_limit_bytes": 20 * 1024 * 1024,
        },
        "documents": {"http_method": "GET", "path": "/agents/{agent_id}/knowledge-bases/{knowledge_base_id}/documents"},
        "update_document": {"http_method": "PATCH", "path": "/agents/{agent_id}/knowledge-bases/{knowledge_base_id}/documents/{document_id}", "body": "dict"},
        "archive_document": {"http_method": "POST", "path": "/agents/{agent_id}/knowledge-bases/{knowledge_base_id}/documents/{document_id}:archive"},
        "versions": {"http_method": "GET", "path": "/agents/{agent_id}/knowledge-bases/{knowledge_base_id}/versions"},
        "rollback": {"http_method": "POST", "path": "/agents/{agent_id}/knowledge-bases/{knowledge_base_id}:rollback", "body": {"version": "string"}},
        "concepts": {"http_method": "GET", "path": "/agents/{agent_id}/knowledge-bases/{knowledge_base_id}/concepts"},
    },
}


def module_envelope(operation, value, *, request_id):
    return {
        "kind": "request", "method": "module_call", "messageId": f"matrix-{request_id}",
        "runId": "matrix-run", "operationId": f"matrix-op-{request_id}",
        "requestId": request_id, "module": "knowledge",
        "payload": {"operation": operation, "input": value},
    }


def module_snapshot():
    """Run the actual StaffDeck module dispatcher against an isolated host DB."""
    from app.api.module_knowledge import router
    from app.db import get_session
    from app.db.models import AgentProfile, KnowledgeBase, Tenant, User
    from app.agents.branching import ensure_open_gallery_binding, mark_resource_open_gallery

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    app = FastAPI()
    app.include_router(router)
    with Session(engine, expire_on_commit=False) as db:
        db.add(Tenant(id="matrix-tenant", name="Matrix Tenant"))
        db.add(User(id="matrix-user", tenant_id="matrix-tenant", username="matrix", role="admin", password_hash=""))
        db.add(AgentProfile(id="matrix-agent", tenant_id="matrix-tenant", name="Matrix Agent", is_overall=True))
        base = KnowledgeBase(id="matrix-host-base", tenant_id="matrix-tenant", name="Host Base")
        db.add(base)
        db.flush()
        mark_resource_open_gallery(base, {})
        ensure_open_gallery_binding(db, "matrix-tenant", "knowledge_base", base.id, "active", metadata_json={})
        db.commit()

    def override_session():
        with Session(engine, expire_on_commit=False) as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    rows = []

    def record(name, response):
        rows.append({"operation": name, **response_shape(response)})
        return response

    with TestClient(app) as client:
        record("manifest", client.get("/module-manifest"))
        record("malformed", client.post("/v2/module/call", json={"messageId": "broken", "requestId": "broken", "payload": {}}))
        record("unknown_operation", client.post("/v2/module/call", json=module_envelope("invented", {}, request_id="unknown")))
        common = {"tenantId": "matrix-tenant", "agentId": "matrix-agent", "actorUserId": "matrix-user"}
        record("list_bases", client.post("/v2/module/call", json=module_envelope("list_bases", common, request_id="list")))
        record("get_missing", client.post("/v2/module/call", json=module_envelope("get_base", {**common, "knowledgeBaseId": "missing"}, request_id="missing")))
        record("query_empty", client.post("/v2/module/call", json=module_envelope("query", {**common, "query": ""}, request_id="query-empty")))
        record("create_base", client.post("/v2/module/call", json=module_envelope("create_base", {**common, "name": "Matrix Created"}, request_id="create")))
    engine.dispose()
    return rows


def public_snapshot():
    """Run the public facade with a temporary admin principal and host DB."""
    from app.db import get_session
    from app.db.models import AgentProfile, KnowledgeBase, Tenant, User
    from app.agents.branching import ensure_open_gallery_binding, mark_resource_open_gallery
    from app.public_api import staffdeck_facade
    from app.public_api.app import create_public_api_app
    from app.public_api.auth import get_public_principal, PublicPrincipal

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    actor = User(id="matrix-public-user", tenant_id="matrix-public", username="matrix-public", role="admin", password_hash="")
    principal = PublicPrincipal("matrix-public", actor, frozenset({"knowledge:read", "knowledge:write"}))
    with Session(engine, expire_on_commit=False) as db:
        db.add(Tenant(id="matrix-public", name="Matrix Public"))
        db.add(actor)
        db.add(AgentProfile(id="matrix-public-agent", tenant_id="matrix-public", name="Public Agent", is_overall=True))
        base = KnowledgeBase(id="matrix-public-base", tenant_id="matrix-public", name="Public Base")
        db.add(base)
        db.flush()
        mark_resource_open_gallery(base, {})
        ensure_open_gallery_binding(db, "matrix-public", "knowledge_base", base.id, "active", metadata_json={})
        db.commit()

    app = create_public_api_app()

    def override_session():
        with Session(engine, expire_on_commit=False) as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_public_principal] = lambda: principal
    rows = []

    def record(name, response):
        rows.append({"operation": name, **response_shape(response)})
        return response

    with TestClient(app) as client:
        record("list_bases", client.get("/agents/matrix-public-agent/knowledge-bases"))
        record("list_bases_missing_agent", client.get("/agents/missing/knowledge-bases"))
        created = record("create_base", client.post("/agents/matrix-public-agent/knowledge-bases", json={"name": "Public Created"}))
        base_id = created.json().get("id", "matrix-public-base") if created.status_code < 400 else "matrix-public-base"
        record("update_base", client.patch(
            f"/agents/matrix-public-agent/knowledge-bases/{base_id}",
            json={"name": "Public Updated"},
        ))
        record("search_base", client.post(
            f"/agents/matrix-public-agent/knowledge-bases/{base_id}:search",
            json={"query": "matrix"},
        ))
        record("list_versions", client.get(
            f"/agents/matrix-public-agent/knowledge-bases/{base_id}/versions",
        ))
        record("list_documents", client.get(
            f"/agents/matrix-public-agent/knowledge-bases/{base_id}/documents",
        ))
        record("list_concepts", client.get(
            f"/agents/matrix-public-agent/knowledge-bases/{base_id}/concepts",
        ))
        record("archive_base", client.post(
            f"/agents/matrix-public-agent/knowledge-bases/{base_id}:archive",
        ))
        record("scope_override", client.post("/agents/matrix-public-agent/knowledge-bases", json={"name": "Bad", "tenant_id": "other"}))
        record("get_missing_document", client.get("/agents/matrix-public-agent/knowledge-bases/matrix-public-base/documents/missing"))
    engine.dispose()
    return rows


def main(output_path: str) -> int:
    database = tempfile.NamedTemporaryFile(prefix="km-compat-", suffix=".sqlite", delete=False)
    database.close()
    runtime = Runtime(f"sqlite:///{database.name}", configuration=Configuration('full'))
    legacy_fixture = load_legacy_sdk_fixture()
    report: dict[str, object] = {
        "status": "partial",
        "schema": "km-compatibility-matrix/v5",
        "entry_points": {},
        "snapshots": {"knowledge_core.v1": [], "staffdeck.knowledge.v1": [], "staffdeck.public": [], "python_sdk": []},
        "legacy_contracts": {"python_sdk": LEGACY_SDK_WIRE},
        "coverage": {"status": "partial", "complete": False, "not_snapshot": []},
        "legacy_sdk_fixture": {
            "path": str(LEGACY_SDK_FIXTURE),
            "source_commit": legacy_fixture["source_commit"],
            "response_count": len(legacy_fixture["responses"]),
            "method_count": legacy_fixture["method_count"],
            "contracts": legacy_sdk_response_contract(legacy_fixture),
        },
    }
    try:
        with TestClient(create_app(runtime)) as client:
            report["entry_points"]["knowledge_core.v1"] = {
                "prefix": "/api/km/v1",
                "routes": route_inventory(client.app),
            }
            spaces = client.get("/api/km/v1/spaces")
            assert spaces.status_code == 200
            report["snapshots"]["knowledge_core.v1"] = core_snapshot(client)
            invalid = client.post("/api/km/v1/spaces", json={})
            report["snapshots"]["knowledge_core.v1"].append({"operation": "spaces.create.missing_name", **response_shape(invalid)})
        from app.api.module_knowledge import METHODS, module_manifest
        manifest = module_manifest()
        report["entry_points"]["staffdeck.knowledge.v1"] = {
            "manifest_path": "/module-manifest",
            "call_path": "/v2/module/call",
            "protocol": manifest,
            "routes": {"/module-manifest": ["GET"], "/v2/module/call": ["POST"]},
            "methods": list(METHODS),
        }
        report["snapshots"]["staffdeck.knowledge.v1"] = [
            {"operation": "manifest", "status": 200, "body": stable(manifest)},
            {"operation": "method_inventory", "status": 200, "body": {"methods": list(METHODS)}},
        ]
        report["snapshots"]["staffdeck.knowledge.v1"].extend(module_snapshot())
        from app.public_api.app import create_public_api_app
        public_app = create_public_api_app()
        public_routes = route_inventory(public_app)
        knowledge_routes = [row for row in public_routes if "knowledge" in str(row["path"])]
        report["entry_points"]["staffdeck.public"] = {
            "routes": knowledge_routes,
            "representative_status": "route_inventory_and_schema",
        }
        report["snapshots"]["staffdeck.public"] = [
            {"operation": "route_contract", "status": 200,
             "body": {"routes": knowledge_routes}},
        ]
        report["snapshots"]["staffdeck.public"].extend(public_snapshot())
        from knowledge_core.client import KnowledgeClient
        report["snapshots"]["python_sdk"] = [
            {"operation": "legacy_method_inventory", "status": 200,
             "body": {"methods": sorted(LEGACY_SDK_WIRE["methods"])}},
            {"operation": "knowledge_core_method_inventory", "status": 200,
             "body": {"methods": sorted(name for name in dir(KnowledgeClient) if not name.startswith("_") )}},
        ]
        current_public = report["snapshots"]["staffdeck.public"]
        assert isinstance(current_public, list)
        current_by_operation = {row["operation"]: row for row in current_public if "operation" in row}
        historical_statuses = {
            name: contract["statuses"]
            for name, contract in report["legacy_sdk_fixture"]["contracts"].items()
        }
        # The public facade replay exercises the synchronous resource methods;
        # jobs and multipart operations remain covered by their focused tests.
        operation_status_map = {
            "list": "list_bases", "create": "create_base", "update": "update_base",
            "archive": "archive_base", "search": "search_base", "versions": "list_versions",
            "documents": "list_documents", "concepts": "list_concepts",
        }
        status_checks = {}
        for method, operation in operation_status_map.items():
            actual = current_by_operation.get(operation, {}).get("status")
            expected = historical_statuses.get(method, [])
            historical_contract = report["legacy_sdk_fixture"]["contracts"].get(method, {})
            current_body = current_by_operation.get(operation, {}).get("body") or {}
            current_keys = sorted(current_body.keys()) if isinstance(current_body, dict) else []
            expected_keys = historical_contract.get("stable_body_keys", [])
            status_checks[method] = {
                "expected_historical_statuses": expected,
                "current_status": actual,
                "expected_stable_body_keys": expected_keys,
                "current_stable_body_keys": current_keys,
                "matched": bool(actual in expected and current_keys == expected_keys) if expected else False,
            }
        report["legacy_sdk_fixture"]["current_status_checks"] = status_checks
        report["coverage"]["not_snapshot"] = [
            "legacy async job completion and multipart parsing are covered by focused facade/client tests",
            "historical bodies contain host identity metadata; current facade snapshots are compared by stable field shape and status",
        ]
        report["coverage"]["legacy_sdk_wire_complete"] = True
        report["coverage"]["legacy_sdk_fixture_complete"] = True
        report["coverage"]["legacy_sdk_statuses_complete"] = all(item["matched"] for item in status_checks.values())
        report["coverage"]["legacy_sdk_body_shapes_complete"] = report["coverage"]["legacy_sdk_statuses_complete"]
        report["coverage"]["public_facade_replay"] = True
        report["coverage"]["snapshot_counts"] = {key: len(value) for key, value in report["snapshots"].items()}
        report["coverage"]["representative_checks_passed"] = bool(
            report["coverage"]["legacy_sdk_wire_complete"]
            and report["coverage"]["legacy_sdk_fixture_complete"]
            and report["coverage"]["legacy_sdk_statuses_complete"]
            and report["coverage"]["legacy_sdk_body_shapes_complete"]
            and report["coverage"]["public_facade_replay"]
        )
        report["coverage"]["complete"] = False
        report["coverage"]["status"] = "not_accepted"
        report["status"] = "not_accepted"
        report["acceptance_gaps"] = [
            "No 77c21973 recursive response comparison",
            "Current replay does not require independent KM HTTP",
            "Not all REST, SDK and manifest operations and errors are replayed",
        ]
    finally:
        runtime.close()
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "snapshot_counts": report["coverage"]["snapshot_counts"], "output": str(path)}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/km-compatibility-matrix-1014-v2.json"))
