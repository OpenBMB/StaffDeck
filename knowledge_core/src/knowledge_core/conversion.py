"""Offline, staged SQLite conversion. Source databases are opened read-only."""
from datetime import datetime, UTC
import json
import os
from pathlib import Path
import sqlite3
import shutil
from uuid import uuid4
from sqlalchemy import insert, select, text
from .configuration import Configuration
from .runtime import Runtime
from .kernel import storage as s
from .references import remap


def durable(file):
    descriptor = os.open(file, os.O_RDONLY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def write_journal(journal, state):
    journal.write_text(json.dumps(state))
    durable(journal)
    durable(journal.parent)


def read_database(path):
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result = {}
    try:
        for name in sorted(tables):
            if not name.startswith("sqlite_"):
                result[name] = [dict(row) for row in connection.execute('SELECT * FROM "' + name.replace('"', '""') + '"')]
    finally: connection.close()
    return result


def decode(value, default=None):
    return json.loads(value) if isinstance(value, str) and value else value if value is not None else default


def timestamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=None) if isinstance(value, str) else value


def current_documents(data):
    if "km_documents" in data:
        # A snapshot clone retains its source ID/revision. Never deduplicate by text/title.
        rows = [row for row in data["km_documents"] if row["status"] == "ready"]
        if "km_branch_contents" in data:
            heads = {row["document_id"] for row in data["km_branch_contents"]}
            catalog_ids = {row["document_id"] for row in data.get("km_catalog_contents", [])}
            rows = [row for row in rows if row["id"] in heads or row["id"] not in catalog_ids]
        if "km_space_contents" in data:
            visible = {row["document_id"] for row in data["km_space_contents"]}
            rows = [row for row in rows if row["id"] in visible]
            if "km_catalog_space_bindings" in data:
                bindings = {(row["knowledge_base_id"], row["space_id"]) for row in data["km_catalog_space_bindings"] if not row.get("hidden", False)}
                memberships = {(row["document_id"], row["space_id"]) for row in data["km_space_contents"]}
                ownership = {}
                for link in data.get("km_catalog_contents", []): ownership.setdefault(link["document_id"], set()).add(link["knowledge_base_id"])
                rows = [row for row in rows if not ownership.get(row["id"]) or any(
                    (base, space) in bindings for base in ownership[row["id"]] for document, space in memberships if document == row["id"])]
        return rows
    visible_bindings = {(row["space_id"], row["knowledge_base_id"]) for row in data.get("knowledge_bindings", [])
                        if not row["hidden"]}
    heads = {row["head_version_id"] for row in data.get("knowledge_branches", [])
             if row["status"] == "active" and (row["space_id"], row["knowledge_base_id"]) in visible_bindings}
    return [row for row in data.get("knowledge_documents", []) if row["status"] == "ready" and
            (row["knowledge_base_version_id"] in heads or not row["knowledge_base_version_id"])]


def preflight(path, configuration):
    data = read_database(path)
    jobs = data.get("km_ingest_jobs", data.get("knowledge_ingest_jobs", []))
    active = [row["id"] for row in jobs if row["status"] not in {"succeeded", "failed", "cancelled"}]
    errors = ["Finish or cancel active tasks: " + ", ".join(active)] if active else []
    if not ("km_documents" in data or "knowledge_documents" in data): errors.append("Not a Knowledge Core database")
    if "km_schema_versions" in data:
        current = {row["module"] for row in data["km_schema_versions"]} - {"core"}
        requested = {name for name, enabled in configuration.modules.items() if enabled}
        if requested - current: errors.append("Conversion only disables modules; enable new modules after conversion")
    errors.extend(link_errors(data))
    return {"ok": not errors, "errors": errors, "source": str(path.resolve()), "target": configuration.as_dict(),
            "counts": {"spaces": len(data.get("km_spaces", data.get("knowledge_spaces", []))),
                       "catalogs": len(data.get("km_catalogs", data.get("knowledge_bases", []))),
                       "branches": len(data.get("km_branches", data.get("knowledge_branches", []))),
                       "current_documents": len(current_documents(data)), "tasks": len(jobs)},
            "source_tables": sorted(data)}


