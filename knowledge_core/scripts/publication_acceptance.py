"""Exercise current discovery/publication modules without a host or legacy ORM."""
import argparse
import json
from pathlib import Path
import tempfile

from fastapi.testclient import TestClient
from sqlalchemy import select
from knowledge_core.api import create_app
from knowledge_core.configuration import Configuration
from knowledge_core.kernel import storage as s
from knowledge_core.runtime import Runtime


class Publisher:
    def __init__(self):
        self.references = {}
        self.fail_once = False

    def publish(self, suggestion, idempotency_key):
        self.references.setdefault(idempotency_key, {"target_id": "external-" + str(len(self.references) + 1)})
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("external success, response lost")
        return self.references[idempotency_key]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    publisher = Publisher()
    checks = []
    with tempfile.TemporaryDirectory(prefix="km-publication-") as folder:
        runtime = Runtime("sqlite:///" + folder + "/knowledge.sqlite", publisher=publisher,
            configuration=Configuration(modules={"discovery": True, "publication": True}))
        with TestClient(create_app(runtime)) as client:
            discovery = runtime.plugins["discovery"]
            with runtime.engine.begin() as db:
                for identifier, status in (("review", "pending"), ("reject", "pending"), ("retry", "reviewed"), ("historical", "confirmed")):
                    db.execute(discovery.suggestions.insert().values(id=identifier, document_id="", title=identifier,
                        suggestion_type="procedure", status=status, payload={"steps": ["Work"]}, source_refs=[], created_at=s.now(), updated_at=s.now()))
                    db.execute(discovery.contexts.insert().values(suggestion_id=identifier, context={"tenant_id": "local"}))
            reviewed = client.post("/api/km/v1/suggestions/review/review")
            require(reviewed.status_code == 200 and reviewed.json()["status"] == "reviewed" and not publisher.references, "Local review failed")
            checks.append("review remains local")
            first = client.post("/api/km/v1/suggestions/review/publish")
            require(first.status_code == 200 and client.post("/api/km/v1/suggestions/review/publish").json() == first.json() and len(publisher.references) == 1, "Duplicate external publication")
            checks.append("duplicate publication returns the same target")
            require(client.post("/api/km/v1/suggestions/reject/reject").json()["status"] == "rejected" and client.post("/api/km/v1/suggestions/reject/publish").status_code == 409, "Rejected suggestion published")
            checks.append("rejected suggestion cannot publish")
            publisher.fail_once = True
            require(client.post("/api/km/v1/suggestions/retry/publish").status_code == 502, "Expected external failure")
            with runtime.engine.connect() as db:
                receipt = db.execute(select(runtime.plugins["publication"].receipts).where(runtime.plugins["publication"].receipts.c.suggestion_id == "retry")).mappings().one()
                require(receipt["status"] == "retryable", "Recoverable state missing")
            require(client.post("/api/km/v1/suggestions/retry/publish").status_code == 200 and len(publisher.references) == 2, "Retry duplicated external resource")
            checks.append("external success and lost response retries idempotently")
            require(client.post("/api/km/v1/suggestions/historical/publish").status_code == 409 and len(publisher.references) == 2, "Historical publication repeated")
            checks.append("historical unknown target is never republished")
    result = {"status": "passed", "checks": checks}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
