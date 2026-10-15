import argparse
import json
import os
from pathlib import Path

import uvicorn

from .api import create_app
from .http_publication import HttpSuggestionPublisher
from .http_capabilities import HttpCapabilities, SpaceCapabilities, CapabilityBinding
from .identity import Principal, StaticBearerIdentity
from .configuration import Configuration
from .ports import ModelConfig, OpenAICompatibleModel
from .runtime import Runtime


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the independent knowledge service")
    parser.add_argument(
        "--database-url", default=os.getenv("KM_DATABASE_URL", "sqlite:///knowledge.sqlite")
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--read-only", action="store_true", default=os.getenv("KM_READ_ONLY") == "true")
    parser.add_argument("--identity-file", type=Path, default=None)
    parser.add_argument("--migration", choices=["preflight", "migrate", "compare"])
    parser.add_argument("--source", type=Path)
    parser.add_argument("--model-config", type=Path, default=None)
    parser.add_argument("--publisher-url", default=os.getenv("KM_PUBLISHER_URL"))
    parser.add_argument("--publisher-bearer", default=os.getenv("KM_PUBLISHER_BEARER"))
    parser.add_argument("--capabilities-url", default=os.getenv("KM_CAPABILITIES_URL"))
    parser.add_argument("--capabilities-bearer", default=os.getenv("KM_CAPABILITIES_BEARER", ""))
    parser.add_argument("--capabilities-file", type=Path, default=os.getenv("KM_CAPABILITIES_FILE"))
    parser.add_argument("--ui-dir", type=Path, default=os.getenv("KM_UI_DIRECTORY"))
    parser.add_argument("--config", type=Path, default=os.getenv("KM_CONFIG"))
    parser.add_argument("--conversion", choices=["preflight", "convert", "restore", "export", "recover"])
    parser.add_argument("--export-file", type=Path)
    parser.add_argument("--backup-root", type=Path)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--source-config", type=Path)
    args = parser.parse_args()
    if args.conversion:
        from sqlalchemy.engine import make_url
        from .conversion import preflight as conversion_preflight, convert, restore, export_changes, recover_interrupted
        url = make_url(args.database_url)
        if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
            parser.error("Offline conversion requires a file SQLite database")
        database = Path(url.database)
        if args.conversion in {"preflight", "convert"} and not args.config: parser.error("--config is required for the target configuration")
        if args.conversion == "convert" and not args.backup_root: parser.error("--backup-root is required")
        if args.conversion in {"restore", "export"} and not args.backup: parser.error("--backup is required")
        if args.conversion == "export" and not args.export_file: parser.error("--export-file is required")
        try:
            if args.conversion == "restore": report = restore(database, args.backup, args.source_config)
            elif args.conversion == "recover": report = recover_interrupted(database)
            elif args.conversion == "export": report = export_changes(database, args.backup, args.export_file)
            elif args.conversion == "preflight": report = conversion_preflight(database, Configuration.load(args.config))
            else: report = convert(database, Configuration.load(args.config), args.backup_root, args.source_config)
        except ValueError as error: report = {"ok": False, "errors": [str(error)]}
        print(json.dumps(report, indent=2))
        raise SystemExit(0 if report["ok"] else 1)
    if args.migration:
        from .migration import compare, migrate, preflight
        if args.source is None:
            parser.error("--source is required for migration commands")
        try:
            report = (
                preflight(args.source)
                if args.migration == "preflight"
                else migrate(args.source, args.database_url)
                if args.migration == "migrate"
                else compare(args.source, args.database_url)
            )
        except ValueError as exc:
            report = {"ok": False, "errors": [str(exc)]}
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        raise SystemExit(0 if report["ok"] else 1)
    identity = None
    configuration = Configuration.load(args.config) if args.config else existing_configuration(args.database_url)
    if args.identity_file:
        entries = json.loads(args.identity_file.read_text())
        identity = StaticBearerIdentity(
            {
                token: Principal(
                    id=value["id"],
                    tenant_id=value.get("tenant_id", "local"),
                    display_name=value.get("display_name", value["id"]),
                    spaces=frozenset(value.get("spaces", [])),
                    permissions=frozenset(value.get("permissions", ["read"])),
                )
                for token, value in entries.items()
            }
        )
    model_config = (
        ModelConfig(**json.loads(args.model_config.read_text())) if args.model_config else None
    )
    if args.capabilities_file and args.capabilities_url:
        parser.error("Use either --capabilities-file or --capabilities-url")
    capabilities = SpaceCapabilities([CapabilityBinding(**row) for row in json.loads(args.capabilities_file.read_text())]) if args.capabilities_file else HttpCapabilities(args.capabilities_url, args.capabilities_bearer) if args.capabilities_url else None
    uvicorn.run(
        create_app(
            Runtime(
                args.database_url,
                configuration=configuration,
                read_only=args.read_only,
                identity=identity,
                model_config=model_config,
                model=OpenAICompatibleModel() if model_config else None,
                capabilities=capabilities,
                publisher=HttpSuggestionPublisher(args.publisher_url, args.publisher_bearer)
                if args.publisher_url else None,
            ), ui_directory=args.ui_dir,
        ),
        host=args.host,
        port=args.port,
    )


def existing_configuration(database_url):
    """Fresh installs are minimal; existing complete databases retain their mode."""
    from sqlalchemy import create_engine, inspect, text
    engine = create_engine(database_url)
    try:
        tables = set(inspect(engine).get_table_names())
        if "knowledge_documents" in tables:
            return Configuration("full")
        if "km_schema_versions" in tables:
            with engine.connect() as db:
                modules = {row[0]: True for row in db.execute(text("SELECT module FROM km_schema_versions")) if row[0] != "core"}
            return Configuration(modules=modules)
        return Configuration()
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
