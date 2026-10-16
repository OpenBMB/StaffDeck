from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, MetaData, create_engine, inspect, select
from sqlalchemy.dialects.sqlite import insert

from .database import initialize_schema
from .models import KnowledgeModel

KNOWLEDGE_TABLES = (
    "knowledge_bases",
    "knowledge_base_versions",
    "knowledge_documents",
    "knowledge_buckets",
    "knowledge_chunks",
    "knowledge_concepts",
    "knowledge_discovery_suggestions",
    "knowledge_ingest_jobs",
)
SPACE_TABLES = (
    "knowledge_spaces",
    "knowledge_bindings",
    "knowledge_branches",
    "knowledge_publications",
    "knowledge_citation_snapshots",
    "knowledge_version_access",
)


def source_engine(path: Path):
    absolute = path.resolve()
    if not absolute.is_file():
        raise ValueError("Source SQLite file does not exist")
    return create_engine(f"sqlite:///file:{absolute}?mode=ro&uri=true")


def read_source(path: Path) -> dict[str, list[dict[str, Any]]]:
    engine = source_engine(path)
    try:
        metadata = MetaData()
        available = set(inspect(engine).get_table_names())
        names = set(KNOWLEDGE_TABLES + SPACE_TABLES) | {
            "agent_profiles",
            "agent_knowledge_branches",
            "agent_resource_bindings",
            "knowledge_publication_receipts",
        }
        metadata.reflect(engine, only=list(names & available))
        with engine.connect() as connection:
            return {
                name: [
                    dict(row)
                    for row in connection.execute(
                        select(metadata.tables[name])
                    ).mappings()
                ]
                for name in names & available
            }
    finally:
        engine.dispose()


