"""Check removed ownership paths and initialize an isolated host schema."""
import argparse
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pilotdeck-root", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    violations = []
    removed = ["backend/app/knowledge/service.py", "backend/app/knowledge/parser.py", "backend/app/knowledge/okf.py"]
    for path in removed:
        if (root / path).exists(): violations.append(f"Native implementation exists: {path}")
    domain_names = {"KnowledgeBase", "KnowledgeBaseVersion", "KnowledgeDocument", "KnowledgeBucket", "KnowledgeChunk", "KnowledgeConcept", "KnowledgeIngestJob", "KnowledgeDiscoverySuggestion", "AgentKnowledgeBranch"}
    checked = []
    for folder in (root / "backend/app", root / "backend/src", root / "knowledge_core/src/knowledge_core"):
        for path in folder.rglob("*.py"):
            tree = ast.parse(path.read_text())
            checked.append(str(path.relative_to(root)))
            independent = path.is_relative_to(root / "knowledge_core/src")
            for node in ast.walk(tree):
                imports = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                for module in imports:
                    if independent and module.split(".")[0] in {"app", "staffdeck_harness", "harness"}:
                        violations.append(f"Host import: {path.relative_to(root)}:{node.lineno} {module}")
                    if module in {"app.knowledge.service", "app.knowledge.parser", "app.knowledge.okf"}:
                        violations.append(f"Native import: {path.relative_to(root)}:{node.lineno} {module}")
                if isinstance(node, ast.ImportFrom) and node.module == "app.db.models" and any(alias.name in domain_names for alias in node.names):
                    violations.append(f"Host knowledge ORM import: {path.relative_to(root)}:{node.lineno}")
    plugin = args.pilotdeck_root / "ui/src/composition/modules"
    for name in ("knowledge-core.tsx", "KnowledgeFrame.tsx"):
        source = (plugin / name).read_text()
        if any(token in source for token in ("./staffdeck/", "vendor/", "SkillsPageHost", "StaffDeckLocale")):
            violations.append(f"Knowledge vendor dependency: {name}")
    for name in ("staffdeck-knowledge.tsx", "staffdeck/vendor/KnowledgePage.tsx", "staffdeck/vendor/KnowledgePageHost.tsx"):
        if (plugin / name).exists(): violations.append(f"Retired frontend exists: {name}")
    with tempfile.TemporaryDirectory(prefix="km-host-schema-") as temporary:
        env = {**os.environ, "DATABASE_URL": "sqlite:///" + str(Path(temporary) / "host.sqlite"),
               "PYTHONPATH": f"{root / 'backend'}:{root / 'backend/src'}:{root / 'knowledge_core/src'}",
               "ULTRARAG_DOTENV": "/nonexistent-km-acceptance.env", "STARTUP_ORPHAN_CLEANUP_ENABLED": "false"}
        command = [sys.executable, "-c", "import json; from app.db.database import init_db,engine; from sqlalchemy import inspect; init_db(); print('KM_SCHEMA '+json.dumps(inspect(engine).get_table_names()))"]
        result = subprocess.run(command, env=env, cwd=temporary, capture_output=True, text=True)
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    (args.evidence_dir / "cleanup-schema.log").write_text(result.stdout + result.stderr)
    schema = next((json.loads(line.removeprefix("KM_SCHEMA ")) for line in result.stdout.splitlines() if line.startswith("KM_SCHEMA ")), [])
    if result.returncode or not schema: violations.append("Host schema initialization failed")
    forbidden = [name for name in schema if (name.startswith("knowledge_") and name != "knowledge_publication_receipts") or name == "agent_knowledge_branches"]
    violations.extend(f"Host knowledge storage table: {name}" for name in forbidden)
    report = {"status": "passed" if not violations else "not_accepted", "violations": violations,
              "removed_implementations": removed, "checked_python_files": checked, "host_tables": schema,
              "allowed_host_business_receipts": ["knowledge_publication_receipts"], "schema_command": command}
    (args.evidence_dir / "cleanup.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "violations": violations, "host_tables": len(schema)}))
    return 0 if not violations else 1


if __name__ == "__main__":
    raise SystemExit(main())