def link_errors(data):
    errors = []
    legacy = "knowledge_documents" in data
    prefix = "knowledge_" if legacy else "km_"
    documents = {row["id"] for row in data.get(prefix + "documents", [])}
    buckets = {row["id"] for row in data.get(prefix + "buckets", [])}
    for name in ("buckets", "chunks"):
        for row in data.get(prefix + name, []):
            if row["document_id"] not in documents: errors.append(f"{prefix}{name}/{row['id']}: missing document {row['document_id']}")
            if name == "chunks" and row["bucket_id"] not in buckets: errors.append(f"{prefix}{name}/{row['id']}: missing bucket {row['bucket_id']}")
    if not legacy:
        for name in ("km_catalog_contents", "km_space_contents", "km_branch_contents"):
            for row in data.get(name, []):
                if row["document_id"] not in documents: errors.append(f"{name}: missing document {row['document_id']}")
        references = {
            "km_catalog_contents": {"knowledge_base_id": "km_catalogs"},
            "km_space_contents": {"space_id": "km_spaces"},
            "km_catalog_space_bindings": {"space_id": "km_spaces", "knowledge_base_id": "km_catalogs"},
            "km_branches": {"knowledge_base_id": "km_catalogs"},
            "km_branch_contents": {"branch_id": "km_branches"},
            "km_branch_spaces": {"branch_id": "km_branches", "space_id": "km_spaces"},
            "km_version_spaces": {"version_id": "km_versions", "space_id": "km_spaces"},
            "km_versions": {"knowledge_base_id": "km_catalogs"},
            "km_publications": {"suggestion_id": "km_suggestions"},
        }
    else:
        references = {
            "knowledge_bindings": {"space_id": "knowledge_spaces", "knowledge_base_id": "knowledge_bases"},
            "knowledge_branches": {"space_id": "knowledge_spaces", "knowledge_base_id": "knowledge_bases",
                                   "head_version_id": "knowledge_base_versions", "base_version_id": "knowledge_base_versions"},
            "knowledge_documents": {"knowledge_base_id": "knowledge_bases", "knowledge_base_version_id": "knowledge_base_versions"},
            "knowledge_publications": {"suggestion_id": "knowledge_discovery_suggestions"},
        }
    for table, fields in references.items():
        for field, target in fields.items():
            identifiers = {row['id'] for row in data.get(target, [])}
            for row in data.get(table, []):
                if row.get(field) and row[field] not in identifiers: errors.append(f"{table}: missing {target}/{row[field]}")
    return errors


def validate_staged(runtime, details):
    data = read_database(Path(runtime.engine.url.database))
    errors = link_errors(data)
    if errors: raise ValueError("Staged links invalid: " + "; ".join(errors))
    with runtime.engine.connect() as db:
        if db.execute(text("PRAGMA integrity_check")).scalar() != "ok": raise ValueError("Staged SQLite validation failed")
        for name, table in (("documents", s.documents), ("buckets", s.buckets), ("chunks", s.chunks), ("citations", s.citations)):
            if len(db.execute(select(table)).all()) != details["counts"][name]: raise ValueError(name + " count mismatch")


def core_row(row, table, legacy=False):
    result = {key: row[key] for key in table.c.keys() if key in row}
    for field in ("metadata", "context", "principal", "payload"):
        if field in table.c: result[field] = decode(row.get(field + "_json" if legacy and field == "metadata" else field), {} if field != "payload" else None)
    for field in ("created_at", "updated_at", "finished_at"):
        if field in result: result[field] = timestamp(result[field])
    if table is s.documents:
        metadata = result["metadata"]
        result["content"] = row.get("content", metadata.get("raw_text", ""))
    if table is s.buckets:
        result["content"] = row.get("content", result["metadata"].get("content", row.get("summary", "")))
        if legacy and row.get('bucket_key'):
            result['metadata'] = {**result['metadata'], 'bucket_key': row['bucket_key']}
    return result


def recovery_contexts(data, document_id, source, configuration):
    if "knowledge_documents" in data:
        base = source.get("knowledge_base_id")
        version = source.get("knowledge_base_version_id")
        return [{key: value for key, value in {"knowledge_base_id": base, "version_id": version}.items()
                 if configuration.enabled("catalog" if key == "knowledge_base_id" else "versions") and value}]
    bases = [row["knowledge_base_id"] for row in data.get("km_catalog_contents", []) if row["document_id"] == document_id] or [None]
    spaces = [row["space_id"] for row in data.get("km_space_contents", []) if row["document_id"] == document_id] or [None]
    return [{**({"knowledge_base_id": base} if configuration.enabled("catalog") and base else {}),
             **({"space_id": space} if configuration.enabled("spaces") and space else {})} for base in bases for space in spaces]


