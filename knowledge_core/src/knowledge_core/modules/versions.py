"""Catalog-level branches and immutable content snapshots; spaces are optional."""
from fastapi import Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy import Column, DateTime, JSON, String, Table, select, update, delete
from .base import Plugin
from ..kernel import storage as s
from ..references import remap


class Versions(Plugin):
    schema_version = 2
    operations = {"list_versions": ("GET", "/knowledge-bases/{knowledge_base_id}/versions"),
                  "get_branch": ("GET", "/knowledge-bases/{knowledge_base_id}/branch"),
                  **{name: ("POST", "/knowledge-bases/{knowledge_base_id}/versions/" + action)
                     for name, action in (("copy_version", "copy"), ("sync_base", "sync"), ("publish_version", "publish"), ("rollback_version", "rollback"))}}

    def __init__(self, runtime):
        super().__init__(runtime)
        self.versions = Table("km_versions", self.metadata, Column("id", String, primary_key=True),
            Column("knowledge_base_id", String, nullable=False), Column("name", String), Column("parent_id", String),
            Column("version", String, nullable=False, default='1.0.0'), Column("description", String),
            Column("status", String, nullable=False, default='active'), Column("capability_scope", String, nullable=False, default='general'),
            Column("metadata", JSON, nullable=False, default=dict), Column("updated_at", DateTime, default=s.now),
            Column("snapshot", JSON, nullable=False), Column("created_at", DateTime, nullable=False))
        self.branches = Table("km_branches", self.metadata, Column("id", String, primary_key=True),
            Column("knowledge_base_id", String, nullable=False), Column("head_version_id", String),
            Column("published_version_id", String), Column("updated_at", DateTime, nullable=False))
        self.branches.append_column(Column('base_version_id', String))
        self.branches.append_column(Column('status', String, nullable=False, default='active'))
        self.branches.append_column(Column('sync_state', String, nullable=False, default='synced'))
        self.branches.append_column(Column('created_at', DateTime, nullable=False, default=s.now))
        self.contents = Table("km_branch_contents", self.metadata, Column("branch_id", String, primary_key=True),
                              Column("document_id", String, primary_key=True))
        if runtime.configuration.enabled("spaces"):
            self.scopes = runtime.plugins["spaces"].branch_scopes

    def branch(self, db, context, create=False):
        base = context.get("knowledge_base_id")
        if not base: raise HTTPException(422, "knowledge_base_id is required for branch operations")
        statement = select(self.branches).where(self.branches.c.knowledge_base_id == base)
        if hasattr(self, "scopes"):
            statement = statement.join(self.scopes, self.branches.c.id == self.scopes.c.branch_id).where(self.scopes.c.space_id == context["space_id"])
        row = db.execute(statement).mappings().first()
        if row: return dict(row)
        if not create: raise HTTPException(404, "Branch not found")
        row = {"id": s.identifier("kbranch"), "knowledge_base_id": base, "head_version_id": None,
               "published_version_id": None, "updated_at": s.now()}
        db.execute(self.branches.insert().values(**row))
        if hasattr(self, "scopes"): db.execute(self.scopes.insert().values(branch_id=row["id"], space_id=context["space_id"]))
        return row

    def assign(self, db, document_id, context):
        if not context.get("knowledge_base_id"): return
        branch = self.branch(db, context, create=True)
        if not db.execute(select(self.contents).where(self.contents.c.branch_id == branch["id"], self.contents.c.document_id == document_id)).first():
            db.execute(self.contents.insert().values(branch_id=branch["id"], document_id=document_id))

    def content_changed(self, db, document_id):
        branches = select(self.contents.c.branch_id).where(self.contents.c.document_id == document_id)
        db.execute(update(self.branches).where(self.branches.c.id.in_(branches)).values(updated_at=s.now()))

    def validate(self, context, action):
        ids = context.get("version_ids", [])
        if context.get("version_id"): ids = [context["version_id"]]
        if not isinstance(ids, list) or not all(isinstance(value, str) for value in ids): raise HTTPException(422, "Invalid version IDs")
        if ids and action == "write": raise HTTPException(409, "Snapshots are immutable; restore to a branch before editing")
        with self.runtime.engine.connect() as db:
            for version_id in ids:
                row = db.execute(select(self.versions).where(self.versions.c.id == version_id)).mappings().first()
                if not row: raise HTTPException(404, "Version not found")
                self.runtime.plugins["catalog"].validate({**context, "knowledge_base_id": row["knowledge_base_id"]})
                spaces = self.runtime.plugins.get("spaces")
                if spaces and not db.execute(select(spaces.version_access).where(
                    spaces.version_access.c.version_id == version_id,
                    spaces.version_access.c.space_id == context["space_id"])).first():
                    raise HTTPException(404, "Version not visible in this space")

    def historical(self, table, context):
        ids = context.get("version_ids", []) or ([context["version_id"]] if context.get("version_id") else [])
        if not ids: return None
        result = {}
        with self.runtime.engine.connect() as db:
            rows = db.execute(select(self.versions).where(self.versions.c.id.in_(ids))).mappings().all()
        for version in rows:
            for document in version["snapshot"].values():
                items = [document] if table is s.documents else document["buckets"] if table is s.buckets else document["chunks"]
                for item in items:
                    row = {key: value for key, value in item.items() if key in table.c}
                    from datetime import datetime
                    for key in ("created_at", "updated_at"): row[key] = datetime.fromisoformat(row[key])
                    result[row["id"]] = row
        return list(result.values())

    def visible(self, document_id, context):
        if context.get("version_id") or context.get("version_ids"):
            ids = context.get("version_ids", []) or [context["version_id"]]
            with self.runtime.engine.connect() as db:
                rows = db.execute(select(self.versions).where(self.versions.c.id.in_(ids))).mappings()
                return any(document_id in row["snapshot"] for row in rows)
        if not context.get("knowledge_base_id"):
            with self.runtime.engine.connect() as db:
                statement = select(self.contents).where(self.contents.c.document_id == document_id)
                if hasattr(self, "scopes"):
                    statement = statement.join(self.scopes, self.contents.c.branch_id == self.scopes.c.branch_id).where(self.scopes.c.space_id == context.get("space_id"))
                member = db.execute(statement).first()
                assigned = db.execute(select(self.runtime.plugins["catalog"].contents).where(
                    self.runtime.plugins["catalog"].contents.c.document_id == document_id)).first()
            return member is not None or assigned is None
        with self.runtime.engine.connect() as db:
            try: branch = self.branch(db, context)
            except HTTPException: return False
            return db.execute(select(self.contents).where(self.contents.c.branch_id == branch["id"], self.contents.c.document_id == document_id)).first() is not None

    def snapshot(self, db, branch):
        ids = db.execute(select(self.contents.c.document_id).where(self.contents.c.branch_id == branch["id"])).scalars().all()
        result = {}
        for row in db.execute(select(s.documents).where(s.documents.c.id.in_(ids))).mappings():
            item = dict(row)
            item["buckets"] = [dict(x) for x in db.execute(select(s.buckets).where(s.buckets.c.document_id == row["id"])).mappings()]
            item["chunks"] = [dict(x) for x in db.execute(select(s.chunks).where(s.chunks.c.document_id == row["id"])).mappings()]
            item["modules"] = {name: plugin.snapshot(db, row["id"]) for name, plugin in self.runtime.plugins.items() if name != "versions"}
            result[row["id"]] = jsonable_encoder(item)
        return result

    def checkpoint(self, db, context, name):
        branch = self.branch(db, context, create=True)
        row = {"id": s.identifier("kversion"), "knowledge_base_id": context["knowledge_base_id"], "name": name,
               "parent_id": branch["head_version_id"], "snapshot": self.snapshot(db, branch), "created_at": s.now()}
        db.execute(self.versions.insert().values(**row))
        spaces = self.runtime.plugins.get("spaces")
        if spaces: db.execute(spaces.version_access.insert().values(version_id=row["id"], space_id=context["space_id"]))
        db.execute(update(self.branches).where(self.branches.c.id == branch["id"]).values(head_version_id=row["id"], updated_at=s.now()))
        return row

    def restore(self, db, branch, target, scope, preserve_metadata=False):
        db.execute(delete(self.contents).where(self.contents.c.branch_id == branch["id"]))
        mapping = {}
        for source_id, snapshot in target["snapshot"].items():
            mapping[source_id] = s.identifier("kdoc")
            for kind, prefix in (("buckets", "kbucket"), ("chunks", "kchunk")):
                for source in snapshot[kind]: mapping[source["id"]] = s.identifier(prefix)
        restored = []
        for source_id, snapshot in target["snapshot"].items():
            document_id = mapping[source_id]
            row = remap({key: value for key, value in snapshot.items() if key in s.documents.c}, mapping)
            source_metadata = snapshot["metadata"]
            if preserve_metadata: row['metadata'] = dict(source_metadata)
            row.update(id=document_id, created_at=s.now(), updated_at=s.now(),
                metadata={**row["metadata"], "revision_source_id": source_metadata.get("revision_source_id", source_id),
                    "revision_source_updated_at": source_metadata.get("revision_source_updated_at", snapshot["updated_at"])})
            if preserve_metadata:
                from datetime import datetime
                row['created_at'] = datetime.fromisoformat(snapshot['created_at'])
            db.execute(s.documents.insert().values(**row))
            buckets = {}
            for source in snapshot["buckets"]:
                item = remap(dict(source), mapping); item.update(id=mapping[source["id"]], document_id=document_id, created_at=s.now(), updated_at=s.now())
                if preserve_metadata:
                    item['metadata'] = dict(source['metadata']); item['created_at'] = datetime.fromisoformat(source['created_at'])
                buckets[source["id"]] = item["id"]; db.execute(s.buckets.insert().values(**item))
                mapping[source["id"]] = item["id"]
            for source in snapshot["chunks"]:
                item = remap(dict(source), mapping); item.update(id=mapping[source["id"]], document_id=document_id,
                    bucket_id=buckets[source["bucket_id"]], created_at=s.now(), updated_at=s.now())
                if preserve_metadata:
                    item['metadata'] = dict(source['metadata']); item['created_at'] = datetime.fromisoformat(source['created_at'])
                db.execute(s.chunks.insert().values(**item))
                mapping[source["id"]] = item["id"]
                db.execute(s.citations.insert().values(id=item["id"], document_id=document_id,
                    payload={**jsonable_encoder(item), "recovery_contexts": [dict(scope)]}))
            self.runtime.assign_content(db, document_id, scope)
            restored.append((document_id, snapshot))
        document_mapping = {source_id: mapping[source_id] for source_id in target["snapshot"]}
        for document_id, snapshot in restored:
            for name, value in snapshot.get("modules", {}).items():
                plugin = self.runtime.plugins.get(name)
                if plugin and name != "versions" and value is not None:
                    plugin.restore_content(db, value, document_id, mapping, preserve_metadata=preserve_metadata) if name in {'concepts', 'discovery'} else plugin.restore_content(db, value, document_id, mapping)
        db.execute(update(self.branches).where(self.branches.c.id == branch["id"]).values(head_version_id=target["id"], updated_at=s.now()))
        return {"version_id": target["id"], "document_mapping": document_mapping}

    def register(self, app, dependencies):
        principal, context, value, validate = (dependencies[key] for key in ("principal", "context", "value", "validate"))

        @app.get("/api/km/v1/knowledge-bases/{knowledge_base_id}/branch")
        def get_branch(knowledge_base_id: str, request: Request, actor=Depends(principal)):
            scope = context(request, {"knowledge_base_id": knowledge_base_id}, actor)
            self.runtime.authorize(actor, scope, "read")
            with self.runtime.engine.connect() as db: return jsonable_encoder(self.branch(db, scope))

        @app.get("/api/km/v1/knowledge-bases/{knowledge_base_id}/versions")
        def list_versions(knowledge_base_id: str, request: Request, actor=Depends(principal)):
            scope = context(request, {"knowledge_base_id": knowledge_base_id}, actor)
            self.runtime.authorize(actor, scope, "read")
            with self.runtime.engine.connect() as db:
                statement = select(self.versions).where(self.versions.c.knowledge_base_id == knowledge_base_id)
                spaces = self.runtime.plugins.get("spaces")
                if spaces:
                    statement = statement.join(spaces.version_access, self.versions.c.id == spaces.version_access.c.version_id).where(spaces.version_access.c.space_id == scope["space_id"])
                return [dict(row) for row in db.execute(statement).mappings()]

        def operation(name):
            async def execute(knowledge_base_id: str, request: Request, actor=Depends(principal)):
                body = await value(request)
                scope = context(request, {**{key: value for key, value in body.items() if key != "version_id"}, "knowledge_base_id": knowledge_base_id}, actor)
                self.runtime.authorize(actor, scope, "write")
                validate(body, {"name", "version_id", "expected_updated_at"}, ("expected_updated_at",))
                with self.runtime.engine.begin() as db:
                    branch = self.branch(db, scope, create=True)
                    if branch["updated_at"].isoformat() != body["expected_updated_at"]: raise HTTPException(409, "Stale branch")
                    if name == "copy": return self.checkpoint(db, scope, body.get("name", "Snapshot"))
                    target_id = body.get("version_id") or branch["head_version_id"]
                    target = db.execute(select(self.versions).where(self.versions.c.id == target_id,
                        self.versions.c.knowledge_base_id == knowledge_base_id)).mappings().first()
                    if not target: raise HTTPException(404, "Version not found")
                    self.validate({**scope, "version_id": target_id}, "read")
                    if name == "publish":
                        db.execute(update(self.branches).where(self.branches.c.id == branch["id"]).values(published_version_id=target_id, updated_at=s.now()))
                    else:
                        return {"status": name, **self.restore(db, branch, target, scope)}
                    return {"status": "published", "version_id": target_id}
            return execute
        for name in ("copy", "sync", "publish", "rollback"):
            app.add_api_route("/api/km/v1/knowledge-bases/{knowledge_base_id}/versions/" + name, operation(name), methods=["POST"])


create = Versions
