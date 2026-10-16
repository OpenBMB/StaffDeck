"""Run identical isolated scenarios from an explicitly supplied StaffDeck checkout."""

import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--km-url")
    args = parser.parse_args()
    source = args.source.resolve()
    os.environ.update(ULTRARAG_DOTENV=str(source / ".missing-dotenv"), APP_SECRET="km-contract-acceptance")
    if args.km_url:
        os.environ.update(STAFFDECK_KM_URL=args.km_url, STAFFDECK_KM_BEARER="contract-owner")
    else:
        os.environ.pop("STAFFDECK_KM_URL", None)
    sys.path[:0] = [str(source / "backend"), str(source / "backend/src"), str(source / "sdk/python/src"), str(source)]
    import httpx
    import pytest
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlmodel import Session
    from backend.tests.test_public_api_v1 import _client, _tenant_key
    from app.api import knowledge, knowledge_bases, module_knowledge
    from app.db import get_session
    from app.db.models import User
    from app.security.auth import create_access_token
    if not args.km_url:
        from app.knowledge import service
    from app.public_api import jobs
    from app.public_api.pilotdeck_domain_host import PilotDeckDomainHostClient, bind_pilotdeck_domain_host
    from app.public_api.pilotdeck_domain_binding import FixedPilotDeckDomainHostClient
    if args.km_url:
        from knowledge_core.parser import extract_text
    else:
        from app.knowledge.parser import extract_text
    from staffdeck import StaffDeck, APIError

    class PublicHTTP:
        """Independent REST requests; SDK execution uses its own resource methods."""
        def __init__(self, transport, api_key):
            self.http = httpx.Client(base_url="http://testserver/api/v1/", headers={"Authorization": "Bearer " + api_key}, transport=httpx.MockTransport(transport))
            self.knowledge_bases = self
            self.jobs = SimpleNamespace(get=lambda identifier: self.request("GET", "jobs/" + identifier))

        def __enter__(self): return self
        def __exit__(self, *args): self.http.close()

        def request(self, method, path, **kwargs):
            response = self.http.request(method, path, **kwargs)
            return SimpleNamespace(data=response.json()) if response.is_success else None

        def path(self, agent, kb=None):
            return "agents/" + agent + "/knowledge-bases" + ("/" + kb if kb else "")

        def list(self, agent): return self.request("GET", self.path(agent))
        def create(self, agent, body): return self.request("POST", self.path(agent), json=body)
        def update(self, agent, kb, body): return self.request("PATCH", self.path(agent, kb), json=body)
        def archive(self, agent, kb): return self.request("POST", self.path(agent, kb) + ":archive")
        def search(self, agent, kb, body): return self.request("POST", self.path(agent, kb) + ":search", json=body)
        def upsert_entries(self, agent, kb, body, *, idempotency_key=None):
            return self.request("POST", self.path(agent, kb) + "/entries", json=body, headers={"Idempotency-Key": idempotency_key})
        def upload_document(self, agent, kb, file, *, title=None):
            return self.request("POST", self.path(agent, kb) + "/documents", files={"file": (file.name, file.read_bytes(), "text/markdown")}, data={"title": title})
        def documents(self, agent, kb): return self.request("GET", self.path(agent, kb) + "/documents")
        def update_document(self, agent, kb, doc, body): return self.request("PATCH", self.path(agent, kb) + "/documents/" + doc, json=body)
        def archive_document(self, agent, kb, doc): return self.request("POST", self.path(agent, kb) + "/documents/" + doc + ":archive")
        def versions(self, agent, kb): return self.request("GET", self.path(agent, kb) + "/versions")
        def concepts(self, agent, kb): return self.request("GET", self.path(agent, kb) + "/concepts")
        def rollback(self, agent, kb, version): return self.request("POST", self.path(agent, kb) + ":rollback", json={"version": version})

    def capability(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/describe"):
            return httpx.Response(200, json={"operations": ["list_model_catalog", "model_prepare", "model_stream", "file_parse"]})
        if body["operation"] == "file_parse":
            value = body["input"]
            if value["filename"] == "capability-fail.html":
                return httpx.Response(503, json={"error": "ACCEPTANCE_PARSER_PORT_UNAVAILABLE"})
            text, file_type = extract_text(value["filename"], base64.b64decode(value["content_base64"]))
            return httpx.Response(200, json={"text": text, "metadata": {"fileType": file_type}})
        if body["operation"] == "list_model_catalog":
            return httpx.Response(200, json={"data": [{"id": "fixture/model", "provider": "fixture", "model": "model", "available": True, "is_default": True}], "defaultSelection": {"mode": "model", "provider": "fixture", "model": "model"}})
        if body["operation"] == "model_prepare":
            value = body["input"]
            return httpx.Response(200, json={"requestId": value["requestId"], "selection": {"requestedModelId": "fixture/model", "selectedModelId": "model", "providerId": "fixture"}, "request": value["request"]})
        if body["operation"] == "model_stream":
            events = [{"type": "text_delta", "text": '{"discoveries": []}'}, {"type": "message_end", "finishReason": "stop"}]
            return httpx.Response(200, content="\n".join(json.dumps(event) for event in events), headers={"content-type": "application/x-ndjson", "x-pilotdeck-model-id": "model", "x-pilotdeck-provider-id": "fixture"})
        return httpx.Response(503, json={"error": "explicit_no_model_fixture"})

    patch = pytest.MonkeyPatch()
    # Generated identifiers are ordered fixtures on both sides. SQLite may use
    # a primary-key index for unordered legacy lint scans; random UUIDs make
    # the historical response itself vary between identical runs.
    from itertools import count
    identifier_sequence = count(1)
    import app.db.models as host_models
    patch.setattr(host_models, "new_id", lambda prefix: f"{prefix}_{next(identifier_sequence):032x}")
    rows = []
    directory = tempfile.TemporaryDirectory(prefix="km-contract-")
    server, engine, token = _client(patch, database_url=f"sqlite:///{directory.name}/host.sqlite")
    from replay_storage import stabilize_indexes
    stabilize_indexes(engine)
    server = TestClient(server.app, raise_server_exceptions=False)
    with Session(engine) as db:
        viewer = User(id="viewer", tenant_id="tenant_api", username="viewer", role="member", password_hash="x")
        db.add(viewer)
        db.commit()
        viewer_token = create_access_token(viewer)
    jobs.engine = engine
    if not args.km_url:
        service.engine = engine
    pending = []
    def enqueue(kind, fn, *arguments, **kwargs):
        with Session(engine) as db:
            job = db.get(__import__('app.db.models', fromlist=['KnowledgeIngestJob']).KnowledgeIngestJob, arguments[0])
            if job and job.filename == "cancel.md": return
        pending.append((fn, arguments))
    if not args.km_url:
        patch.setattr(knowledge, "enqueue_async_job", enqueue)
    if not args.km_url:
        original_extract = service.extract_ingest_text
        def parse_port(job, metadata, content, db):
            if job.filename == "capability-fail.html":
                from app.knowledge.parser import KnowledgeParseError
                raise KnowledgeParseError("ACCEPTANCE_PARSER_PORT_UNAVAILABLE")
            return original_extract(job, metadata, content, db)
        patch.setattr(service, "extract_ingest_text", parse_port)
    bind_pilotdeck_domain_host(FixedPilotDeckDomainHostClient("http://capability-fixture", "fixture", "fixture-user", httpx.MockTransport(capability), "tenant_api", "user_api_admin", "agent_api"))
    host = FastAPI()
    for router in [knowledge.router, knowledge_bases.router, module_knowledge.router]:
        host.include_router(router)
    host.dependency_overrides[get_session] = server.app.dependency_overrides[get_session]
    native = TestClient(host, raise_server_exceptions=False)
    common = {"tenantId": "tenant_api", "agentId": "agent_api", "actorUserId": "user_api_admin"}
    scope = {"tenant_id": "tenant_api", "agent_id": "agent_api"}
    trace = []
    if args.km_url:
        import app.knowledge.http_adapter as adapter
        original = adapter.httpx.request
        def tracked(method, url, **kwargs):
            trace.append({"method": method, "path": str(url).split("/api/km/v1")[-1]})
            return original(method, url, **kwargs)
        patch.setattr(adapter.httpx, "request", tracked)

    def record(case, client, method, path, **kwargs):
        request = {"method": method, "path": path}
        request.update({key: value for key, value in kwargs.items() if key in {"json", "params"}})
        response = client.request(method, path, **kwargs)
        try:
            body = response.json()
        except ValueError:
            if response.headers.get("content-type", "").startswith("application/zip"):
                import io, zipfile
                with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                    body = {name: archive.read(name).decode() for name in sorted(archive.namelist())}
            else:
                body = response.text
        rows.append({"case": case, "request": request, "response": {"status": response.status_code, "body": body}})
        print(case, response.status_code, flush=True)
        while pending:
            fn, arguments = pending.pop(0)
            fn(*arguments)
        return body

    def module(name, value=None, suffix="success", *, record_case=True):
        case = f"module.{name}.{suffix}"
        envelope = {"kind": "request", "method": "module_call", "messageId": case,
                    "runId": "contract", "operationId": case, "requestId": case, "module": "knowledge",
                    "payload": {"operation": name, "input": {**common, **(value or {})}}}
        body = record(case, native, "POST", "/v2/module/call", json=envelope) if record_case else native.post("/v2/module/call", json=envelope).json()
        return body.get("payload", {}).get("result") if isinstance(body, dict) else None

    def attempt(name, fn):
        try:
            return fn()
        except Exception as exc:
            rows.append({"case": name, "execution_error": str(exc)})
            return None

    def run_public_job(identifier):
        with patch.context() as worker_patch:
            if not args.km_url:
                worker_patch.setattr(knowledge, "enqueue_async_job", lambda kind, fn, *a, **kw: fn(*a))
            jobs.run_job(identifier)

    try:
        with server, native:
            key = _tenant_key(server, token, ["*"])
            restricted_client = server.post("/api-clients", headers={"Authorization": "Bearer " + token}, json={"name": "restricted", "scopes": ["*"]}).json()
            read_key = server.post(f"/api-clients/{restricted_client['id']}/credentials", headers={"Authorization": "Bearer " + token}, json={"name": "read", "scopes": ["knowledge:read"]}).json()["api_key"]
            for name, client, prefix in [("rest", native, "/api/enterprise/knowledge-bases"), ("public", server, "/agents/agent_api/knowledge-bases")]:
                record(f"{name}.unauthenticated", client, "GET", prefix, params=scope if name == "rest" else {})
            record("module.manifest", native, "GET", "/module-manifest")
            record("module.malformed", native, "POST", "/v2/module/call", json={"payload": {}})
            module("create_base", {"actorUserId": "missing", "name": "Unauthorized"}, "unauthenticated")
            module("create_base", {"actorUserId": "viewer", "name": "Forbidden"}, "permission_denied")
            module("get_base", {"knowledgeBaseId": "missing"}, "missing")
            module("create_base", {"name": ""}, "invalid")
            for name in module_knowledge.METHODS:
                module(name, {"knowledgeBaseId": "missing", "documentId": "missing", "bucketId": "missing",
                              "chunkId": "missing", "jobId": "missing", "suggestionId": "missing", "conceptId": "missing",
                              "name": "", "query": "", "version": "missing", "filename": "bad.zip",
                              "contentBase64": "!", "contentMd": ""}, "invalid_or_missing")

            def module_workflow():
                base = module("create_base", {"name": "Module Policy"})
                kb = base["id"]
                ref = {"knowledgeBaseId": kb}
                module("list_bases")
                module("get_base", ref)
                module("get_base", {**ref, "agentId": "agent_other"}, "cross_space")
                module("update_base", {**ref, "description": "updated"})
                job = module("import_document", {**ref, "filename": "policy.md", "contentBase64": base64.b64encode(b"# Policy\n\nExpense reimbursement requires receipts within 30 days.").decode()})
                for _ in range(200):
                    result = native.post("/v2/module/call", json={"kind": "request", "method": "module_call", "messageId": "poll", "runId": "contract", "operationId": "poll", "requestId": "poll", "module": "knowledge", "payload": {"operation": "get_job", "input": {**common, "jobId": job["id"]}}}).json().get("payload", {}).get("result", {})
                    if result.get("status") in {"failed", "succeeded", "cancelled"}: break
                    time.sleep(.05)
                module("get_job", {"jobId": job["id"]})
                module("list_jobs")
                module("cancel_job", {"jobId": job["id"]}, "terminal")
                docs = module("list_documents", ref)
                doc = docs[0]
                dref = {"documentId": doc["id"]}
                module("get_document", dref)
                module("update_document", {**dref, "title": "Policy updated", "expectedUpdatedAt": doc["updated_at"]})
                module("update_document", {**dref, "contentMd": "stale", "expectedUpdatedAt": "2000-01-01T00:00:00"}, "conflict")
                buckets = module("list_document_buckets", dref)
                bucket = buckets[0]
                module("update_bucket", {"bucketId": bucket["id"], "title": "Expenses"})
                chunks = module("list_bucket_chunks", {"bucketId": bucket["id"]})
                module("update_chunk", {"chunkId": chunks[0]["id"], "content": "Expense policy requires original receipts within 30 days."})
                module("query", {"query": "Expense receipts", "knowledgeBaseIds": [kb]})
                module("resolve_citation", {"chunkId": chunks[0]["id"]})
                concepts = module("list_okf_concepts", ref)
                if concepts:
                    cref = {**ref, "conceptId": concepts[0]["concept_id"]}
                    module("get_okf_concept", cref)
                    module("upsert_okf_concept", {**cref, "contentMd": concepts[0]["content_md"]})
                module("lint_okf", ref)
                bundle = module("export_okf", ref)
                if bundle:
                    module("import_okf", {**ref, "filename": bundle["filename"], "contentBase64": bundle["content_base64"]})
                discoveries = module("list_discoveries", ref)
                if discoveries:
                    module("confirm_discovery", {"suggestionId": discoveries[0]["id"]})
                    module("confirm_discovery", {"suggestionId": discoveries[0]["id"]}, "duplicate_publish")
                    if len(discoveries) > 1:
                        module("reject_discovery", {"suggestionId": discoveries[1]["id"]})
                    else:
                        module("upsert_okf_concept", {**ref, "conceptId": "topics/reject", "contentMd": "---\ntype: \"Topic\"\ntitle: \"Reject fixture\"\n---\n\nA separate orphan concept."}, "reject_setup")
                        module("lint_okf", ref, "reject_setup")
                        reject_rows = module("list_discoveries", ref, "reject_setup")
                        rejected = next(row for row in reject_rows if row["status"] == "pending")
                        module("reject_discovery", {"suggestionId": rejected["id"]})
                versions = module("list_versions", ref)
                module("rollback_version", {**ref, "version": versions[-1]["version"]})
                module("publish_version", ref)
                module("sync_base", ref, "gallery_unavailable")
                module("rollback_version", {**ref, "version": "missing-version"}, "stale_version")
                module("delete_document", dref, "historical_missing")
                restored_job = module("import_document", {**ref, "filename": "delete-policy.md", "contentBase64": base64.b64encode(b"# Policy\n\nArchive this document.").decode()}, "delete_setup")
                for _ in range(200):
                    reply = native.post("/v2/module/call", json={"kind": "request", "method": "module_call", "messageId": "poll", "runId": "contract", "operationId": "poll", "requestId": "poll", "module": "knowledge", "payload": {"operation": "get_job", "input": {**common, "jobId": restored_job["id"]}}}).json().get("payload", {}).get("result", {})
                    if reply.get("status") in {"failed", "succeeded", "cancelled"}: break
                    time.sleep(.05)
                current_docs = module("list_documents", ref, "delete_setup")
                module("delete_document", {"documentId": current_docs[-1]["id"]})
                module("import_okf", {**ref, "filename": "bad.zip", "contentBase64": base64.b64encode(b"invalid archive").decode()}, "parse_failure")
                cancel = module("import_document", {**ref, "filename": "cancel.md", "contentBase64": base64.b64encode(b"# Cancel\n\nHold this queued job.").decode()}, "cancel_setup")
                module("cancel_job", {"jobId": cancel["id"]})
                failed = module("import_document", {**ref, "filename": "capability-fail.html", "contentBase64": base64.b64encode(b"<p>Port failure</p>").decode()}, "capability_failure_setup")
                for _ in range(200):
                    reply = native.post("/v2/module/call", json={"kind": "request", "method": "module_call", "messageId": "poll", "runId": "contract", "operationId": "poll", "requestId": "poll", "module": "knowledge", "payload": {"operation": "get_job", "input": {**common, "jobId": failed["id"]}}}).json().get("payload", {}).get("result", {})
                    if reply.get("status") in {"failed", "succeeded", "cancelled"}: break
                    time.sleep(.05)
                module("get_job", {"jobId": failed["id"]}, "capability_failure")
                module("delete_base", ref)
            attempt("module.workflow", module_workflow)

            def rest_workflow():
                headers = {"Authorization": "Bearer " + token}
                def rest(case, method, path, **kwargs):
                    return record("rest." + case, native, method, path, headers=headers, params=scope, **kwargs)
                prefix = "/api/enterprise/knowledge-bases"
                base = rest("create", "POST", prefix, json={"tenant_id": "tenant_api", "name": "REST Policy"})
                kb = base["id"]
                record("rest.permission_denied", native, "POST", prefix, headers={"Authorization": "Bearer " + viewer_token}, params=scope, json={"tenant_id": "tenant_api", "name": "Forbidden"})
                record("rest.cross_space", native, "GET", prefix + "/" + kb, headers=headers, params={**scope, "agent_id": "agent_other"})
                rest("list", "GET", prefix)
                rest("get", "GET", prefix + "/" + kb)
                rest("update", "PUT", prefix + "/" + kb, json={"tenant_id": "tenant_api", "description": "updated"})
                rest("versions", "GET", prefix + "/" + kb + "/versions")
                rest("missing", "GET", prefix + "/missing")
                rest("invalid", "POST", prefix, json={})
                upload = rest("upload", "POST", "/api/enterprise/knowledge/documents", json={"tenant_id": "tenant_api", "knowledge_base_id": kb, "filename": "rest-policy.md", "content_base64": base64.b64encode(b"# Policy\n\nExpense receipts must be retained.").decode()})
                if upload.get("id"):
                    for _ in range(200):
                        terminal = native.get("/api/enterprise/knowledge/jobs/" + upload["id"], headers=headers, params=scope).json()
                        if terminal.get("status") in {"failed", "succeeded", "cancelled"}: break
                        time.sleep(.05)
                    rest("job_terminal", "GET", "/api/enterprise/knowledge/jobs/" + upload["id"])
                documents = record("rest.documents", native, "GET", "/api/enterprise/knowledge/documents", headers=headers, params={**scope, "knowledge_base_id": kb})
                if isinstance(documents, list) and documents:
                    document = documents[0]
                    dpath = "/api/enterprise/knowledge/documents/" + document["id"]
                    rest("get_document", "GET", dpath)
                    rest("conflict", "PUT", dpath, json={"tenant_id": "tenant_api", "content_md": "stale", "expected_updated_at": "2000-01-01T00:00:00"})
                    document = rest("update_document", "PUT", dpath, json={"tenant_id": "tenant_api", "title": "REST Policy updated", "expected_updated_at": document["updated_at"]})
                    dpath = "/api/enterprise/knowledge/documents/" + document["id"]
                    buckets = rest("buckets", "GET", dpath + "/buckets")
                    bucket = buckets[0]
                    rest("update_bucket", "PUT", "/api/enterprise/knowledge/buckets/" + bucket["id"], json={"tenant_id": "tenant_api", "title": "Receipts"})
                    chunks = rest("chunks", "GET", "/api/enterprise/knowledge/buckets/" + bucket["id"] + "/chunks")
                    rest("update_chunk", "PUT", "/api/enterprise/knowledge/chunks/" + chunks[0]["id"], json={"tenant_id": "tenant_api", "content": "Expense receipts must be retained for 30 days."})
                rest("query", "POST", "/api/enterprise/knowledge/search", json={"tenant_id": "tenant_api", "agent_id": "agent_api", "knowledge_base_ids": [kb], "query": "Expense receipts"})
                concepts = rest("concepts", "GET", prefix + "/" + kb + "/okf/concepts")
                concept_path = prefix + "/" + kb + "/okf/concepts/" + concepts[0]["concept_id"]
                rest("get_concept", "GET", concept_path)
                rest("update_concept", "PUT", concept_path, json={"tenant_id": "tenant_api", "content_md": concepts[0]["content_md"]})
                rest("lint", "POST", prefix + "/" + kb + "/okf/lint")
                rest("export", "GET", prefix + "/" + kb + "/okf/export")
                rest("parse_failure", "POST", "/api/enterprise/knowledge/okf/import", json={"tenant_id": "tenant_api", "knowledge_base_id": kb, "filename": "bad.zip", "content_base64": base64.b64encode(b"invalid archive").decode()})
                suggestions = record("rest.discoveries", native, "GET", "/api/enterprise/knowledge/discoveries", headers=headers, params={**scope, "knowledge_base_id": kb})
                if not suggestions:
                    rest("discovery_setup", "PUT", prefix + "/" + kb + "/okf/concepts/topics/rest-warning", json={"tenant_id": "tenant_api", "content_md": "---\ntype: Topic\ntitle: REST warning\n---\n\nAn orphan concept."})
                    rest("discovery_lint", "POST", prefix + "/" + kb + "/okf/lint")
                    suggestions = record("rest.discoveries_after_lint", native, "GET", "/api/enterprise/knowledge/discoveries", headers=headers, params={**scope, "knowledge_base_id": kb})
                if suggestions:
                    for case, row, operation in [("confirm_discovery", suggestions[0], "confirm"), ("duplicate_publish", suggestions[0], "confirm")]:
                        record("rest." + case, native, "POST", "/api/enterprise/knowledge/discoveries/" + row["id"] + "/" + operation, headers=headers, params=scope)
                    if len(suggestions) > 1:
                        rest("reject_discovery", "POST", "/api/enterprise/knowledge/discoveries/" + suggestions[1]["id"] + "/reject")
                bundle = native.get(prefix + "/" + kb + "/okf/export", headers=headers, params=scope)
                rest("import_okf", "POST", "/api/enterprise/knowledge/okf/import", json={"tenant_id": "tenant_api", "agent_id": "agent_api", "knowledge_base_id": kb, "filename": "rest-okf.zip", "content_base64": base64.b64encode(bundle.content).decode()})
                cancel = rest("cancel_setup", "POST", "/api/enterprise/knowledge/documents", json={"tenant_id": "tenant_api", "knowledge_base_id": kb, "filename": "cancel.md", "content_base64": base64.b64encode(b"# Cancel\n\nHeld queued task.").decode()})
                rest("cancel", "POST", "/api/enterprise/knowledge/jobs/" + cancel["id"] + "/cancel")
                rest("list_jobs", "GET", "/api/enterprise/knowledge/jobs")
                failed = rest("capability_failure_setup", "POST", "/api/enterprise/knowledge/documents", json={"tenant_id": "tenant_api", "knowledge_base_id": kb, "filename": "capability-fail.html", "content_base64": base64.b64encode(b"<p>Port failure</p>").decode()})
                for _ in range(200):
                    reply = native.get("/api/enterprise/knowledge/jobs/" + failed["id"], headers=headers, params=scope).json()
                    if reply.get("status") in {"failed", "succeeded", "cancelled"}: break
                    time.sleep(.05)
                rest("capability_failure", "GET", "/api/enterprise/knowledge/jobs/" + failed["id"])
                versions = rest("versions_after_ingest", "GET", prefix + "/" + kb + "/versions")
                rest("rollback", "POST", prefix + "/" + kb + "/rollback", json={"tenant_id": "tenant_api", "agent_id": "agent_api", "version": versions[-1]["version"]})
                rest("publish", "POST", prefix + "/" + kb + "/promote-to-overall")
                rest("stale_version", "POST", prefix + "/" + kb + "/rollback", json={"tenant_id": "tenant_api", "agent_id": "agent_api", "version": "missing-version"})
                rest("cross_tenant", "PUT", prefix + "/" + kb, json={"tenant_id": "other", "name": "forbidden"})
                rest("archive", "DELETE", prefix + "/" + kb)
            attempt("rest.workflow", rest_workflow)

            for surface in ["public", "sdk"]:
                transport_rows = []
                def transport(request):
                    path = request.url.raw_path.decode().removeprefix("/api/v1")
                    response = server.request(request.method, path, content=request.content, headers=dict(request.headers))
                    transport_rows.append({"method": request.method, "path": path,
                                           "body": json.loads(request.content) if request.content and request.headers.get("content-type", "").startswith("application/json") else None,
                                           "response": {"status": response.status_code, "body": response.json() if response.headers.get("content-type", "").startswith(("application/json", "application/problem+json")) else response.text}})
                    while pending:
                        fn, arguments = pending.pop(0)
                        fn(*arguments)
                    return httpx.Response(response.status_code, content=response.content, headers=response.headers)
                context = PublicHTTP(transport, key) if surface == "public" else StaffDeck(base_url="http://testserver/api/v1", api_key=key, transport=httpx.MockTransport(transport))
                with context as sdk:
                    kb = None
                    doc = "missing"
                    def call(name, fn):
                        before = len(transport_rows)
                        try:
                            value = fn()
                        except APIError:
                            value = None
                        except Exception as exc:
                            rows.append({"case": surface + "." + name + ".execution", "execution_error": str(exc)})
                            value = None
                        for i, row in enumerate(transport_rows[before:]):
                            rows.append({"case": surface + "." + name + (f".{i}" if i else ""), "request": {"method": row["method"], "path": row["path"], "json": row["body"]}, "response": row["response"]})
                            print(surface + "." + name, row["response"]["status"], flush=True)
                        return value
                    def knowledge_terminal(name, identifier):
                        path = "/agents/agent_api/knowledge-jobs/" + identifier
                        for _ in range(200):
                            response = server.get(path, headers={"Authorization": "Bearer " + key})
                            if response.status_code != 200 or response.json().get("status") in {"failed", "succeeded", "cancelled"}:
                                break
                            time.sleep(.05)
                        return record(surface + "." + name, server, "GET", path, headers={"Authorization": "Bearer " + key})
                    created = call("create", lambda: sdk.knowledge_bases.create("agent_api", {"name": surface + " Policy"}))
                    if not created: continue
                    kb = created.data["id"]
                    saved_key = sdk.http.headers["Authorization"] if surface == "public" else None
                    if surface == "public":
                        sdk.http.headers["Authorization"] = "Bearer " + read_key
                        call("permission_denied", lambda: sdk.knowledge_bases.create("agent_api", {"name": "Forbidden"}))
                        sdk.http.headers["Authorization"] = "Bearer invalid"
                        call("unauthenticated", lambda: sdk.knowledge_bases.list("agent_api"))
                        sdk.http.headers["Authorization"] = saved_key
                    else:
                        with StaffDeck(base_url="http://testserver/api/v1", api_key=read_key, transport=httpx.MockTransport(transport)) as restricted:
                            call("permission_denied", lambda: restricted.knowledge_bases.create("agent_api", {"name": "Forbidden"}))
                        with StaffDeck(base_url="http://testserver/api/v1", api_key="invalid", transport=httpx.MockTransport(transport)) as unauthorized:
                            call("unauthenticated", lambda: unauthorized.knowledge_bases.list("agent_api"))
                    call("missing", lambda: sdk.knowledge_bases.documents("agent_api", "missing"))
                    call("invalid", lambda: sdk.knowledge_bases.create("agent_api", {}))
                    call("cross_space", lambda: sdk.knowledge_bases.documents("agent_other", kb))
                    call("list", lambda: sdk.knowledge_bases.list("agent_api"))
                    call("update", lambda: sdk.knowledge_bases.update("agent_api", kb, {"description": "updated"}))
                    receipt = call("upsert_entries", lambda: sdk.knowledge_bases.upsert_entries("agent_api", kb, {"entries": [{"title": "Policy", "content": "# Policy\n\nKeep expense receipts."}]}, idempotency_key=surface + "-entries"))
                    call("duplicate_ingest", lambda: sdk.knowledge_bases.upsert_entries("agent_api", kb, {"entries": [{"title": "Policy", "content": "# Policy\n\nKeep expense receipts."}]}, idempotency_key=surface + "-entries"))
                    if receipt:
                        attempt(surface + ".run_job", lambda: run_public_job(receipt.data["id"]))
                        call("job_terminal", lambda: sdk.jobs.get(receipt.data["id"]))
                    documents = call("documents", lambda: sdk.knowledge_bases.documents("agent_api", kb))
                    if documents and documents.data["data"]: doc = documents.data["data"][0]["id"]
                    call("conflict", lambda: sdk.knowledge_bases.update_document("agent_api", kb, doc, {"content_md": "stale", "expected_updated_at": "2000-01-01T00:00:00"}))
                    call("update_document", lambda: sdk.knowledge_bases.update_document("agent_api", kb, doc, {"title": "Policy Updated"}))
                    call("search", lambda: sdk.knowledge_bases.search("agent_api", kb, {"query": "receipts"}))
                    file = Path(directory.name) / "leave.md"
                    file.write_text("# Leave\n\nAnnual leave is paid.")
                    uploaded = call("upload_document", lambda: sdk.knowledge_bases.upload_document("agent_api", kb, file, title="Leave"))
                    if uploaded:
                        knowledge_terminal("upload_job_terminal", uploaded.data["id"])
                    current_documents = call("documents_before_archive", lambda: sdk.knowledge_bases.documents("agent_api", kb))
                    if current_documents and current_documents.data["data"]:
                        doc = current_documents.data["data"][-1]["id"]
                    call("archive_document", lambda: sdk.knowledge_bases.archive_document("agent_api", kb, doc))
                    call("versions", lambda: sdk.knowledge_bases.versions("agent_api", kb))
                    call("concepts", lambda: sdk.knowledge_bases.concepts("agent_api", kb))
                    invalid_file = Path(directory.name) / "bad.zip"
                    invalid_file.write_bytes(b"invalid archive")
                    failed_receipt = call("parse_failure", lambda: sdk.knowledge_bases.upload_document("agent_api", kb, invalid_file))
                    if failed_receipt and isinstance(failed_receipt.data, dict) and failed_receipt.data.get("id"):
                        knowledge_terminal("parse_failure_terminal", failed_receipt.data["id"])
                    port_file = Path(directory.name) / "capability-fail.html"
                    port_file.write_text("<p>Port failure</p>")
                    port_receipt = call("capability_failure", lambda: sdk.knowledge_bases.upload_document("agent_api", kb, port_file))
                    if port_receipt and isinstance(port_receipt.data, dict) and port_receipt.data.get("id"):
                        knowledge_terminal("capability_failure_terminal", port_receipt.data["id"])
                    call("rollback", lambda: sdk.knowledge_bases.rollback("agent_api", kb, "1.0.0"))
                    call("stale_version", lambda: sdk.knowledge_bases.rollback("agent_api", kb, "missing-version"))
                    call("archive", lambda: sdk.knowledge_bases.archive("agent_api", kb))
            from app.db.models import AgentProfile
            with Session(engine) as db:
                db.add(AgentProfile(id="overall", tenant_id="tenant_api", name="Overall", is_overall=True))
                db.commit()
            gallery_base = module("create_base", {"agentId": "overall", "name": "Gallery sync"}, "sync_setup")
            module("sync_base", {"knowledgeBaseId": gallery_base["id"]})
            record("rest.sync", native, "POST", "/api/enterprise/knowledge-bases/" + gallery_base["id"] + "/sync-from-overall", headers={"Authorization": "Bearer " + token}, params=scope)
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"source_commit": subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip(), "mode": "km_http" if args.km_url else "historical_native", "http_calls": trace, "cases": rows}, indent=2, ensure_ascii=False) + "\n")
        engine.dispose()
        patch.undo()
        directory.cleanup()


if __name__ == "__main__":
    main()