def legacy_scope_data(data):
    """Translate historical ownership into module-owned relations without writing the source."""
    converted = {}
    for name, table in (("documents", s.documents), ("buckets", s.buckets), ("chunks", s.chunks), ("ingest_jobs", s.jobs)):
        converted["km_" + name] = [core_row(row, table, True) for row in data.get("knowledge_" + name, [])]
    converted["km_catalogs"] = [{**{key: row.get(key) for key in ("id", "tenant_id", "name", "description", "status", "capability_scope", "created_at", "updated_at")},
                                'metadata': decode(row.get('metadata_json'), {})}
                               for row in data.get("knowledge_bases", [])]
    converted["km_catalog_contents"] = [{"knowledge_base_id": row["knowledge_base_id"], "document_id": row["id"]}
                                        for row in data.get("knowledge_documents", [])]
    converted["km_spaces"] = [{key: row[key] for key in ("id", "name", "tenant_id", 'owner_id', 'owner_name', 'status', 'created_at', 'updated_at') if key in row} for row in data.get("knowledge_spaces", [])]
    converted["km_catalog_space_bindings"] = [{**{key: row[key] for key in ("id", "knowledge_base_id", "space_id", "hidden", 'created_at', 'updated_at')},
        'metadata': decode(row.get('metadata_json'), {})}
                                             for row in data.get("knowledge_bindings", [])]
    converted["km_branches"] = [{key: row.get(key) for key in ("id", "knowledge_base_id", 'base_version_id', "head_version_id", 'status', 'sync_state', 'created_at', "updated_at")}
                               for row in data.get("knowledge_branches", [])]
    converted["km_branch_spaces"] = [{"branch_id": row["id"], "space_id": row["space_id"]}
                                    for row in data.get("knowledge_branches", [])]
    converted["km_version_spaces"] = [{"version_id": row["version_id"], "space_id": row["space_id"]}
                                     for row in data.get("knowledge_version_access", [])]
    converted["km_concepts"] = [{"id": row["id"], "document_id": row["document_id"], "concept_id": row["concept_id"],
        "concept_type": row["concept_type"], "title": row["title"], "content_md": row["content_md"],
        "links": decode(row["links_json"], []), "citations": decode(row["citations_json"], []),
        "source_refs": decode(row["source_refs_json"], []), "created_at": row["created_at"], "updated_at": row["updated_at"]}
        for row in data.get("knowledge_concepts", [])]
    converted['km_concept_contexts'] = [{'concept_id': row['id'], 'context': {
        'tenant_id': row['tenant_id'], 'knowledge_base_id': row['knowledge_base_id'],
        'version_id': row.get('knowledge_base_version_id')}} for row in data.get('knowledge_concepts', [])]
    converted["km_suggestions"] = [{"id": row["id"], "document_id": row["document_id"], "title": row["title"],
        "suggestion_type": row["suggestion_type"], "status": row["status"], "payload": decode(row["payload_json"], {}),
        "source_refs": decode(row["source_refs_json"], []), "reason": row["reason"], "created_at": row["created_at"], "updated_at": row["updated_at"]}
        for row in data.get("knowledge_discovery_suggestions", [])]
    converted['km_suggestion_contexts'] = [{'suggestion_id': row['id'], 'context': {
        'tenant_id': row['tenant_id'], 'knowledge_base_id': row['knowledge_base_id'],
        'version_id': row.get('knowledge_base_version_id')}} for row in data.get('knowledge_discovery_suggestions', [])]
    converted["km_publications"] = [{"suggestion_id": row["suggestion_id"], "idempotency_key": row["idempotency_key"],
        "status": row["status"], "reference": decode(row["target_reference"], {}), "error": row["error"], "updated_at": row["updated_at"]}
        for row in data.get("knowledge_publications", [])]
    converted["km_branch_contents"], converted["km_space_contents"], converted["km_versions"] = [], [], []
    documents = {row["id"]: row for row in converted["km_documents"]}
    from fastapi.encoders import jsonable_encoder
    for version in data.get("knowledge_base_versions", []):
        snapshot = {}
        for source in data.get("knowledge_documents", []):
            if source["knowledge_base_version_id"] != version["id"]: continue
            document = {**documents[source["id"]],
                "buckets": [row for row in converted["km_buckets"] if row["document_id"] == source["id"]],
                "chunks": [row for row in converted["km_chunks"] if row["document_id"] == source["id"]],
                "modules": {"concepts": [row for row in converted["km_concepts"] if row["document_id"] == source["id"]],
                            "discovery": [row for row in converted["km_suggestions"] if row["document_id"] == source["id"]]}}
            snapshot[source["id"]] = jsonable_encoder(document)
        converted["km_versions"].append({"id": version["id"], "knowledge_base_id": version["knowledge_base_id"],
            "name": version["name"], "parent_id": None, "snapshot": snapshot, "created_at": version["created_at"],
            **{key: version[key] for key in ('version', 'description', 'status', 'capability_scope', 'updated_at')},
            'metadata': decode(version.get('metadata_json'), {})})
    memberships = set()
    for branch in data.get("knowledge_branches", []):
        if branch["status"] != "active": continue
        for document in data.get("knowledge_documents", []):
            if document["knowledge_base_version_id"] == branch["head_version_id"]:
                converted["km_branch_contents"].append({"branch_id": branch["id"], "document_id": document["id"]})
                memberships.add((branch["space_id"], document["id"]))
    for access in data.get("knowledge_version_access", []):
        for document in data.get("knowledge_documents", []):
            if document["knowledge_base_version_id"] == access["version_id"]: memberships.add((access["space_id"], document["id"]))
    converted["km_space_contents"] = [{"space_id": space, "document_id": document} for space, document in sorted(memberships)]
    converted["km_citation_snapshots"] = [{"id": row["id"], "document_id": decode(row["payload_json"], {}).get("document_id"),
                                           "payload": decode(row["payload_json"], {})} for row in data.get("knowledge_citation_snapshots", [])]
    for job in converted["km_ingest_jobs"]:
        source = next(row for row in data["knowledge_ingest_jobs"] if row["id"] == job["id"])
        scope = next((row["space_id"] for row in data.get("knowledge_branches", []) if row["head_version_id"] == source["knowledge_base_version_id"]), None)
        job["context"] = {"tenant_id": source["tenant_id"], "knowledge_base_id": source["knowledge_base_id"],
                          **({"space_id": scope} if scope else {}), "version_id": source["knowledge_base_version_id"]}
    return converted


