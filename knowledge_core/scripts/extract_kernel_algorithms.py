"""One-time mechanical extraction of host-independent content algorithms."""
import ast
from pathlib import Path

root = Path(__file__).resolve().parents[1] / "src/knowledge_core"
source = (root / "service.py").read_text()
tree = ast.parse(source)
names = {"bucket_read", "chunk_read", "_default_knowledge_base_version_id"}
parts = []
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.Assign, ast.AnnAssign)):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            continue
        if isinstance(node, ast.Assign) and any(isinstance(n, ast.Name) and n.id in {"logger", "PROMPT_DIR", "BUCKET_PROMPT", "DISCOVERY_PROMPT", "SEARCH_PROMPT", "DOCUMENT_ROUTE_PROMPT"} for n in node.targets):
            continue
        parts.append(ast.get_source_segment(source, node))
header = "from __future__ import annotations\nimport json\nimport re\nfrom datetime import timedelta\nfrom typing import Any\nfrom ..citations import CITATION_EXCERPT_CHAR_LIMIT\n\n"
(root / "kernel/algorithms.py").write_text(header + "\n\n".join(parts) + "\n")

source = (root / "okf.py").read_text()
excluded = {"upsert_concepts", "search_concepts", "export_okf_bundle", "create_concept_evidence_rows", "persist_lint_issues", "lint_okf_concepts", "_lint_issue"}
parts = [("@dataclass\n" if isinstance(node, ast.ClassDef) and node.decorator_list else "") + ast.get_source_segment(source, node) for node in ast.parse(source).body
         if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))
         and not (isinstance(node, ast.FunctionDef) and node.name in excluded)]
header = "from __future__ import annotations\nimport json\nimport re\nfrom dataclasses import dataclass\nfrom io import BytesIO\nfrom pathlib import Path\nfrom typing import Any\nfrom zipfile import ZipFile\nfrom ..citations import CITATION_EXCERPT_CHAR_LIMIT\n\n"
lint = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "lint_okf_concepts")
parts.append("def lint_okf_concepts(concepts):\n" + "\n".join("    " + ast.unparse(node).replace("\n", "\n    ") for node in lint.body[1:]))
parts.append('def _lint_issue(row, issue_type, message):\n    return {"issue_type": issue_type, "title": row.title, "message": message, "concept_id": row.concept_id, "concept_type": row.concept_type, "document_id": row.document_id}')
(root / "modules/okf_rules.py").write_text(header + "\n\n".join(parts) + "\n")
