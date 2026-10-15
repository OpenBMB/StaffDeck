"""Core transport. Optional routes are registered by enabled plugins only."""
from contextlib import asynccontextmanager
from pathlib import Path
import json
from fastapi import FastAPI, Depends, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool
from . import storage as s


CORE_OPERATIONS = {
    "identity": ("GET", "/identity"), "list_models": ("GET", "/models"),
    "list_documents": ("GET", "/documents"), "import_document": ("POST", "/documents"),
    "get_document": ("GET", "/documents/{document_id}"), "update_document": ("PUT", "/documents/{document_id}"),
    "delete_document": ("DELETE", "/documents/{document_id}"), "query": ("POST", "/query"),
    "resolve_citation": ("GET", "/citations/{chunk_id}"), "get_job": ("GET", "/jobs/{job_id}"),
    "list_jobs": ("GET", "/jobs"), "cancel_job": ("POST", "/jobs/{job_id}/cancel"),
    "list_document_buckets": ("GET", "/documents/{document_id}/buckets"), "list_document_chunks": ("GET", "/documents/{document_id}/chunks"),
    "get_bucket": ("GET", "/buckets/{bucket_id}"), "get_chunk": ("GET", "/chunks/{chunk_id}"),
    "update_bucket": ("PUT", "/buckets/{bucket_id}"), "update_chunk": ("PUT", "/chunks/{chunk_id}"),
    "list_bucket_chunks": ("GET", "/buckets/{bucket_id}/chunks"),
}


def mount_ui(app, directory):
    ui = directory or Path(__file__).parents[1] / "ui"
    if directory:
        try: valid = (ui / "index.html").is_file() and json.loads((ui / "version.json").read_text())["protocol"] == "knowledge-frame/v1"
        except (OSError, ValueError, KeyError): valid = False
        if not valid: raise ValueError("Invalid Knowledge UI artifact")
    if ui.is_dir():
        app.mount("/ui/v1", StaticFiles(directory=ui, html=True), name="knowledge-ui-versioned")
        app.mount("/", StaticFiles(directory=ui, html=True), name="knowledge-ui")