def preflight(path: Path) -> dict[str, Any]:
    data = read_source(path)
    errors = []
    for table in KNOWLEDGE_TABLES:
        if table not in data:
            errors.append(f"Missing required source table: {table}")
    indexes = {
        table: {row["id"]: row for row in rows}
        for table, rows in data.items()
        if rows and "id" in rows[0]
    }
    relationships = {
        "knowledge_base_id": "knowledge_bases",
        "knowledge_base_version_id": "knowledge_base_versions",
        "document_id": "knowledge_documents",
        "bucket_id": "knowledge_buckets",
        "space_id": "knowledge_spaces",
        "suggestion_id": "knowledge_discovery_suggestions",
        "head_version_id": "knowledge_base_versions",
        "base_version_id": "knowledge_base_versions",
        "version_id": "knowledge_base_versions",
    }
    for table in KNOWLEDGE_TABLES + SPACE_TABLES:
        for row in data.get(table, []):
            if table == "knowledge_ingest_jobs" and row["status"] in {
                "queued",
                "running",
                "cancel_requested",
            }:
                errors.append(f"Unfinished task: {row['id']}")
            for field, target_table in relationships.items():
                value = row.get(field)
                if not value:
                    continue
                target = indexes.get(target_table, {}).get(value)
                if target is None:
                    errors.append(f"{table}/{row['id']}: missing {field}={value}")
                elif target["tenant_id"] != row["tenant_id"]:
                    errors.append(f"{table}/{row['id']}: cross-namespace {field}")
                elif field in {
                    "document_id",
                    "bucket_id",
                    "knowledge_base_version_id",
                } and (target.get("knowledge_base_id") != row.get("knowledge_base_id")):
                    errors.append(f"{table}/{row['id']}: mismatched {field} ownership")
                elif field in {"head_version_id", "base_version_id", "version_id"} and (
                    target.get("knowledge_base_id") != row.get("knowledge_base_id")
                ):
                    errors.append(f"{table}/{row['id']}: mismatched {field} ownership")
            if table in {
                "knowledge_buckets",
                "knowledge_chunks",
                "knowledge_concepts",
                "knowledge_discovery_suggestions",
            }:
                for field in ("document_id", "bucket_id"):
                    related = indexes.get(relationships[field], {}).get(row.get(field))
                    if related and related.get("knowledge_base_version_id") != row.get(
                        "knowledge_base_version_id"
                    ):
                        errors.append(
                            f"{table}/{row['id']}: mismatched {field} version"
                        )
    for row in data.get("agent_resource_bindings", []):
        if row["resource_type"] != "knowledge_base":
            continue
        if row["agent_id"] not in indexes.get("agent_profiles", {}):
            errors.append(f"Binding {row['id']}: missing employee")
        if row["resource_id"] not in indexes.get("knowledge_bases", {}):
            errors.append(f"Binding {row['id']}: missing knowledge base")
        for target_name, target_id in (
            ("agent_profiles", row["agent_id"]),
            ("knowledge_bases", row["resource_id"]),
        ):
            target = indexes.get(target_name, {}).get(target_id)
            if target and target["tenant_id"] != row["tenant_id"]:
                errors.append(f"Binding {row['id']}: cross-namespace {target_name}")
    for row in data.get("agent_knowledge_branches", []):
        for field, target_table in (
            ("agent_id", "agent_profiles"),
            ("knowledge_base_id", "knowledge_bases"),
        ):
            target = indexes.get(target_table, {}).get(row.get(field))
            if target and target["tenant_id"] != row["tenant_id"]:
                errors.append(f"Branch {row['id']}: cross-namespace {field}")
        if row["agent_id"] not in indexes.get("agent_profiles", {}):
            errors.append(f"Branch {row['id']}: missing employee")
        if not any(
            binding["resource_type"] == "knowledge_base"
            and binding["tenant_id"] == row["tenant_id"]
            and binding["agent_id"] == row["agent_id"]
            and binding["resource_id"] == row["knowledge_base_id"]
            for binding in data.get("agent_resource_bindings", [])
        ):
            errors.append(f"Branch {row['id']}: missing resource binding")
        versions = [
            v
            for v in data.get("knowledge_base_versions", [])
            if v["tenant_id"] == row["tenant_id"]
            and v["knowledge_base_id"] == row["knowledge_base_id"]
        ]
        for field in ("head_version", "base_version"):
            if not any(version["version"] == row[field] for version in versions):
                errors.append(f"Branch {row['id']}: missing {field}={row[field]}")
    for row in data.get("knowledge_publication_receipts", []):
        suggestion = indexes.get("knowledge_discovery_suggestions", {}).get(row["suggestion_id"])
        reference = row.get("target_reference_json") or {}
        if isinstance(reference, str):
            reference = json.loads(reference)
        if not suggestion or suggestion["tenant_id"] != row["tenant_id"]:
            errors.append(f"Publication {row['id']}: missing or cross-namespace suggestion")
        elif suggestion["status"] != "confirmed":
            errors.append(f"Publication {row['id']}: suggestion is not confirmed")
        if row["idempotency_key"] != f"km:{row['tenant_id']}:{row['suggestion_id']}":
            errors.append(f"Publication {row['id']}: incompatible idempotency key")
        if not reference:
            errors.append(f"Publication {row['id']}: missing target reference")
    if "knowledge_spaces" not in data:
        for row in data.get("knowledge_bases", []):
            metadata = row.get("metadata_json") or {}
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            owner = metadata.get("owner_agent_id")
            if owner and owner not in indexes.get("agent_profiles", {}):
                errors.append(f"Knowledge base {row['id']}: missing owner employee")
    return {
        "ok": not errors,
        "errors": errors,
        "counts": {name: len(data.get(name, [])) for name in KNOWLEDGE_TABLES},
    }


def decode_row(row, table):
    value = dict(row)
    for column in table.columns:
        item = value.get(column.name)
        if isinstance(column.type, JSON) and isinstance(item, str):
            value[column.name] = json.loads(item)
    return {key: item for key, item in value.items() if key in table.c}


