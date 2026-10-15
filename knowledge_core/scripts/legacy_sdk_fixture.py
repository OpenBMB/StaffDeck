"""Replay the historical Python SDK knowledge surface and save response shapes.

The source checkout is supplied explicitly so this cannot silently record the
current facade as historical evidence.
"""

from __future__ import annotations

import base64
import json
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import httpx
import pytest


HISTORICAL_COMMIT = "e39ec532"


def stable(value):
    if isinstance(value, dict):
        return {key: stable(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [stable(item) for item in value]
    if isinstance(value, str):
        if re.fullmatch(r"(?:space|kb|kbver|kdoc|kbucket|kchunk|job|binding|branch|suggestion|publication|agentres|sop|draft|version)_[0-9a-f]+", value):
            return "<id>"
        try:
            datetime.fromisoformat(value)
            return "<timestamp>"
        except ValueError:
            return value
    return value


def main(source_root: str, output_path: str) -> int:
    source = Path(source_root).resolve()
    if not (source / ".git").exists():
        raise SystemExit(f"historical source checkout is required: {source}")
    if HISTORICAL_COMMIT not in __import__("subprocess").check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ):
        raise SystemExit(f"source checkout is not pinned to {HISTORICAL_COMMIT}")

    sys.path.insert(0, str(source))
    sys.path.insert(0, str(source / "backend"))
    sys.path.insert(0, str(source / "sdk/python/src"))
    from backend.tests.test_public_api_v1 import _client, _tenant_key
    from staffdeck import APIError, StaffDeck
    from app.public_api import jobs
    from app.api import knowledge
    from app.knowledge import service

    patch = pytest.MonkeyPatch()
    patch.setenv("ULTRARAG_DOTENV", str(source / ".missing-dotenv"))
    patch.setenv("DATABASE_URL", "sqlite://")
    patch.setenv("APP_SECRET", "legacy-sdk-fixture")
    fixture_db = Path(tempfile.mkstemp(prefix="km-legacy-sdk-", suffix=".sqlite")[1])
    server, engine, token = _client(patch, database_url=f"sqlite:///{fixture_db}")
    key = _tenant_key(server, token, ["*"])
    jobs.engine = engine
    service.engine = engine
    patch.setattr(knowledge, "enqueue_async_job", lambda kind, fn, *args, **kwargs: fn(*args))
    records = []

    def transport(request):
        path = request.url.raw_path.decode().removeprefix("/api/v1")
        response = server.request(request.method, path, content=request.content, headers=dict(request.headers))
        records.append({
            "method": request.method,
            "path": path,
            "status": response.status_code,
            "body": stable(response.json() if response.content else None),
        })
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)

    with server, StaffDeck(
        base_url="http://testserver/api/v1", api_key=key,
        transport=httpx.MockTransport(transport),
    ) as sdk:
        kb = sdk.knowledge_bases.create("agent_api", {"name": "Fixture Knowledge"}).data["id"]
        sdk.knowledge_bases.list("agent_api")
        sdk.knowledge_bases.update("agent_api", kb, {"description": "Fixture updated"})
        receipt = sdk.knowledge_bases.upsert_entries(
            "agent_api", kb, {"entries": [{"title": "Policy", "content": "# Policy\nKeep receipts."}]},
            idempotency_key="legacy-fixture-entry",
        )
        jobs.run_job(receipt.data["id"])
        result = sdk.jobs.wait(receipt.data["id"]).data
        document_id = result["result"]["documents"][0]["document_id"]
        sdk.knowledge_bases.documents("agent_api", kb)
        document = sdk.knowledge_bases.documents("agent_api", kb).data["data"][0]
        sdk.knowledge_bases.update_document(
            "agent_api", kb, document_id,
            {"content_md": "# Policy\nKeep original receipts.", "expected_updated_at": document["updated_at"]},
        )
        sdk.knowledge_bases.search("agent_api", kb, {"query": "receipts"})
        source_file = source / "sdk/python/legacy-fixture.md"
        source_file.write_text("# Leave\nAnnual leave is paid.\n", encoding="utf-8")
        upload = sdk.knowledge_bases.upload_document("agent_api", kb, source_file, title="Leave")
        jobs.run_job(upload.data["id"])
        sdk.jobs.wait(upload.data["id"])
        try:
            sdk.knowledge_bases.archive_document("agent_api", kb, document_id)
        except APIError:
            # Preserve the historical response body/status; the old service
            # legitimately rejects this cross-version document path.
            pass
        sdk.knowledge_bases.versions("agent_api", kb)
        sdk.knowledge_bases.concepts("agent_api", kb)
        sdk.knowledge_bases.rollback("agent_api", kb, "1.0.0")
        sdk.knowledge_bases.archive("agent_api", kb)

    source_file.unlink(missing_ok=True)
    engine.dispose()
    fixture_db.unlink(missing_ok=True)
    patch.undo()
    expected_methods = {
        "list", "create", "update", "archive", "search", "upsert_entries",
        "upload_document", "documents", "update_document", "archive_document",
        "versions", "rollback", "concepts",
    }
    observed = {item["path"].split("/agents/agent_api/knowledge-bases", 1)[-1].split("?", 1)[0] for item in records}
    if len(records) < len(expected_methods):
        raise SystemExit(f"historical replay recorded too few responses: {len(records)}")
    report = {
        "source_commit": HISTORICAL_COMMIT,
        "source_checkout": str(source),
        "method_count": len(expected_methods),
        "responses": records,
        "wire_paths_observed": sorted(observed),
    }
    Path(output_path).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"source_commit": HISTORICAL_COMMIT, "responses": len(records), "output": output_path}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