def populate(runtime, data):
    if "knowledge_documents" in data:
        data = legacy_scope_data(data)
    legacy = "knowledge_documents" in data
    prefix = "knowledge_" if legacy else "km_"
    rows = current_documents(data)
    mapping, revisions, selected = {}, {}, []
    for row in rows:
        metadata = decode(row.get("metadata_json" if legacy else "metadata"), {})
        source = metadata.get("revision_source_id", metadata.get("copied_from_document_id", row["id"]))
        revision = metadata.get("revision_source_updated_at", row["updated_at"])
        if isinstance(revision, str):
            try: revision = timestamp(revision).isoformat()
            except ValueError: pass
        key = (source, revision)
        if key in revisions:
            mapping[row["id"]] = revisions[key]
        else:
            resource_id = row["id"]
            if resource_id in selected: resource_id = s.identifier("kdoc")
            mapping[row["id"]] = resource_id; revisions[key] = resource_id; selected.append(resource_id)
    counts = {"documents": 0, "buckets": 0, "chunks": 0, "citations": 0}
    inserted = set()
    canonical = {}
    all_documents = {row["id"]: row for row in data.get(prefix + "documents", [])}
    bucket_mapping, chunk_mapping = {}, {}
    citation_mapping = {}
    with runtime.engine.begin() as db:
        for source in rows:
            document_id = mapping[source["id"]]
            if document_id in inserted: continue
            row = core_row(source, s.documents, legacy); row["id"] = document_id
            db.execute(insert(s.documents).values(**row)); inserted.add(document_id); canonical[document_id] = source["id"]; counts["documents"] += 1
        for name, table in (("buckets", s.buckets), ("chunks", s.chunks)):
            for source in data.get(prefix + name, []):
                if source["document_id"] not in mapping: continue
                if canonical[mapping[source["document_id"]]] != source["document_id"]: continue
                row = core_row(source, table, legacy); row["document_id"] = mapping[source["document_id"]]
                db.execute(insert(table).values(**row)); counts[name] += 1
                (bucket_mapping if table is s.buckets else chunk_mapping)[source["id"]] = row["id"]
                if table is s.chunks:
                    db.execute(insert(s.citations).values(id=row["id"], document_id=row["document_id"], payload=remap({key: value.isoformat() if isinstance(value, datetime) else value for key, value in row.items()}, mapping)))
                    counts["citations"] += 1
                    citation_mapping[source["id"]] = row["id"]
        known = set(db.execute(select(s.chunks.c.id)).scalars())
        for source in data.get(prefix + "citation_snapshots", []):
            if source["id"] in known: continue
            payload = decode(source.get("payload_json" if legacy else "payload"), {})
            document_id = source.get("document_id") or payload.get("document_id")
            payload = {**payload, "recovery_contexts": recovery_contexts(data, document_id, all_documents.get(document_id, source), runtime.configuration)}
            # Archived revision evidence is retained in the core snapshot table.
            db.execute(insert(s.citations).values(id=source["id"], document_id=mapping.get(document_id), payload=remap(payload, mapping)))
            counts["citations"] += 1
            citation_mapping[source["id"]] = source["id"]
        for source in data.get(prefix + "chunks", []):
            if source["id"] in known or any(row["id"] == source["id"] for row in data.get(prefix + "citation_snapshots", [])): continue
            row = core_row(source, s.chunks, legacy)
            payload = {key: value.isoformat() if isinstance(value, datetime) else value for key, value in row.items()}
            payload["recovery_contexts"] = recovery_contexts(data, source["document_id"], all_documents.get(source["document_id"], source), runtime.configuration)
            db.execute(insert(s.citations).values(id=row["id"], document_id=mapping.get(source["document_id"]),
                payload=payload))
            counts["citations"] += 1
            citation_mapping[source["id"]] = row["id"]
        for source in data.get(prefix + "ingest_jobs", []):
            row = core_row(source, s.jobs, legacy)
            row["document_id"] = mapping.get(source.get("document_id"))
            row["payload"] = None
            row["context"] = {key: value for key, value in decode(source.get("context"), {}).items() if
                key == "tenant_id" or (key == "space_id" and runtime.configuration.enabled("spaces")) or
                (key == "knowledge_base_id" and runtime.configuration.enabled("catalog")) or
                (key == "version_id" and runtime.configuration.enabled("versions"))}
            row["principal"] = decode(source.get("principal"), {"id": "migration"})
            db.execute(insert(s.jobs).values(**row))
        if "catalog" in runtime.plugins:
            plugin = runtime.plugins["catalog"]
            catalogs = data.get("knowledge_bases" if legacy else "km_catalogs", [])
            for source in catalogs:
                    db.execute(plugin.catalogs.insert().values(id=source["id"], name=source["name"], description=source.get("description"),
                        tenant_id=source.get('tenant_id', 'local'), metadata=decode(source.get('metadata_json') if legacy else source.get('metadata'), {}),
                        capability_scope=source.get('capability_scope', 'general'),
                        status=source.get("status", "active"), created_at=timestamp(source.get("created_at")) or s.now(), updated_at=timestamp(source.get("updated_at")) or s.now()))
            links = [{"knowledge_base_id": row["knowledge_base_id"], "document_id": row["id"]} for row in rows] if legacy else data.get("km_catalog_contents", [])
            seen = set()
            for source in links:
                if source["document_id"] not in mapping: continue
                key = (source["knowledge_base_id"], mapping[source["document_id"]])
                if key not in seen: db.execute(plugin.contents.insert().values(knowledge_base_id=key[0], document_id=key[1])); seen.add(key)
        # Retained module records must be copied rather than regenerated.
        merged_branches = {}
        if "versions" in runtime.plugins and not runtime.configuration.enabled("spaces"):
            for source in data.get("km_branches", []):
                merged_branches[source["id"]] = "merged:" + source["knowledge_base_id"]
        for name, plugin in runtime.plugins.items():
            if name == "catalog": continue
            for table in plugin.metadata.tables.values():
                if table.name == "km_branches" and merged_branches:
                    bases = sorted({row["knowledge_base_id"] for row in data["km_branches"]})
                    for base_id in bases:
                        db.execute(table.insert().values(id="merged:" + base_id, knowledge_base_id=base_id,
                            head_version_id=None, published_version_id=None, updated_at=s.now()))
                    continue
                if table.name == "km_branch_contents" and merged_branches:
                    seen = set()
                    for source in data.get(table.name, []):
                        if source["document_id"] not in mapping: continue
                        key = (merged_branches[source["branch_id"]], mapping[source["document_id"]])
                        if key not in seen: db.execute(table.insert().values(branch_id=key[0], document_id=key[1])); seen.add(key)
                    continue
                if table.name == "km_space_contents":
                    seen = set()
                    for source in data.get(table.name, []):
                        if source["document_id"] not in mapping: continue
                        key = (source["space_id"], mapping[source["document_id"]])
                        if key not in seen: db.execute(table.insert().values(space_id=key[0], document_id=key[1])); seen.add(key)
                    continue
                if table.name not in data:
                    raise ValueError("Retaining legacy " + name + " requires an explicit module mapping")
                seen = set()
                for source in data[table.name]:
                    if "document_id" in source and source["document_id"] and source["document_id"] not in mapping:
                        continue
                    if table.name == 'km_suggestion_contexts':
                        discovery = runtime.plugins['discovery']
                        if not db.execute(select(discovery.suggestions.c.id).where(discovery.suggestions.c.id == source['suggestion_id'])).first(): continue
                    if table.name == 'km_concept_contexts':
                        concepts = runtime.plugins['concepts']
                        if not db.execute(select(concepts.concepts.c.id).where(concepts.concepts.c.id == source['concept_id'])).first(): continue
                    row = {key: source[key] for key in table.c.keys() if key in source}
                    for column in table.c:
                        if column.name not in row: continue
                        if str(column.type) == "JSON": row[column.name] = decode(row[column.name])
                        if str(column.type) == "DATETIME": row[column.name] = timestamp(row[column.name])
                    if table.name == "km_versions":
                        for document in row["snapshot"].values():
                            document["modules"] = {name: value for name, value in document.get("modules", {}).items()
                                                   if runtime.configuration.enabled(name)}
                    else: row = remap(row, mapping)
                    if table.name == "km_publications":
                        discovery = runtime.plugins["discovery"]
                        if not db.execute(select(discovery.suggestions.c.id).where(discovery.suggestions.c.id == row["suggestion_id"])).first(): continue
                    key = tuple(row[column.name] for column in table.primary_key.columns)
                    if key in seen: continue
                    seen.add(key)
                    if not db.execute(select(table).where(*[column == row[column.name] for column in table.primary_key.columns])).first():
                        db.execute(table.insert().values(**row))
        if merged_branches:
            for base_id in sorted({row["knowledge_base_id"] for row in data["km_branches"]}):
                runtime.plugins["versions"].checkpoint(db, {"knowledge_base_id": base_id}, "Merged current branches")
        backup_id = uuid4().hex
        db.execute(text("CREATE TABLE km_conversion_guard (id INTEGER PRIMARY KEY, writes INTEGER NOT NULL, backup_id TEXT NOT NULL)"))
        db.execute(text("INSERT INTO km_conversion_guard VALUES (1, 0, :backup_id)"), {"backup_id": backup_id})
        from .kernel.schema import install_write_guard
        install_write_guard(db, [*s.metadata.tables, *(name for plugin in runtime.plugins.values() for name in plugin.metadata.tables)])
    for table_name, target in (("buckets", bucket_mapping), ("chunks", chunk_mapping)):
        for source in data.get(prefix + table_name, []):
            target.setdefault(source["id"], None)
    return {"counts": counts, "document_mapping": mapping, "bucket_mapping": bucket_mapping,
            "chunk_mapping": chunk_mapping, "citation_mapping": citation_mapping,
            "task_mapping": {row["id"]: row["id"] for row in data.get(prefix + "ingest_jobs", [])}, "backup_id": backup_id}


