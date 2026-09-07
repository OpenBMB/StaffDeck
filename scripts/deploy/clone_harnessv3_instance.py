"""One-time, server-local copy into the explicitly requested isolated deployment."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path("/data/Staffdeck_Harnessv3")
SOURCE = Path("/data/StaffDeck/backend").resolve()
DATA = ROOT / "data"
SOURCE_DATA = Path("/home/heli/.local/share/StaffDeck")
destination = DATA / "staffdeck.db"
if destination.exists():
    raise SystemExit("Refusing to overwrite an existing instance database")

source = sqlite3.connect(f"file:{SOURCE / 'skill_agent_loop.db'}?mode=ro", uri=True)
copy = sqlite3.connect(destination)
source.backup(copy, pages=256)
assert copy.execute("pragma integrity_check").fetchone()[0] == "ok"
tables = {r[0] for r in copy.execute("select name from sqlite_master where type='table'")}
changes = {}


def update(table, assignment, where="1"):
    if table in tables:
        changes[table] = copy.execute(f'update "{table}" set {assignment} where {where}').rowcount


update("channel_bindings", "status='disabled', connected=0")
update("scheduled_tasks", "status='paused', lease_owner=NULL, lease_until=NULL, next_run_at=NULL")
for table in ("channel_deliveries", "webhook_deliveries", "api_jobs", "a2a_task_runs", "knowledge_ingest_jobs"):
    update(table, "status='cancelled'", "status in ('queued','pending','running','retrying','processing')")
update("webhook_endpoints", "status='disabled'")
for table in ("harness_turns", "harness_runs", "harness_task_frames"):
    update(table, "status='cancelled'", "status in ('queued','running','executing','planning','waiting_dependencies')")
update("chat_sessions", "status='active'", "status in ('running','executing')")
update("ui_configs", f"harness_storage_path='{DATA / 'harness_workspaces'}'")

# Relocate structured references to copied workspaces/attachments, never credentials or prose.
prefixes = [(str(SOURCE_DATA), str(DATA)), (str(SOURCE), str(ROOT / "backend"))]


def relocate(value):
    if isinstance(value, dict):
        return {k: relocate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [relocate(v) for v in value]
    if isinstance(value, str):
        for old, new in prefixes:
            if value == old or value.startswith(old + "/"):
                return new + value[len(old):]
    return value


relocated = 0
for table in tables:
    for column in copy.execute(f'pragma table_info("{table}")').fetchall():
        name, kind = column[1:3]
        if kind.upper() != "JSON":
            continue
        rows = copy.execute(f'SELECT rowid,"{name}" FROM "{table}" WHERE "{name}" LIKE ? OR "{name}" LIKE ?',
                            (f"%{SOURCE_DATA}%", f"%{SOURCE}%")).fetchall()
        for rowid, raw in rows:
            try:
                value = json.loads(raw)
            except (ValueError, TypeError):
                continue
            changed = relocate(value)
            if changed != value:
                copy.execute(f'UPDATE "{table}" SET "{name}"=? WHERE rowid=?', (json.dumps(changed, ensure_ascii=False), rowid))
                relocated += 1
copy.commit()
for name in ("harness_workspaces", "attachments"):
    if (SOURCE_DATA / name).exists():
        shutil.copytree(SOURCE_DATA / name, DATA / name)
for name in (".harness-workspaces", "uploads"):
    if (SOURCE / name).exists():
        shutil.copytree(SOURCE / name, ROOT / "backend" / name, dirs_exist_ok=True)

# Read the old application's encryption key only on the server; never print or transfer it.
os.chdir(SOURCE)
sys.path.insert(0, str(SOURCE))
from app.config import get_settings  # noqa: E402 - load the source instance after selecting its cwd
secret = get_settings().app_secret
settings = {
    "APP_SECRET": secret,
    "DATABASE_URL": f"sqlite:///{destination}",
    "ULTRARAG_DATA_DIR": str(DATA),
    "HARNESS_V3_ENABLED": "true",
    "HARNESS_V3_FALLBACK_TO_V2": "false",
    "HARNESS_V3_ROOT": str(ROOT / "engine/deepseek-harness-0.1.2-alpha.2"),
    "HARNESS_V3_HOME": str(DATA / "harness-home"),
    "HARNESS_V3_NODE_BIN": "/usr/local/bin/node",
    "TOOL_BASE_URL": "http://127.0.0.1:10186",
    "CORS_ORIGINS": "http://39.102.210.77:10086",
    "GENERAL_SKILL_RUNTIME_PYTHON": str(ROOT / ".venv/bin/python"),
    "PUBLIC_API_ENABLED": "true",
}
env_file = ROOT / "deploy/runtime.env"
env_file.write_text("\n".join(f"{k}={json.dumps(v)}" for k, v in settings.items()) + "\n")
env_file.chmod(0o640)
summary = {"source": str(SOURCE), "database": str(destination), "integrity": "ok", "disabled": changes,
           "relocated_json_rows": relocated, "counts": {t: copy.execute(f'select count(*) from "{t}"').fetchone()[0]
           for t in ("users", "agent_profiles", "skills", "general_skills", "knowledge_documents", "model_configs")}}
(ROOT / "deploy/clone-report.json").write_text(json.dumps(summary, indent=2))
print(json.dumps(summary))