def _migrate_legacy(source: Path, target_url: str) -> dict[str, Any]:
    report = preflight(source)
    if not report["ok"]:
        return report
    engine = create_engine(target_url)
    if engine.url.get_backend_name() != "sqlite":
        engine.dispose()
        raise ValueError("Migration currently supports SQLite only")
    target_path = engine.url.database
    if (
        not target_path
        or target_path == ":memory:"
        or Path(target_path).resolve() == source.resolve()
    ):
        engine.dispose()
        raise ValueError("Target must be a different persistent database")
    data = read_source(source)
    mapping = []
    try:
        initialize_schema(engine)
        with engine.begin() as connection:

            def put(table_name, values):
                table = KnowledgeModel.metadata.tables[table_name]
                value = decode_row(values, table)
                if table_name == "knowledge_bases" and "knowledge_spaces" not in data:
                    metadata = value.get("metadata_json") or {}
                    if not metadata.get("owner_agent_id"):
                        published = next(
                            (
                                v
                                for v in data["knowledge_base_versions"]
                                if v["tenant_id"] == value["tenant_id"]
                                and v["knowledge_base_id"] == value["id"]
                                and v["version"]
                                == metadata.get("current_version", "1.0.0")
                            ),
                            None,
                        )
                        if published is None:
                            raise ValueError(
                                f"Knowledge base {value['id']}: missing published version"
                            )
                        value["metadata_json"] = {
                            **metadata,
                            "published_version_id": published["id"],
                        }
                existing = (
                    connection.execute(select(table).where(table.c.id == value["id"]))
                    .mappings()
                    .first()
                )
                if existing is not None:
                    if any(existing[key] != item for key, item in value.items()):
                        raise ValueError(f"Target conflict: {table_name}/{value['id']}")
                    return
                connection.execute(insert(table).values(**value))

            for table in KNOWLEDGE_TABLES + SPACE_TABLES:
                for row in data.get(table, []):
                    put(table, row)
            for receipt in data.get("knowledge_publication_receipts", []):
                put("knowledge_publications", {
                    "id": receipt["id"], "tenant_id": receipt["tenant_id"],
                    "suggestion_id": receipt["suggestion_id"],
                    "idempotency_key": receipt["idempotency_key"],
                    "status": "confirmed",
                    "target_reference": receipt["target_reference_json"],
                    "updated_at": receipt["created_at"],
                })
            if "knowledge_spaces" in data:
                report["space_mapping"] = []
                report["target_url"] = target_url
                return report
            for employee in data.get("agent_profiles", []):
                tenant, employee_id = employee["tenant_id"], employee["id"]
                space_id = f"sd:{tenant}:{employee_id}"
                mapping.append(
                    {
                        "tenant_id": tenant,
                        "agent_id": employee_id,
                        "space_id": space_id,
                        "is_overall": bool(employee.get("is_overall")),
                    }
                )
                put(
                    "knowledge_spaces",
                    {
                        "id": space_id,
                        "tenant_id": tenant,
                        "name": employee.get("name") or employee_id,
                        "owner_id": employee.get("created_by_user_id") or "legacy",
                        "owner_name": employee.get("name") or employee_id,
                        "status": "active",
                        "created_at": employee["created_at"],
                        "updated_at": employee["updated_at"],
                    },
                )
            space_map = {
                (row["tenant_id"], row["agent_id"]): row["space_id"] for row in mapping
            }
            versions = data["knowledge_base_versions"]
            for kb in data["knowledge_bases"]:
                tenant, kb_id = kb["tenant_id"], kb["id"]
                metadata = kb.get("metadata_json") or {}
                if isinstance(metadata, str):
                    metadata = json.loads(metadata)
                candidates = [
                    v
                    for v in versions
                    if v["tenant_id"] == tenant and v["knowledge_base_id"] == kb_id
                ]
                if not candidates:
                    raise ValueError(f"Knowledge base {kb_id}: no version")
                published_name = metadata.get("current_version", "1.0.0")
                published = next(
                    (v for v in candidates if v["version"] == published_name), None
                )
                private = metadata.get("owner_agent_id")
                if not private and published is None:
                    raise ValueError(
                        f"Knowledge base {kb_id}: missing published version={published_name}"
                    )
                # Public bases may be visible without an explicit employee binding.
                overall = next(
                    (
                        row
                        for row in mapping
                        if row["tenant_id"] == tenant and row["is_overall"]
                    ),
                    None,
                )
                space_id = (
                    space_map[(tenant, private)]
                    if private
                    else (overall["space_id"] if overall else f"sd:{tenant}:gallery")
                )
                if not private and overall is None:
                    put(
                        "knowledge_spaces",
                        {
                            "id": space_id,
                            "tenant_id": tenant,
                            "name": "Gallery",
                            "owner_id": "legacy",
                            "owner_name": "Legacy",
                            "status": "active",
                            "created_at": kb["created_at"],
                            "updated_at": kb["updated_at"],
                        },
                    )
                key = (
                    tenant,
                    private if private else (overall or {}).get("agent_id"),
                    kb_id,
                )
                existing_binding = next(
                    (
                        r
                        for r in data.get("agent_resource_bindings", [])
                        if r["resource_type"] == "knowledge_base"
                        and (r["tenant_id"], r["agent_id"], r["resource_id"]) == key
                    ),
                    None,
                )
                if existing_binding:
                    continue
                head = published or max(candidates, key=lambda row: row["created_at"])
                binding_id = f"legacy:{space_id}:{kb_id}"
                put(
                    "knowledge_bindings",
                    {
                        "id": binding_id,
                        "tenant_id": tenant,
                        "space_id": space_id,
                        "knowledge_base_id": kb_id,
                        "hidden": False,
                        "created_at": kb["created_at"],
                        "updated_at": kb["updated_at"],
                    },
                )
                put(
                    "knowledge_branches",
                    {
                        "id": f"branch:{binding_id}",
                        "tenant_id": tenant,
                        "space_id": space_id,
                        "knowledge_base_id": kb_id,
                        "base_version_id": head["id"],
                        "head_version_id": head["id"],
                        "status": "active",
                        "sync_state": "synced",
                        "created_at": kb["created_at"],
                        "updated_at": kb["updated_at"],
                    },
                )
                put(
                    "knowledge_version_access",
                    {
                        "id": f"{space_id}:{head['id']}",
                        "tenant_id": tenant,
                        "space_id": space_id,
                        "knowledge_base_id": kb_id,
                        "version_id": head["id"],
                    },
                )
                for version in candidates:
                    version_metadata = version.get("metadata_json") or {}
                    if isinstance(version_metadata, str):
                        version_metadata = json.loads(version_metadata)
                    version_owner = version_metadata.get("owner_agent_id")
                    if version_owner == private or (not private and not version_owner):
                        put(
                            "knowledge_version_access",
                            {
                                "id": f"{space_id}:{version['id']}",
                                "tenant_id": tenant,
                                "space_id": space_id,
                                "knowledge_base_id": kb_id,
                                "version_id": version["id"],
                            },
                        )
            branches = {
                (row["tenant_id"], row["agent_id"], row["knowledge_base_id"]): row
                for row in data.get("agent_knowledge_branches", [])
            }
            for resource in data.get("agent_resource_bindings", []):
                if resource["resource_type"] != "knowledge_base":
                    continue
                tenant, agent, kb_id = (
                    resource["tenant_id"],
                    resource["agent_id"],
                    resource["resource_id"],
                )
                space_id = space_map[(tenant, agent)]
                put(
                    "knowledge_bindings",
                    {
                        "id": resource["id"],
                        "tenant_id": tenant,
                        "space_id": space_id,
                        "knowledge_base_id": kb_id,
                        "hidden": resource.get("status") == "deleted",
                        "metadata_json": resource.get("metadata_json") or {},
                        "created_at": resource["created_at"],
                        "updated_at": resource["updated_at"],
                    },
                )
                branch = branches.get((tenant, agent, kb_id))
                candidates = [
                    row
                    for row in versions
                    if row["tenant_id"] == tenant and row["knowledge_base_id"] == kb_id
                ]
                if not candidates:
                    raise ValueError(f"Binding {resource['id']}: no version")
                kb = next(row for row in data["knowledge_bases"] if row["id"] == kb_id)
                metadata = kb.get("metadata_json") or {}
                if isinstance(metadata, str):
                    metadata = json.loads(metadata)
                head_name = branch["head_version"] if branch else metadata.get("current_version", "1.0.0")
                head = next((v for v in candidates if v["version"] == head_name), None)
                if head is None:
                    raise ValueError(f"Binding {resource['id']}: missing current version={head_name}")
                base = next(
                    (
                        v
                        for v in candidates
                        if branch and v["version"] == branch["base_version"]
                    ),
                    head,
                )
                put(
                    "knowledge_branches",
                    {
                        "id": branch["id"] if branch else f"branch:{resource['id']}",
                        "tenant_id": tenant,
                        "space_id": space_id,
                        "knowledge_base_id": kb_id,
                        "base_version_id": base["id"],
                        "head_version_id": head["id"],
                        "status": resource.get("status", "active")
                        if resource.get("status") != "active"
                        else (branch or {}).get("status", "active"),
                        "sync_state": (branch or {}).get("sync_state", "synced"),
                        "created_at": (branch or resource)["created_at"],
                        "updated_at": (branch or resource)["updated_at"],
                    },
                )
                for version in {head["id"], base["id"]}:
                    put(
                        "knowledge_version_access",
                        {
                            "id": f"{space_id}:{version}",
                            "tenant_id": tenant,
                            "space_id": space_id,
                            "knowledge_base_id": kb_id,
                            "version_id": version,
                        },
                    )
                for version in candidates:
                    version_metadata = version.get("metadata_json") or {}
                    if isinstance(version_metadata, str):
                        version_metadata = json.loads(version_metadata)
                    if version_metadata.get("owner_agent_id") == agent:
                        put(
                            "knowledge_version_access",
                            {
                                "id": f"{space_id}:{version['id']}",
                                "tenant_id": tenant,
                                "space_id": space_id,
                                "knowledge_base_id": kb_id,
                                "version_id": version["id"],
                            },
                        )
        report["space_mapping"] = mapping
        report["target_url"] = target_url
        return report
    finally:
        engine.dispose()