def create_app(runtime, *, ui_directory=None, transport=None):
    @asynccontextmanager
    async def lifespan(app):
        runtime.initialize()
        try: yield
        finally: runtime.close()
    app = FastAPI(title="Knowledge Core", lifespan=lifespan)
    app.state.runtime = runtime
    prefix = "/api/km/v1"

    def principal(authorization: str | None = Header(default=None)):
        try: return runtime.identity.authenticate(authorization)
        except HTTPException: raise
        except Exception as error: raise HTTPException(503, "Identity provider unavailable") from error

    async def value(request):
        try: result = await request.json()
        except ValueError as error: raise HTTPException(422, "Invalid JSON") from error
        if not isinstance(result, dict): raise HTTPException(422, "Expected JSON object")
        return result

    def context(request, body, actor):
        combined = {**dict(request.query_params), **body}
        result = {"tenant_id": actor.tenant_id}
        for module, names in (("spaces", ("space_id", "source_space_id")), ("catalog", ("knowledge_base_id", "knowledge_base_ids")), ("versions", ("version_id", "version_ids"))):
            for name in names:
                if name in combined:
                    if not runtime.configuration.enabled(module): raise HTTPException(422, f"{name} requires enabled {module} module")
                    if name.endswith("_ids"):
                        if not isinstance(combined[name], list) or not all(isinstance(item, str) and item.strip() for item in combined[name]):
                            raise HTTPException(422, name + " must be an array of IDs")
                    elif not isinstance(combined[name], str) or not combined[name].strip():
                        raise HTTPException(422, name + " must be an ID")
                    result[name] = combined[name]
        if any(name in combined for name in ("actor", "actor_id", "tenant_id", "agent_id")):
            raise HTTPException(422, "Identity comes from the trusted entrypoint")
        return result

    def validate(body, allowed, required=()):
        common = {"space_id", "knowledge_base_id", "version_id"}
        if set(body) - allowed - common: raise HTTPException(422, "Unknown fields: " + ", ".join(sorted(set(body) - allowed - common)))
        for name in required:
            if name not in body or not isinstance(body[name], str) or not body[name].strip(): raise HTTPException(422, name + " is required")
        if "metadata" in body and not isinstance(body["metadata"], dict): raise HTTPException(422, "metadata must be an object")
        if "model_id" in body and (not isinstance(body["model_id"], str) or not body["model_id"].strip()): raise HTTPException(422, "Invalid model_id")
        if "title" in body and not isinstance(body["title"], str): raise HTTPException(422, "title must be a string")

    @app.exception_handler(IntegrityError)
    async def conflict(request, error):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "Resource conflict"}, status_code=409)

    @app.get("/api/km/config")
    def configuration():
        operations = {name: {"method": method, "path": prefix + path} for name, (method, path) in CORE_OPERATIONS.items()}
        for plugin in runtime.plugins.values():
            operations.update({name: {"method": method, "path": prefix + path} for name, (method, path) in plugin.operations.items()})
        return {**runtime.configuration.as_dict(), "api_protocol": "knowledge-core/v1", "operations": operations,
                "identity_required": runtime.configuration.enabled("authentication"), "model_mode": "model" if runtime.model_config or runtime.capabilities else "lexical"}

    @app.get(prefix + "/identity")
    def identity(actor=Depends(principal)):
        return {"id": actor.id, "permissions": sorted(actor.permissions)}

    @app.get(prefix + "/models")
    def models(request: Request, actor=Depends(principal)):
        scope = context(request, {}, actor); runtime.authorize(actor, scope, "read")
        if runtime.capabilities:
            provider = runtime.capabilities.scoped(actor.tenant_id, scope.get("space_id", ""), None)
            if provider:
                value = provider.call("catalog", {})
                return value.get("models", value.get("data", [])) if isinstance(value, dict) else value
            return []
        return [{"id": runtime.model_config.model, "name": runtime.model_config.model, "default": True}] if runtime.model_config else []

    @app.get("/health/live")
    def live(): return {"status": "live"}

    @app.get("/health/ready")
    def ready():
        if not runtime.ready(): raise HTTPException(503, "Database unavailable")
        return {"status": "ready"}

    @app.get(prefix + "/documents")
    def documents(request: Request, actor=Depends(principal)):
        return jsonable_encoder(runtime.content.list(s.documents, context(request, {}, actor), actor))

    @app.post(prefix + "/documents", status_code=202)
    async def upload(request: Request, actor=Depends(principal)):
        body = await value(request)
        scope = context(request, body, actor)
        if body.get("model_id") and not runtime.capabilities and (not runtime.model_config or body["model_id"] != runtime.model_config.model):
            raise HTTPException(422, "Unknown model_id")
        validate(body, {"filename", "content_base64", "title", "metadata", "model_id"}, ("filename", "content_base64"))
        try: row = runtime.tasks.create(body, scope, actor)
        except ValueError as error: raise HTTPException(422, str(error)) from error
        return job_view(row)

    @app.post(prefix + "/query")
    async def query(request: Request, actor=Depends(principal)):
        body = await value(request)
        scope = context(request, body, actor)
        if body.get("model_id") and not runtime.capabilities and (not runtime.model_config or body["model_id"] != runtime.model_config.model):
            raise HTTPException(422, "Unknown model_id")
        validate(body, {"query", "document_ids", "max_chunks", "use_model", "model_id", "knowledge_base_ids", "version_ids"}, ("query",))
        if "max_chunks" in body and (type(body["max_chunks"]) is not int or not 1 <= body["max_chunks"] <= 100): raise HTTPException(422, "Invalid max_chunks")
        if "use_model" in body and type(body["use_model"]) is not bool: raise HTTPException(422, "use_model must be boolean")
        if "document_ids" in body and (not isinstance(body["document_ids"], list) or not all(isinstance(item, str) and item.strip() for item in body["document_ids"])):
            raise HTTPException(422, "document_ids must be an array of IDs")
        return jsonable_encoder(await run_in_threadpool(runtime.retrieval.query, body, scope, actor))

    @app.get(prefix + "/documents/{document_id}/buckets")
    def buckets(document_id: str, request: Request, actor=Depends(principal)):
        return jsonable_encoder(runtime.content.list(s.buckets, context(request, {}, actor), actor, document_id=document_id))

    @app.get(prefix + "/documents/{document_id}/chunks")
    def chunks(document_id: str, request: Request, actor=Depends(principal)):
        return jsonable_encoder(runtime.content.list(s.chunks, context(request, {}, actor), actor, document_id=document_id))

    @app.get(prefix + "/buckets/{bucket_id}/chunks")
    def bucket_chunks(bucket_id: str, request: Request, actor=Depends(principal)):
        return jsonable_encoder(runtime.content.list(s.chunks, context(request, {}, actor), actor, bucket_id=bucket_id))

    def register_entity(name, table):
        def get_entity(resource_id: str, request: Request, actor=Depends(principal)):
            return jsonable_encoder(runtime.content.get(table, resource_id, context(request, {}, actor), actor))
        async def edit_entity(resource_id: str, request: Request, actor=Depends(principal)):
            body = await value(request)
            scope = context(request, body, actor)
            fields = {"content_md", "metadata", "expected_updated_at"}
            if "title" in table.c: fields.add("title")
            if "summary" in table.c: fields.add("summary")
            if table is s.documents: fields.add("status")
            validate(body, fields, ("expected_updated_at",))
            for field in ("content_md", "title", "summary", "status"):
                if field in body and not isinstance(body[field], str): raise HTTPException(422, field + " must be a string")
            if "status" in body and body["status"] not in {"ready", "archived"}: raise HTTPException(422, "Invalid document status")
            if "metadata" in body and not isinstance(body["metadata"], dict): raise HTTPException(422, "metadata must be an object")
            for field in ("space_id", "knowledge_base_id", "version_id"): body.pop(field, None)
            return jsonable_encoder(await run_in_threadpool(runtime.content.edit, table, resource_id, body, scope, actor))
        app.add_api_route(prefix + "/" + name + "/{resource_id}", get_entity, methods=["GET"])
        app.add_api_route(prefix + "/" + name + "/{resource_id}", edit_entity, methods=["PUT"])
    for name, table in (("documents", s.documents), ("buckets", s.buckets), ("chunks", s.chunks)): register_entity(name, table)

    @app.delete(prefix + "/documents/{document_id}")
    def archive(document_id: str, request: Request, actor=Depends(principal)):
        return runtime.content.archive(document_id, context(request, {}, actor), actor)

    @app.get(prefix + "/citations/{chunk_id}")
    def citation(chunk_id: str, request: Request, actor=Depends(principal)):
        scope = context(request, {}, actor)
        runtime.authorize(actor, scope, "read")
        return runtime.evidence.resolve(chunk_id, scope, actor)

    def job_view(row):
        result = {key: content for key, content in row.items() if key not in {"payload", "principal", "context"}}
        result["progress"] = float(result["progress"])
        return jsonable_encoder(result)

    def get_job(job_id, request, actor, action="read"):
        scope = context(request, {}, actor)
        runtime.authorize(actor, scope, action)
        with runtime.engine.connect() as db: row = db.execute(select(s.jobs).where(s.jobs.c.id == job_id)).mappings().first()
        if not row or any(row["context"].get(name) != scope.get(name) for module, name in (("spaces", "space_id"), ("catalog", "knowledge_base_id"), ("versions", "version_id")) if runtime.configuration.enabled(module) and (module == "spaces" or name in scope)):
            raise HTTPException(404, "Task not found")
        return dict(row)

    @app.get(prefix + "/jobs/{job_id}")
    def job(job_id: str, request: Request, actor=Depends(principal)): return job_view(get_job(job_id, request, actor))

    @app.get(prefix + "/jobs")
    def jobs(request: Request, actor=Depends(principal)):
        scope = context(request, {}, actor); runtime.authorize(actor, scope, "read")
        with runtime.engine.connect() as db: rows = list(db.execute(select(s.jobs).order_by(s.jobs.c.created_at.desc())).mappings())
        return [job_view(row) for row in rows if all(row["context"].get(name) == scope.get(name) for module, name in (("spaces", "space_id"), ("catalog", "knowledge_base_id"), ("versions", "version_id")) if runtime.configuration.enabled(module) and (module == "spaces" or name in scope))]

    @app.post(prefix + "/jobs/{job_id}/cancel")
    def cancel(job_id: str, request: Request, actor=Depends(principal)):
        row = get_job(job_id, request, actor, "write")
        if row["status"] not in {"succeeded", "failed", "cancelled"}:
            runtime.tasks.cancel(job_id)
        return job_view(get_job(job_id, request, actor))

    if transport:
        transport(app, runtime, principal)
    else:
        for plugin in runtime.plugins.values(): plugin.register(app, {"principal": principal, "context": context, "value": value, "validate": validate})
    @app.api_route("/api/{unregistered:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    def unavailable(unregistered: str):
        raise HTTPException(404, "Operation unavailable in this configuration")
    mount_ui(app, ui_directory)
    return app
