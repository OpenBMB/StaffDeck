from datetime import timedelta
from fastapi import Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy import Column, DateTime, JSON, String, Table, select, update
from .base import Plugin
from ..kernel import storage as s


class Publication(Plugin):
    operations = {"publish_discovery": ("POST", "/suggestions/{suggestion_id}/publish")}

    def __init__(self, runtime):
        super().__init__(runtime)
        if runtime.publisher is None: self.operations = {}
        self.receipts = Table("km_publications", self.metadata, Column("suggestion_id", String, primary_key=True),
            Column("idempotency_key", String, unique=True), Column("status", String), Column("reference", JSON),
            Column("error", String), Column("updated_at", DateTime))

    def publish(self, suggestion_id, scope, actor):
        discovery = self.runtime.plugins["discovery"]
        suggestion = discovery.get(suggestion_id, scope, actor, "write")
        if suggestion["status"] not in {"reviewed", "confirmed"}: raise HTTPException(409, "Review before publishing")
        with self.runtime.engine.begin() as db:
            stored_context = db.execute(select(discovery.contexts.c.context).where(discovery.contexts.c.suggestion_id == suggestion_id)).scalar() or {}
            if stored_context.get('tenant_id', actor.tenant_id) != actor.tenant_id:
                raise HTTPException(404, "Suggestion not found")
            row = db.execute(select(self.receipts).where(self.receipts.c.suggestion_id == suggestion_id)).mappings().first()
            if row and row["status"] == "confirmed": return row["reference"]
            if not row:
                if suggestion["status"] == "confirmed": raise HTTPException(409, "Historical publication target is unknown")
                row = {"suggestion_id": suggestion_id, "idempotency_key": "km:" + actor.tenant_id + ":" + suggestion_id, "status": "retryable", "reference": None, "error": None, "updated_at": s.now()}
                db.execute(self.receipts.insert().values(**row))
            if row["status"] == "publishing" and row["updated_at"] > s.now() - timedelta(seconds=60): raise HTTPException(409, "Publication in progress")
            claimed = db.execute(update(self.receipts).where(self.receipts.c.suggestion_id == suggestion_id,
                self.receipts.c.updated_at == row["updated_at"]).values(status="publishing", updated_at=s.now()))
            if claimed.rowcount != 1: raise HTTPException(409, "Publication claimed concurrently")
        try:
            publication = {**suggestion, 'tenant_id': actor.tenant_id,
                           'knowledge_base_id': stored_context.get('knowledge_base_id', ''),
                           'payload_json': suggestion['payload']}
            reference = self.runtime.publisher.publish(jsonable_encoder(publication), row["idempotency_key"])
            if not isinstance(reference, dict) or not reference: raise ValueError("Publisher returned no reference")
        except Exception as error:
            with self.runtime.engine.begin() as db:
                db.execute(update(self.receipts).where(self.receipts.c.suggestion_id == suggestion_id).values(status="retryable", error=str(error), updated_at=s.now()))
            raise HTTPException(502, "Publisher unavailable") from error
        with self.runtime.engine.begin() as db:
            db.execute(update(self.receipts).where(self.receipts.c.suggestion_id == suggestion_id).values(status="confirmed", reference=reference, updated_at=s.now()))
            db.execute(update(discovery.suggestions).where(discovery.suggestions.c.id == suggestion_id).values(status="confirmed", updated_at=s.now()))
        return reference

    def register(self, app, dependencies):
        if self.runtime.publisher is None: return
        principal, context = (dependencies[key] for key in ("principal", "context"))

        @app.post("/api/km/v1/suggestions/{suggestion_id}/publish")
        def publish(suggestion_id: str, request: Request, actor=Depends(principal)):
            return self.publish(suggestion_id, context(request, {}, actor), actor)


create = Publication