def convert(path, configuration, backup_root, source_config=None):
    from .locking import DatabaseLock
    with DatabaseLock("sqlite:///" + str(path.absolute())):
        return _convert(path, configuration, backup_root, source_config)


def _convert(path, configuration, backup_root, source_config=None):
    report = preflight(path, configuration)
    if not report["ok"]: return report
    if path.with_name(path.name + "-wal").exists():
        raise ValueError("Stop the service and checkpoint SQLite WAL before conversion")
    data = read_database(path)
    backup = backup_root.resolve() / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8])
    backup.mkdir(parents=True, exist_ok=False)
    stage = path.with_name(path.name + ".conversion-" + uuid4().hex)
    replaced = False
    config_stage = None
    journal = path.with_name(path.name + ".km-conversion.json")
    from .identity import StaticBearerIdentity
    runtime = Runtime("sqlite:///" + str(stage.resolve()), configuration=configuration,
                      identity=StaticBearerIdentity({}) if configuration.enabled("authentication") else None)
    try:
        runtime.initialize()
        if configuration.full_contract:
            # Conversion copies the source directory, including its timestamps.
            # Remove the new-install placeholder before copying that directory.
            with runtime.engine.begin() as db:
                db.execute(runtime.plugins['spaces'].spaces.delete())
        details = populate(runtime, data)
        validate_staged(runtime, details)
        runtime.close()
        with sqlite3.connect(stage.resolve().as_uri() + "?mode=ro", uri=True) as source, sqlite3.connect(backup / "converted.sqlite") as target:
            source.backup(target)
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as source, sqlite3.connect(backup / "original.sqlite") as target: source.backup(target)
        original_config = source_config.read_text() if source_config else json.dumps(Configuration("full").as_dict() if "knowledge_documents" in data else {"protocol": "knowledge-config/v1", "preset": "minimal", "modules": {row["module"]: True for row in data["km_schema_versions"] if row["module"] != "core"}})
        (backup / "original-config.json").write_text(original_config)
        (backup / "target-config.json").write_text(json.dumps(configuration.as_dict(), indent=2))
        report.update(details, backup=str(backup), active_database=str(path.resolve()),
                      active_configuration=str(source_config.resolve()) if source_config else None, ok=True)
        (backup / "report.json").write_text(json.dumps(report, indent=2))
        if source_config:
            config_stage = source_config.with_name(source_config.name + ".conversion-" + uuid4().hex)
            config_stage.write_text(json.dumps(configuration.as_dict(), indent=2))
        for file in [*(backup.iterdir()), *([config_stage] if config_stage else [])]: durable(file)
        durable(backup); durable(backup.parent); durable(stage)
        write_journal(journal, {"protocol": "knowledge-conversion-journal/v1", "operation": "convert", "backup": str(backup),
            "active_database": str(path.resolve()), "active_configuration": str(source_config.resolve()) if source_config else None})
        os.replace(stage, path)
        durable(path.parent)
        replaced = True
        if source_config:
            os.replace(config_stage, source_config)
            durable(source_config.parent)
        journal.unlink()
        durable(journal.parent)
        return report
    except Exception:
        runtime.close()
        if replaced:
            shutil.copyfile(backup / "original.sqlite", stage)
            os.replace(stage, path)
            durable(path.parent)
            if source_config:
                config_stage = source_config.with_name(source_config.name + ".rollback-" + uuid4().hex)
                config_stage.write_text((backup / "original-config.json").read_text())
                durable(config_stage)
                os.replace(config_stage, source_config)
                durable(source_config.parent)
        if journal.exists(): journal.unlink()
        if stage.exists(): stage.unlink()
        raise
    finally:
        if config_stage and config_stage.exists(): config_stage.unlink()


