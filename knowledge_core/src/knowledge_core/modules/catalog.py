from fastapi import Depends, HTTPException, Request
from sqlalchemy import Column, DateTime, JSON, String, Table, select, update, delete
from .base import Plugin
from ..kernel.storage import identifier, now


class Catalog(Plugin):
    schema_version = 2
    operations = {"list_bases": ("GET", "/knowledge-bases"), "create_base": ("POST", "/knowledge-bases"),
        "get_base": ("GET", "/knowledge-bases/{knowledge_base_id}"), "update_base": ("PUT", "/knowledge-bases/{knowledge_base_id}"),
        "delete_base": ("DELETE", "/knowledge-bases/{knowledge_base_id}")}

    def __init__(self, runtime):
        super().__init__(runtime)
        self.catalogs = Table("km_catalogs", self.metadata, Column("id", String, primary_key=True),
                              Column("name", String, nullable=False), Column("description", String), Column("status", String, nullable=False),
                              Column("metadata", JSON, nullable=False, default=dict), Column("tenant_id", String, nullable=False, default='local'),
                              Column("capability_scope", String, nullable=False, default='general'),
                              Column("created_at", DateTime, nullable=False), Column("updated_at", DateTime, nullable=False))
        self.contents = Table("km_catalog_contents", self.metadata, Column("knowledge_base_id", String, primary_key=True),
                              Column("document_id", String, primary_key=True))
        if runtime.configuration.enabled("spaces"):
            self.bindings = runtime.plugins["spaces"].catalog_bindings

    def validate(self, context):
        ids = context.get("knowledge_base_ids", [])
        if not isinstance(ids, list): raise HTTPException(422, "knowledge_base_ids must be an array")
        if context.get("knowledge_base_id"): ids = [*ids, context["knowledge_base_id"]]
        with self.runtime.engine.connect() as db:
            for resource_id in ids:
                if not isinstance(resource_id, str): raise HTTPException(422, "Invalid knowledge base ID")
                if not db.execute(select(self.catalogs).where(self.catalogs.c.id == resource_id)).first():
                    raise HTTPException(404, "Knowledge base not found")
                if hasattr(self, "bindings") and not db.execute(select(self.bindings).where(
                    self.bindings.c.knowledge_base_id == resource_id, self.bindings.c.space_id == context.get("space_id"),
                    self.bindings.c.hidden == False)).first():
                    raise HTTPException(404, "Knowledge base not found")

    def visible(self, document_id, context):
        ids = context.get("knowledge_base_ids", [])
        if context.get("knowledge_base_id"): ids = [context["knowledge_base_id"]]
        with self.runtime.engine.connect() as db:
            statement = select(self.contents).where(self.contents.c.document_id == document_id)
            if ids: statement = statement.where(self.contents.c.knowledge_base_id.in_(ids))
            if hasattr(self, "bindings"):
                statement = statement.join(self.bindings, self.contents.c.knowledge_base_id == self.bindings.c.knowledge_base_id).where(
                    self.bindings.c.space_id == context.get("space_id"), self.bindings.c.hidden == False)
                return db.execute(statement).first() is not None or db.execute(select(self.contents).where(self.contents.c.document_id == document_id)).first() is None
            return not ids or db.execute(statement).first() is not None

    def assign(self, db, document_id, context):
        resource_id = context.get("knowledge_base_id")
        if resource_id and not db.execute(select(self.contents).where(self.contents.c.document_id == document_id,
                self.contents.c.knowledge_base_id == resource_id)).first():
            db.execute(self.contents.insert().values(knowledge_base_id=resource_id, document_id=document_id))

    def register(self, app, dependencies):
        principal, context, value, validate = (dependencies[key] for key in ("principal", "context", "value", "validate"))

        @app.get("/api/km/v1/knowledge-bases")
        def list_catalog(request: Request, actor=Depends(principal)):
            scope = context(request, {}, actor); self.runtime.authorize(actor, scope, "read")
            statement = select(self.catalogs)
            if hasattr(self, "bindings"):
                statement = statement.join(self.bindings, self.catalogs.c.id == self.bindings.c.knowledge_base_id).where(self.bindings.c.space_id == scope["space_id"], self.bindings.c.hidden == False)
            with self.runtime.engine.connect() as db: return [dict(row) for row in db.execute(statement).mappings()]

        @app.post("/api/km/v1/knowledge-bases", status_code=201)
        async def create_catalog(request: Request, actor=Depends(principal)):
            body = await value(request); scope = context(request, body, actor)
            self.runtime.authorize(actor, scope, "write")
            validate(body, {"id", "name", "description"}, ("name",))
            row = {"id": body.get("id") or identifier("kbase"), "name": body["name"], "description": body.get("description", ""),
                   "status": "active", "created_at": now(), "updated_at": now()}
            with self.runtime.engine.begin() as db:
                db.execute(self.catalogs.insert().values(**row))
                if hasattr(self, "bindings"): db.execute(self.bindings.insert().values(knowledge_base_id=row["id"], space_id=scope["space_id"]))
                versions = self.runtime.plugins.get("versions")
                if versions: versions.branch(db, {**scope, "knowledge_base_id": row["id"]}, create=True)
            return row

        @app.get("/api/km/v1/knowledge-bases/{knowledge_base_id}")
        def get_catalog(knowledge_base_id: str, request: Request, actor=Depends(principal)):
            scope = context(request, {"knowledge_base_id": knowledge_base_id}, actor); self.runtime.authorize(actor, scope, "read")
            with self.runtime.engine.connect() as db: return dict(db.execute(select(self.catalogs).where(self.catalogs.c.id == knowledge_base_id)).mappings().one())

        @app.put("/api/km/v1/knowledge-bases/{knowledge_base_id}")
        async def edit_catalog(knowledge_base_id: str, request: Request, actor=Depends(principal)):
            body = await value(request); scope = context(request, {**body, "knowledge_base_id": knowledge_base_id}, actor)
            self.runtime.authorize(actor, scope, "write"); validate(body, {"name", "description", "status", "expected_updated_at"}, ("expected_updated_at",))
            with self.runtime.engine.begin() as db:
                row = db.execute(select(self.catalogs).where(self.catalogs.c.id == knowledge_base_id)).mappings().one()
                if row["updated_at"].isoformat() != body["expected_updated_at"]: raise HTTPException(409, "Concurrent catalog edit")
                changes = {key: body[key] for key in ("name", "description", "status") if key in body}; changes["updated_at"] = now()
                db.execute(update(self.catalogs).where(self.catalogs.c.id == knowledge_base_id).values(**changes))
                return {**dict(row), **changes}

        @app.delete("/api/km/v1/knowledge-bases/{knowledge_base_id}")
        def archive_catalog(knowledge_base_id: str, request: Request, actor=Depends(principal)):
            scope = context(request, {"knowledge_base_id": knowledge_base_id}, actor); self.runtime.authorize(actor, scope, "write")
            with self.runtime.engine.begin() as db:
                db.execute(update(self.catalogs).where(self.catalogs.c.id == knowledge_base_id).values(status="archived", updated_at=now()))
            return {"status": "archived"}


create = Catalog