def _compare_legacy(source: Path, target_url: str) -> dict[str, Any]:
    data = read_source(source)
    target = create_engine(target_url)
    errors = []
    try:
        with target.connect() as connection:
            for name in KNOWLEDGE_TABLES + tuple(
                name for name in SPACE_TABLES if name in data
            ):
                table = KnowledgeModel.metadata.tables[name]
                actual = {
                    row["id"]: dict(row)
                    for row in connection.execute(select(table)).mappings()
                }
                expected_ids = {row["id"] for row in data.get(name, [])}
                if set(actual) != expected_ids:
                    errors.append(f"Resource IDs differ: {name}")
                for row in data.get(name, []):
                    expected = decode_row(row, table)
                    if name == "knowledge_bases" and "knowledge_spaces" not in data:
                        metadata = expected.get("metadata_json") or {}
                        actual_metadata = (
                            actual.get(row["id"], {}).get("metadata_json") or {}
                        )
                        if "published_version_id" in actual_metadata:
                            expected["metadata_json"] = {
                                **metadata,
                                "published_version_id": actual_metadata[
                                    "published_version_id"
                                ],
                            }
                    if row["id"] in actual and any(
                        actual[row["id"]][key] != value
                        for key, value in expected.items()
                    ):
                        errors.append(f"Resource differs: {name}/{row['id']}")
        return {"ok": not errors, "errors": errors}
    finally:
        target.dispose()


