"""Schema ownership is checked before any DDL or task recovery."""
from sqlalchemy import inspect, select, text
from .storage import metadata, schema


def initialize(runtime):
    engine = runtime.engine
    if engine.url.get_backend_name() != "sqlite":
        raise ValueError("Knowledge Core currently supports SQLite only")
    desired = {"core": 1, **{name: plugin.schema_version for name, plugin in runtime.plugins.items()}}
    tables = set(inspect(engine).get_table_names())
    current = {}
    if tables and "km_schema_versions" not in tables:
        raise ValueError("Existing complete KM database requires an explicit offline conversion; it cannot start as minimal")
    if "km_schema_versions" in tables:
        with engine.connect() as db:
            current = dict(db.execute(select(schema.c.module, schema.c.version)).all())
        removed = set(current) - set(desired)
        if removed:
            raise ValueError("Disabled stored modules require offline conversion: " + ", ".join(sorted(removed)))
        if any(current[name] != desired[name] for name in current):
            raise ValueError("Unsupported module schema version; run an explicit upgrade")
    allowed = set(metadata.tables)
    for plugin in runtime.plugins.values(): allowed.update(plugin.metadata.tables)
    unexpected = tables - allowed - {"km_conversion_guard"}
    if unexpected:
        raise ValueError("Unexpected stored tables require offline conversion: " + ", ".join(sorted(unexpected)))
    if runtime.read_only:
        if tables - {"km_conversion_guard"} != allowed or current != desired:
            raise ValueError("Initialize the configured schemas before freezing writes")
        return
    with engine.begin() as db:
        metadata.create_all(db)
        for plugin in runtime.plugins.values(): plugin.metadata.create_all(db)
        for name, version in desired.items():
            if name not in current:
                db.execute(schema.insert().values(module=name, version=version))
        if "km_conversion_guard" in tables:
            install_write_guard(db, allowed)


def install_write_guard(db, tables):
    for table in tables:
        quoted = table.replace('"', '""')
        for action in ("INSERT", "UPDATE", "DELETE"):
            trigger = f"guard_{quoted}_{action}"
            db.execute(text(f'CREATE TRIGGER IF NOT EXISTS "{trigger}" AFTER {action} ON "{quoted}" '
                            'BEGIN UPDATE km_conversion_guard SET writes=writes+1 WHERE id=1; END'))
