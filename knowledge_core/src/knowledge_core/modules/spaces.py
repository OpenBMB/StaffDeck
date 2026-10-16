from fastapi import Depends, HTTPException, Request
from sqlalchemy import Boolean, Column, DateTime, JSON, String, Table, select, update
from ..kernel import storage as s
from .base import Plugin


class Spaces(Plugin):
    schema_version = 3
    operations = {"list_spaces": ("GET", "/spaces"), "create_space": ("POST", "/spaces")}

    def __init__(self, runtime):
        super().__init__(runtime)
        self.spaces = Table("km_spaces", self.metadata, Column("id", String, primary_key=True),
                            Column("name", String, nullable=False), Column("tenant_id", String, nullable=False),
                            Column('owner_id', String, default='local'), Column('owner_name', String, default='Local'),
                            Column('status', String, default='active'), Column('created_at', DateTime, default=s.now),
                            Column('updated_at', DateTime, default=s.now))
        self.contents = Table("km_space_contents", self.metadata, Column("space_id", String, primary_key=True),
                              Column("document_id", String, primary_key=True))
        if runtime.configuration.enabled("catalog"):
            self.catalog_bindings = Table("km_catalog_space_bindings", self.metadata,
                Column("knowledge_base_id", String, primary_key=True), Column("space_id", String, primary_key=True),
                Column("hidden", Boolean, nullable=False, default=False, server_default="0"),
                Column('id', String, nullable=False, default=lambda: s.identifier('binding')),
                Column('metadata', JSON, nullable=False, default=dict), Column('created_at', DateTime, default=s.now),
                Column('updated_at', DateTime, default=s.now))
            self.operations = {**self.operations,
                "list_bindings": ("GET", "/bindings"),
                "bind_base": ("POST", "/knowledge-bases/{knowledge_base_id}/bindings"),
                "update_binding": ("PUT", "/knowledge-bases/{knowledge_base_id}/binding")}
        if runtime.configuration.enabled("versions"):
            self.version_access = Table("km_version_spaces", self.metadata,
                Column("version_id", String, primary_key=True), Column("space_id", String, primary_key=True))
            self.branch_scopes = Table("km_branch_spaces", self.metadata,
                Column("branch_id", String, primary_key=True), Column("space_id", String, primary_key=True))

    def authorize(self, actor, context, action):
        space_id = context.get("space_id")
        if not space_id: raise HTTPException(422, "space_id is required")
        self.runtime.authorization.authorize(actor, space_id, action)
        with self.runtime.engine.connect() as db:
            row = db.execute(select(self.spaces).where(self.spaces.c.id == space_id)).mappings().first()
        if row is None or row["tenant_id"] != actor.tenant_id: raise HTTPException(404, "Space not found")

    def visible(self, document_id, context):
        with self.runtime.engine.connect() as db:
            return db.execute(select(self.contents).where(self.contents.c.document_id == document_id,
                                                          self.contents.c.space_id == context.get("space_id"))).first() is not None

    def assign(self, db, document_id, context):
        statement = select(self.contents).where(self.contents.c.document_id == document_id, self.contents.c.space_id == context["space_id"])
        if db.execute(statement).first() is None:
            db.execute(self.contents.insert().values(space_id=context["space_id"], document_id=document_id))

    def register(self, app, dependencies):
        principal, value, validate = (dependencies[key] for key in ("principal", "value", "validate"))

        @app.get("/api/km/v1/spaces")
        def list_spaces(actor=Depends(principal)):
            self.runtime.authorize(actor, {}, "read", require_scope=False)
            with self.runtime.engine.connect() as db:
                rows = db.execute(select(self.spaces).where(self.spaces.c.tenant_id == actor.tenant_id)).mappings().all()
            return [dict(row) for row in rows if "*" in actor.spaces or row["id"] in actor.spaces]

        @app.post("/api/km/v1/spaces", status_code=201)
        async def create_space(request: Request, actor=Depends(principal)):
            self.runtime.authorize(actor, {}, "write", require_scope=False)
            body = await value(request); validate(body, {"id", "name"}, ("id", "name"))
            self.runtime.authorization.authorize(actor, body["id"], "write")
            row = {"id": body["id"], "name": body["name"], "tenant_id": actor.tenant_id}
            with self.runtime.engine.begin() as db: db.execute(self.spaces.insert().values(**row))
            return row

        if not hasattr(self, "catalog_bindings"): return
        context = dependencies["context"]

        @app.get("/api/km/v1/bindings")
        def bindings(request: Request, actor=Depends(principal)):
            scope = context(request, {}, actor); self.runtime.authorize(actor, scope, "read")
            with self.runtime.engine.connect() as db:
                return [dict(row) for row in db.execute(select(self.catalog_bindings).where(
                    self.catalog_bindings.c.space_id == scope["space_id"])).mappings()]

        @app.post("/api/km/v1/knowledge-bases/{knowledge_base_id}/bindings", status_code=201)
        async def bind(knowledge_base_id: str, request: Request, actor=Depends(principal)):
            body = await value(request); validate(body, {"space_id", "hidden"}, ("space_id",))
            source = request.query_params.get("source_space_id")
            if not source: raise HTTPException(422, "source_space_id is required")
            source_scope = {"space_id": source, "knowledge_base_id": knowledge_base_id}
            self.runtime.authorize(actor, source_scope, "write")
            target_scope = {"space_id": body["space_id"], "knowledge_base_id": knowledge_base_id}
            self.authorize(actor, target_scope, "write")
            if "hidden" in body and type(body["hidden"]) is not bool: raise HTTPException(422, "hidden must be boolean")
            catalog = self.runtime.plugins["catalog"]
            versions = self.runtime.plugins.get("versions")
            with self.runtime.engine.begin() as db:
                if db.execute(select(self.catalog_bindings).where(self.catalog_bindings.c.knowledge_base_id == knowledge_base_id,
                    self.catalog_bindings.c.space_id == body["space_id"])).first(): raise HTTPException(409, "Binding already exists")
                db.execute(self.catalog_bindings.insert().values(knowledge_base_id=knowledge_base_id, space_id=body["space_id"], hidden=body.get("hidden", False)))
                if versions:
                    branch = versions.branch(db, source_scope)
                    snapshot = {"id": branch["head_version_id"], "snapshot": versions.snapshot(db, branch)}
                    target = versions.branch(db, target_scope, create=True)
                    versions.restore(db, target, snapshot, target_scope)
                    versions.checkpoint(db, target_scope, "Copied binding")
                else:
                    ids = db.execute(select(catalog.contents.c.document_id).join(self.contents,
                        catalog.contents.c.document_id == self.contents.c.document_id).where(
                        catalog.contents.c.knowledge_base_id == knowledge_base_id, self.contents.c.space_id == source)).scalars().all()
                    for document_id in ids: self.assign(db, document_id, target_scope)
            return {"knowledge_base_id": knowledge_base_id, "space_id": body["space_id"], "hidden": body.get("hidden", False)}

        @app.put("/api/km/v1/knowledge-bases/{knowledge_base_id}/binding")
        async def edit_binding(knowledge_base_id: str, request: Request, actor=Depends(principal)):
            body = await value(request); validate(body, {"hidden"})
            if type(body.get("hidden")) is not bool: raise HTTPException(422, "hidden must be boolean")
            scope = context(request, {}, actor); self.authorize(actor, scope, "write")
            with self.runtime.engine.begin() as db:
                changed = db.execute(update(self.catalog_bindings).where(
                    self.catalog_bindings.c.knowledge_base_id == knowledge_base_id,
                    self.catalog_bindings.c.space_id == scope["space_id"]).values(hidden=body["hidden"]))
                if changed.rowcount != 1: raise HTTPException(404, "Binding not found")
            return {"knowledge_base_id": knowledge_base_id, "space_id": scope["space_id"], "hidden": body["hidden"]}


create = Spaces