def _build_current(source, path):
    from .configuration import Configuration
    from .conversion import convert
    report = _migrate_legacy(source, "sqlite:///" + str(path))
    if not report["ok"]:
        return report
    converted = convert(path, Configuration("full"), path.parent / "conversion-backups")
    if not converted["ok"]:
        return converted
    return {**report, "module_counts": converted["counts"], "document_mapping": converted["document_mapping"]}


def _target_path(source, target_url):
    from sqlalchemy.engine import make_url
    url = make_url(target_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise ValueError("Migration requires a persistent SQLite target")
    target = Path(url.database).resolve()
    if target == source.resolve():
        raise ValueError("Target must differ from the read-only source")
    return target


def _differences(expected, target):
    from .conversion import read_database
    left, right = read_database(expected), read_database(target)
    left.pop("km_conversion_guard", None)
    right.pop("km_conversion_guard", None)
    errors = []
    if set(left) != set(right):
        errors.append("Module tables differ")
    for name in sorted(set(left) & set(right)):
        canonical = lambda rows: sorted(json.dumps(row, sort_keys=True, default=str) for row in rows)
        if canonical(left[name]) != canonical(right[name]):
            errors.append("Resources differ: " + name)
    return errors


def migrate(source: Path, target_url: str) -> dict[str, Any]:
    """Stage historical mappings, then install the current module-owned schema."""
    from .locking import DatabaseLock
    report = preflight(source)
    if not report["ok"]:
        return report
    target = _target_path(source, target_url)
    target.parent.mkdir(parents=True, exist_ok=True)
    with DatabaseLock(target_url), tempfile.TemporaryDirectory(prefix="km-migration-", dir=target.parent) as folder:
        stage = Path(folder) / "knowledge.sqlite"
        report = _build_current(source, stage)
        if not report["ok"]:
            return report
        if target.exists():
            errors = _differences(stage, target)
            if errors:
                return {**report, "ok": False, "errors": ["Target conflict: " + error for error in errors]}
        else:
            # The source and existing target are untouched until staging succeeds.
            os.replace(stage, target)
        return {**report, "target_url": target_url, "schema": "module-owned"}


def compare(source: Path, target_url: str) -> dict[str, Any]:
    report = preflight(source)
    if not report["ok"]:
        return report
    target = _target_path(source, target_url)
    if not target.is_file():
        return {"ok": False, "errors": ["Target SQLite file does not exist"]}
    with tempfile.TemporaryDirectory(prefix="km-compare-") as folder:
        expected = Path(folder) / "knowledge.sqlite"
        report = _build_current(source, expected)
        if not report["ok"]:
            return report
        errors = _differences(expected, target)
        return {"ok": not errors, "errors": errors, "schema": "module-owned"}
