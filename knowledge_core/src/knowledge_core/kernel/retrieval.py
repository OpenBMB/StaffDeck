from sqlalchemy import select
from . import storage as s
from .processing import Retrieval


class Search:
    def __init__(self, runtime): self.runtime = runtime

    def query(self, value, context, actor):
        docs = self.runtime.content.list(s.documents, context, actor)
        docs = [row for row in docs if row["status"] == "ready" and (not value.get("document_ids") or row["id"] in value["document_ids"])]
        ids = {row["id"] for row in docs}
        with self.runtime.engine.connect() as db:
            buckets = [dict(row) for row in db.execute(select(s.buckets).where(s.buckets.c.document_id.in_(ids))).mappings()]
            chunks = [dict(row) for row in db.execute(select(s.chunks).where(s.chunks.c.document_id.in_(ids))).mappings()]
        versions = self.runtime.plugins.get("versions")
        if versions and (context.get("version_id") or context.get("version_ids")):
            buckets = self.runtime.content.list(s.buckets, context, actor)
            chunks = self.runtime.content.list(s.chunks, context, actor)
        model, config, _ = self.runtime.ports(context, value.get("model_id"))
        result = Retrieval(model, config).query(value["query"], docs, buckets, chunks, value.get("max_chunks", 8), value.get("use_model", True), value)
        self.runtime.evidence.bind_revisions(result)
        for plugin in self.runtime.plugins.values(): plugin.retrieve(result, value, context)
        return result
