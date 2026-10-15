"""Identical SQLite index creation for isolated historical/current replays."""

def stabilize_indexes(engine):
    with engine.begin() as connection:
        rows = connection.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL ORDER BY name"
        ).all()
        for name, _ in rows:
            connection.exec_driver_sql('DROP INDEX "' + name.replace('"', '""') + '"')
        for _, statement in rows:
            connection.exec_driver_sql(statement)
