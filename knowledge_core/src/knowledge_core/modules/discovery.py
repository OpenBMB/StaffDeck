from pathlib import Path
from fastapi import Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy import Column, DateTime, JSON, String, Table, select, update, delete
from .base import Plugin
from ..kernel import storage as s


class Discovery(Plugin):
    schema_version = 2
    operations = {"list_discoveries": ("GET", "/suggestions"),
                  "confirm_discovery": ("POST", "/suggestions/{suggestion_id}/review"),
                  **{name + "_discovery": ("POST", "/suggestions/{suggestion_id}/" + name) for name in ("review", "reject", "export")}}

    def __init__(self, runtime):
        super().__init__(runtime)
        self.suggestions = Table("km_suggestions", self.metadata, Column("id", String, primary_key=True),
            Column("document_id", String, nullable=False), Column("title", String), Column("suggestion_type", String),
            Column("status", String), Column("payload", JSON), Column("source_refs", JSON), Column("reason", String),
            Column("created_at", DateTime), Column("updated_at", DateTime))
        self.contexts = Table("km_suggestion_contexts", self.metadata,
            Column("suggestion_id", String, primary_key=True), Column("context", JSON, nullable=False))

    def prepare(self, filename, text, specs, context, model, config):
        if not model or not config: return []
        try:
            raw = model.generate_json(config, (Path(__file__).parents[1] / "prompts/knowledge_discovery_prompt.md").read_text(),
                {"document": {"filename": filename}, "buckets": specs})
            return raw.get("discoveries", []) if isinstance(raw, dict) else []
        except Exception: return []

    def process(self, db, document_id, text, buckets, chunks, context, prepared=None):
        old = select(self.suggestions.c.id).where(self.suggestions.c.document_id == document_id,
            self.suggestions.c.status == 'pending')
        db.execute(delete(self.contexts).where(self.contexts.c.suggestion_id.in_(old)))
        db.execute(delete(self.suggestions).where(self.suggestions.c.id.in_(old)))
        for value in prepared or []:
            if not isinstance(value, dict) or not value.get("suggestion_type") or not isinstance(value.get("payload", {}), dict): continue
            suggestion_id = s.identifier("ksuggestion")
            db.execute(self.suggestions.insert().values(id=suggestion_id, document_id=document_id,
                title=str(value.get("title", "Suggestion")), suggestion_type=str(value["suggestion_type"]), status="pending",
                payload=value.get("payload", {}), source_refs=value.get("source_refs", []), reason=value.get("reason"), created_at=s.now(), updated_at=s.now()))
            db.execute(self.contexts.insert().values(suggestion_id=suggestion_id, context=dict(context)))

    def get(self, suggestion_id, scope, actor, action):
        self.runtime.authorize(actor, scope, action)
        with self.runtime.engine.connect() as db: row = db.execute(select(self.suggestions).where(self.suggestions.c.id == suggestion_id)).mappings().first()
        if not row: raise HTTPException(404, "Knowledge discovery not found" if self.runtime.configuration.full_contract else "Suggestion not found")
        if row['document_id']: self.runtime.require_content(row["document_id"], scope)
        else:
            with self.runtime.engine.connect() as db:
                stored = db.execute(select(self.contexts.c.context).where(self.contexts.c.suggestion_id == suggestion_id)).scalar() or {}
            if any(stored.get(key) != scope.get(key) for key in ('tenant_id', 'space_id') if key in scope): raise HTTPException(404, 'Suggestion not found')
        return dict(row)

    def snapshot(self, db, document_id):
        return [dict(row) for row in db.execute(select(self.suggestions).where(self.suggestions.c.document_id == document_id)).mappings()]

    def restore_content(self, db, value, document_id, mapping, preserve_metadata=False):
        from ..references import remap
        for source in value:
            row = {**source, "id": s.identifier("kdisc" if self.runtime.configuration.full_contract else "ksuggestion"), "document_id": document_id,
                   "payload": source['payload'] if preserve_metadata else remap(source["payload"], mapping), "source_refs": remap(source["source_refs"], mapping),
                   "created_at": s.now(), "updated_at": s.now()}
            db.execute(self.suggestions.insert().values(**row))
            before = db.execute(select(self.contexts.c.context).where(self.contexts.c.suggestion_id == source['id'])).scalar()
            if before:
                db.execute(self.contexts.insert().values(suggestion_id=row['id'], context=remap(before, mapping)))

    def register(self, app, dependencies):
        principal, context = (dependencies[key] for key in ("principal", "context"))

        @app.get("/api/km/v1/suggestions")
        def list_suggestions(request: Request, actor=Depends(principal)):
            scope = context(request, {}, actor); self.runtime.authorize(actor, scope, "read")
            with self.runtime.engine.connect() as db: rows = [dict(row) for row in db.execute(select(self.suggestions)).mappings()]
            return jsonable_encoder([row for row in rows if self.runtime.visible(row["document_id"], scope)])

        def operation(name):
            def execute(suggestion_id: str, request: Request, actor=Depends(principal)):
                scope = context(request, {}, actor); row = self.get(suggestion_id, scope, actor, "read" if name == "export" else "write")
                if name == "export": return jsonable_encoder(row)
                if row["status"] == "confirmed": raise HTTPException(409, "Published suggestion cannot be reviewed or rejected")
                status = "reviewed" if name == "review" else "rejected"
                with self.runtime.engine.begin() as db:
                    changed = db.execute(update(self.suggestions).where(self.suggestions.c.id == suggestion_id,
                        self.suggestions.c.updated_at == row["updated_at"]).values(status=status, updated_at=s.now()))
                    if changed.rowcount != 1: raise HTTPException(409, "Concurrent suggestion edit")
                return {"status": status, "published": False}
            return execute
        for name in ("review", "reject", "export"):
            app.add_api_route("/api/km/v1/suggestions/{suggestion_id}/" + name, operation(name), methods=["POST"])


create = Discovery