def restore(path, backup, source_config=None):
    from .locking import DatabaseLock
    with DatabaseLock("sqlite:///" + str(path.absolute())):
        return _restore(path, backup, source_config)


def _restore(path, backup, source_config=None):
    if path.with_name(path.name + "-wal").exists(): raise ValueError("Stop the service and checkpoint SQLite WAL before restore")
    report = json.loads((backup / "report.json").read_text())
    if report["active_database"] != str(path.resolve()): raise ValueError("Backup belongs to a different database")
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        writes, backup_id = db.execute("SELECT writes, backup_id FROM km_conversion_guard WHERE id=1").fetchone()
        if backup_id != report["backup_id"]: raise ValueError("Backup does not match the active conversion")
        if writes:
            raise ValueError("New writes exist; export new content before an explicit recovery merge")
    config = source_config or (Path(report["active_configuration"]) if report.get("active_configuration") else None)
    if config and report.get("active_configuration") and str(config.resolve()) != report["active_configuration"]:
        raise ValueError("Configuration path differs from the converted deployment")
    stage = path.with_name(path.name + ".restore-" + uuid4().hex)
    config_stage = config.with_name(config.name + ".restore-" + uuid4().hex) if config else None
    previous = path.with_name(path.name + ".before-restore-" + uuid4().hex)
    journal = path.with_name(path.name + ".km-conversion.json")
    replaced = False
    previous_configuration = config.read_text() if config else None
    try:
        with sqlite3.connect((backup / "original.sqlite").resolve().as_uri() + "?mode=ro", uri=True) as source, sqlite3.connect(stage) as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok": raise ValueError("Backup validation failed")
        if config_stage: config_stage.write_text((backup / "original-config.json").read_text())
        shutil.copyfile(path, previous)
        durable(stage); durable(previous)
        if config_stage: durable(config_stage)
        write_journal(journal, {"protocol": "knowledge-conversion-journal/v1", "operation": "restore",
            "backup": str(backup.resolve()), "active_database": str(path.resolve()),
            "active_configuration": str(config.resolve()) if config else None,
            "previous_database": str(previous.resolve())})
        os.replace(stage, path); replaced = True
        durable(path.parent)
        if config_stage:
            os.replace(config_stage, config)
            durable(config.parent)
        journal.unlink(); durable(journal.parent)
    except Exception:
        if replaced:
            os.replace(previous, path)
            durable(path.parent)
            if config:
                rollback_config = config.with_name(config.name + '.rollback-' + uuid4().hex)
                rollback_config.write_text(previous_configuration)
                durable(rollback_config); os.replace(rollback_config, config); durable(config.parent)
        if journal.exists(): journal.unlink(); durable(journal.parent)
        raise
    finally:
        for file in (stage, previous, config_stage):
            if file and file.exists() and not journal.exists(): file.unlink()
    return {"ok": True, "database": str(path), "configuration": str(backup / "original-config.json")}


