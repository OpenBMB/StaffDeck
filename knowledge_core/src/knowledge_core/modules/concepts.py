import base64
import json
from io import BytesIO
from types import SimpleNamespace
from zipfile import BadZipFile, ZipFile, ZIP_DEFLATED
from fastapi import Depends, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from sqlalchemy import Column, DateTime, JSON, String, Table, Text, delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from .base import Plugin
from . import okf_rules as rules
from ..kernel import storage as s
from ..kernel.algorithms import _build_section_nodes


class Concepts(Plugin):
    schema_version = 2
    operations = {"list_concepts": ("GET", "/concepts"), "upsert_concept": ("PUT", "/concepts/{concept_id:path}"),
                  "export_okf": ("GET", "/okf"), "import_okf": ("POST", "/okf"), "lint_okf": ("POST", "/okf/lint")}

    def __init__(self, runtime):
        super().__init__(runtime)
        self.concepts = Table("km_concepts", self.metadata, Column("id", String, primary_key=True),
            Column("document_id", String), Column("concept_id", String), Column("concept_type", String),
            Column("title", String), Column("content_md", Text), Column("links", JSON), Column("citations", JSON),
            Column("source_refs", JSON), Column("created_at", DateTime), Column("updated_at", DateTime))
        self.contexts = Table('km_concept_contexts', self.metadata, Column('concept_id', String, primary_key=True),
            Column('context', JSON, nullable=False))

    def upsert(self, db, value, document_id=None, expected=None):
        parsed = rules.parse_okf_markdown(value["concept_id"], value["content_md"])
        before = db.execute(select(self.concepts).where(self.concepts.c.concept_id == value["concept_id"],
            self.concepts.c.document_id == document_id)).mappings().first()
        if before and (not expected or before["updated_at"].isoformat() != expected): raise HTTPException(409, "Concurrent concept edit")
        row = {"id": before["id"] if before else s.identifier("kconcept"), "document_id": document_id,
            "concept_id": parsed.concept_id, "concept_type": parsed.frontmatter.get("type", "Topic"),
            "title": parsed.frontmatter.get("title", parsed.concept_id), "content_md": parsed.content_md,
            "links": rules.extract_links(parsed.body), "citations": rules.extract_citations(parsed.body),
            "source_refs": value.get("source_refs", []), "created_at": before["created_at"] if before else s.now(), "updated_at": s.now()}
        if before: db.execute(update(self.concepts).where(self.concepts.c.id == before["id"]).values(**row))
        else: db.execute(self.concepts.insert().values(**row))
        citation_id = row["id"] + ":" + row["updated_at"].isoformat()
        payload = {"id": citation_id, "concept_id": row["concept_id"], "document_id": document_id,
                   "content": row["content_md"], "source_ref": row["concept_id"] + ".md",
                   "recovery_contexts": []}
        if document_id:
            spaces = self.runtime.plugins.get("spaces")
            catalog = self.runtime.plugins.get("catalog")
            scopes = [dict(row) for row in db.execute(select(spaces.contents).where(spaces.contents.c.document_id == document_id)).mappings()] if spaces else [{}]
            bases = [dict(row) for row in db.execute(select(catalog.contents).where(catalog.contents.c.document_id == document_id)).mappings()] if catalog else [{}]
            payload["recovery_contexts"] = [{**space, **base} for space in scopes for base in bases]
        db.execute(sqlite_insert(s.citations).values(id=citation_id, document_id=document_id, payload=payload).on_conflict_do_nothing())
        for plugin in self.runtime.plugins.values(): plugin.content_changed(db, document_id)
        return row

    def process(self, db, document_id, text, buckets, chunks, context, prepared=None):
        row = db.execute(select(s.documents).where(s.documents.c.id == document_id)).mappings().one()
        document = SimpleNamespace(**dict(row), metadata_json=row["metadata"])
        items = [SimpleNamespace(**bucket, metadata_json=bucket["metadata"], bucket_key=bucket["metadata"].get('bucket_key', bucket["id"])) for bucket in buckets]
        db.execute(delete(self.concepts).where(self.concepts.c.document_id == document_id))
        for value in rules.build_okf_for_document(document, _build_section_nodes(text), items): self.upsert(db, value, document_id)

    def rows(self, context):
        versions = self.runtime.plugins.get("versions")
        if versions and (context.get("version_id") or context.get("version_ids")):
            ids = context.get("version_ids", []) or [context["version_id"]]
            with self.runtime.engine.connect() as db:
                snapshots = db.execute(select(versions.versions).where(versions.versions.c.id.in_(ids))).mappings().all()
            return [concept for version in snapshots for document in version["snapshot"].values()
                    for concept in document.get("modules", {}).get("concepts", [])]
        with self.runtime.engine.connect() as db: rows = [dict(row) for row in db.execute(select(self.concepts)).mappings()]
        with self.runtime.engine.connect() as db:
            contexts = {row['concept_id']: row['context'] for row in db.execute(select(self.contexts)).mappings()}
            head = versions.branch(db, context)['head_version_id'] if versions and context.get('knowledge_base_id') else None
        result = []
        for row in rows:
            if row['document_id']:
                if self.runtime.visible(row['document_id'], context): result.append(row)
                continue
            scope = contexts.get(row['id'], {})
            if head and scope.get('version_id') != head: continue
            if (not scope and not any(name in self.runtime.plugins for name in ('spaces', 'catalog', 'versions')) or
                all(scope.get(key) == context.get(key) for key in ('tenant_id', 'space_id', 'knowledge_base_id') if key in context)) and (
                not context.get('knowledge_base_ids') or scope.get('knowledge_base_id') in context['knowledge_base_ids']): result.append(row)
        return sorted(result, key=lambda row: (row['concept_type'], row['concept_id']))

    def snapshot(self, db, document_id):
        return [dict(row) for row in db.execute(select(self.concepts).where(self.concepts.c.document_id == document_id)).mappings()]

    def restore_content(self, db, value, document_id, mapping, preserve_metadata=False):
        from ..references import remap
        for row in value:
            self.upsert(db, {"concept_id": row["concept_id"], "content_md": row['content_md'] if preserve_metadata else remap(row["content_md"], mapping),
                            "source_refs": [{**ref, **({'document_id': mapping[ref['document_id']]} if ref.get('document_id') in mapping else {})}
                                for ref in row['source_refs']] if preserve_metadata else remap(row["source_refs"], mapping)}, document_id)

    def retrieve(self, result, value, context):
        if self.runtime.configuration.full_contract:
            rows = self.rows(context)
            items = []
            for row in rows:
                parsed = rules.parse_okf_markdown(row['concept_id'], row['content_md'])
                items.append(SimpleNamespace(**row, description=parsed.frontmatter.get('description'),
                    frontmatter_json=parsed.frontmatter, links_json=row['links'], citations_json=row['citations'], source_refs_json=row['source_refs']))
            selected = rules.search_concepts(value['query'].strip(), items, max(value.get('max_buckets', 4), 4))
            result['selected_concepts'] = rules.selected_concept_cards(selected)
            result['okf_citations'] = rules.okf_citations_for_concepts(selected)
            if rows: result['route_trace'].insert(0, {'phase': 'okf_concept_route', 'message': '正在选择 OKF Wiki 页面',
                'candidate_count': len(rows), 'selected_count': len(selected)})
            return
        tokens = rules._query_tokens(value["query"])
        rows = self.rows(context)
        rows.sort(key=lambda row: sum(token in (row["title"] + row["content_md"]).lower() for token in tokens), reverse=True)
        result["selected_concepts"] = [row for row in rows if any(token in (row["title"] + row["content_md"]).lower() for token in tokens)][:8]
        for row in result["selected_concepts"]:
            updated = row["updated_at"] if isinstance(row["updated_at"], str) else row["updated_at"].isoformat()
            citation_id = row["id"] + ":" + updated
            result["evidence_pack"].append({"chunk_id": citation_id, "concept_id": row["concept_id"],
                "document_id": row["document_id"], "content": row["content_md"], "source_ref": row["concept_id"] + ".md"})

    def register(self, app, dependencies):
        principal, context, value, validate = (dependencies[key] for key in ("principal", "context", "value", "validate"))

        @app.get("/api/km/v1/concepts")
        def list_concepts(request: Request, actor=Depends(principal)):
            scope = context(request, {}, actor); self.runtime.authorize(actor, scope, "read")
            return jsonable_encoder(self.rows(scope))

        @app.put("/api/km/v1/concepts/{concept_id:path}")
        async def upsert(concept_id: str, request: Request, actor=Depends(principal)):
            body = await value(request); scope = context(request, body, actor); self.runtime.authorize(actor, scope, "write")
            validate(body, {"content_md", "document_id", "expected_updated_at"}, ("content_md",))
            if self.runtime.configuration.enabled("spaces") and not body.get("document_id"):
                raise HTTPException(422, "document_id required for a scoped concept")
            if body.get("document_id"): self.runtime.content.get(s.documents, body["document_id"], scope, actor, "write")
            with self.runtime.engine.begin() as db:
                return jsonable_encoder(self.upsert(db, {**body, "concept_id": concept_id}, body.get("document_id"), body.get("expected_updated_at")))

        @app.get("/api/km/v1/okf")
        def export(request: Request, actor=Depends(principal)):
            scope = context(request, {}, actor); self.runtime.authorize(actor, scope, "read")
            output = BytesIO()
            with ZipFile(output, "w", ZIP_DEFLATED) as archive:
                manifest = []
                for row in self.rows(scope):
                    path = row["id"] + "/" + row["concept_id"] + ".md"
                    archive.writestr(path, row["content_md"])
                    manifest.append({"path": path, "concept_id": row["concept_id"], "document_id": row["document_id"]})
                archive.writestr("km-manifest.json", json.dumps({"protocol": "knowledge-okf/v1", "concepts": manifest}))
            return Response(output.getvalue(), media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="knowledge.okf.zip"'})

        @app.post("/api/km/v1/okf")
        async def import_okf(request: Request, actor=Depends(principal)):
            body = await value(request); scope = context(request, body, actor); self.runtime.authorize(actor, scope, "write")
            validate(body, {"filename", "content_base64", "document_id"}, ("filename", "content_base64"))
            if self.runtime.configuration.enabled("spaces") and not body.get("document_id"): raise HTTPException(422, "document_id required for scoped OKF")
            if body.get("document_id"): self.runtime.content.get(s.documents, body["document_id"], scope, actor, "write")
            try:
                content = base64.b64decode(body["content_base64"], validate=True)
                parsed = rules.parse_okf_bundle(body["filename"], content)
                if body["filename"].lower().endswith(".zip"):
                    with ZipFile(BytesIO(content)) as archive:
                        if "km-manifest.json" in archive.namelist():
                            manifest = json.loads(archive.read("km-manifest.json"))
                            if manifest.get("protocol") != "knowledge-okf/v1" or not isinstance(manifest.get("concepts"), list):
                                raise ValueError("Invalid OKF manifest")
                            paths = {rules.normalize_concept_id(row["path"]): row["concept_id"] for row in manifest["concepts"]}
                            parsed = [rules.parse_okf_markdown(paths.get(item.concept_id, item.concept_id), item.content_md) for item in parsed]
            except (ValueError, KeyError, TypeError, BadZipFile) as error: raise HTTPException(422, str(error)) from error
            with self.runtime.engine.begin() as db:
                rows = [self.upsert(db, {"concept_id": item.concept_id, "content_md": item.content_md}, body.get("document_id")) for item in parsed]
            return {"concepts": jsonable_encoder(rows)}

        @app.post("/api/km/v1/okf/lint")
        def lint(request: Request, actor=Depends(principal)):
            scope = context(request, {}, actor); self.runtime.authorize(actor, scope, "read")
            rows = [SimpleNamespace(**row, frontmatter_json=rules.parse_okf_markdown(row["concept_id"], row["content_md"]).frontmatter,
                    citations_json=row["citations"], links_json=row["links"]) for row in self.rows(scope)]
            issues = rules.lint_okf_concepts(rows)
            return {"issues": issues, "issue_count": len(issues)}


create = Concepts
