from fastapi import HTTPException
from sqlalchemy import select
from . import storage as s


class Evidence:
    def __init__(self, runtime): self.runtime = runtime

    def resolve(self, chunk_id, context, actor):
        self.runtime.authorize(actor, context, "read")
        with self.runtime.engine.connect() as db:
            row = db.execute(select(s.citations).where(s.citations.c.id == chunk_id)).mappings().first()
        if not row: raise HTTPException(404, "Citation not found")
        if not row["document_id"] or not self.runtime.visible(row["document_id"], context):
            scopes = row["payload"].get("recovery_contexts", [])
            catalog = self.runtime.plugins.get("catalog")
            if catalog:
                visible_scopes = []
                for scope in scopes:
                    try: catalog.validate({**context, **scope})
                    except HTTPException: continue
                    visible_scopes.append(scope)
                scopes = visible_scopes
            if any(name in self.runtime.plugins for name in ("spaces", "catalog", "versions")) and ("spaces" in self.runtime.plugins or any(field in context for field in ("knowledge_base_id", "version_id", "version_ids", "knowledge_base_ids"))):
                if not any(all(context.get(field) == scope.get(field) for module, field in
                    (("spaces", "space_id"), ("catalog", "knowledge_base_id"), ("versions", "version_id"))
                    if module in self.runtime.plugins and (module == "spaces" or field in context)) for scope in scopes):
                    raise HTTPException(404, "Citation not visible in this context")
        return row["payload"]

    def bind_revisions(self, result):
        if self.runtime.configuration.full_contract: return result
        with self.runtime.engine.connect() as db:
            for row in result["chunks"]:
                citation_id = row["id"] + ":" + row["updated_at"].isoformat()
                if db.execute(select(s.citations.c.id).where(s.citations.c.id == citation_id)).first():
                    row["citation_id"] = citation_id
            for evidence in result["evidence_pack"]:
                hit = next(row for row in result["chunks"] if row["id"] == evidence["chunk_id"])
                evidence["chunk_id"] = hit.get("citation_id", hit["id"])
        return result