def export_changes(path, backup, output):
    """Export a recovery bundle; exporting never grants permission to overwrite writes."""
    from .locking import DatabaseLock
    with DatabaseLock("sqlite:///" + str(path.absolute())):
        report = json.loads((backup / "report.json").read_text())
        if report["active_database"] != str(path.resolve()): raise ValueError("Backup belongs to a different database")
        data, before = read_database(path), read_database(backup / "converted.sqlite")
        guard = data.get("km_conversion_guard", [])
        if not guard or guard[0]["backup_id"] != report["backup_id"]: raise ValueError("Backup does not match active conversion")
        old = {row["id"]: row for row in before["km_documents"]}
        current = {row["id"]: row for row in data["km_documents"]}
        changed = {key for key, row in current.items() if row != old.get(key)}
        tables = {}
        for name in ("km_documents", "km_buckets", "km_chunks", "km_citation_snapshots", "km_ingest_jobs"):
            tables[name] = [row for row in data.get(name, []) if (row["id"] if name == "km_documents" else row.get("document_id")) in changed]
        bundle = {"protocol": "knowledge-recovery-export/v1", "backup_id": report["backup_id"],
                  "documents": sorted(changed), "removed_document_ids": sorted(set(old) - set(current)), "tables": tables,
                  "optional_tables": {name: rows for name, rows in data.items() if name not in s.metadata.tables and name != "km_conversion_guard"}}
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x") as stream: json.dump(bundle, stream, indent=2)
        return {"ok": True, "output": str(output.resolve()), "changed_documents": len(changed), "removed_documents": len(bundle["removed_document_ids"])}


def recover_interrupted(path):
    from .locking import DatabaseLock
    with DatabaseLock("sqlite:///" + str(path.absolute()), recovery=True):
        journal = path.with_name(path.name + ".km-conversion.json")
        if not journal.exists(): raise ValueError("No interrupted conversion")
        state = json.loads(journal.read_text())
        if state.get("protocol") != "knowledge-conversion-journal/v1" or state["active_database"] != str(path.resolve()):
            raise ValueError("Invalid conversion journal")
        if path.with_name(path.name + "-wal").exists(): raise ValueError("Checkpoint SQLite WAL before recovery")
        backup = Path(state["backup"])
        report = json.loads((backup / "report.json").read_text())
        if report["active_database"] != str(path.resolve()): raise ValueError("Recovery backup belongs to a different database")
        current = read_database(path)
        if current.get("km_conversion_guard") and current["km_conversion_guard"][0]["writes"]:
            raise ValueError("New writes exist; manual recovery merge required")
        stage = path.with_name(path.name + ".recovery-" + uuid4().hex)
        with sqlite3.connect((backup / "original.sqlite").resolve().as_uri() + "?mode=ro", uri=True) as source, sqlite3.connect(stage) as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok": raise ValueError("Backup validation failed")
        config = Path(state["active_configuration"]) if state["active_configuration"] else None
        config_stage = config.with_name(config.name + ".recovery-" + uuid4().hex) if config else None
        if config_stage: config_stage.write_text((backup / "original-config.json").read_text())
        durable(stage)
        if config_stage: durable(config_stage)
        os.replace(stage, path)
        durable(path.parent)
        if config_stage:
            os.replace(config_stage, config)
            durable(config.parent)
        journal.unlink()
        durable(journal.parent)
        previous = Path(state["previous_database"]) if state.get("previous_database") else None
        if previous and previous.parent == path.resolve().parent and previous.name.startswith(path.name + ".before-restore-") and previous.exists(): previous.unlink()
        return {"ok": True, "database": str(path.resolve()), "configuration": str(config) if config else None}
