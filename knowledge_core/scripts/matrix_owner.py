"""Independent HTTP owner with explicit deterministic acceptance ports."""

import argparse
import base64
from itertools import count

import uvicorn
from knowledge_core.api import create_app
from knowledge_core.identity import Principal, StaticBearerIdentity
from knowledge_core.parser import extract_text, KnowledgeParseError
from knowledge_core.kernel.runtime import KernelRuntime
from knowledge_core.kernel import storage as storage
from knowledge_core.configuration import Configuration
from knowledge_core.assembly import assemble
from sqlalchemy import select


class Parser:
    def parse(self, filename, content):
        if filename == "capability-fail.html":
            raise KnowledgeParseError("ACCEPTANCE_PARSER_PORT_UNAVAILABLE")
        return extract_text(filename, content)


class Publisher:
    def publish(self, suggestion, idempotency_key):
        return {"status": "confirmed"}


class Owner(KernelRuntime):
    def initialize(self):
        super().initialize()
        from replay_storage import stabilize_indexes
        stabilize_indexes(self.engine)

        original = self.tasks.run
        def run(job_id, principal):
            with self.engine.connect() as db:
                filename = db.execute(select(storage.jobs.c.filename).where(storage.jobs.c.id == job_id)).scalar()
            if filename != 'cancel.md': original(job_id, principal)
        self.tasks.run = run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    sequence = count(1)
    def identifier(prefix):
        return f"{prefix}_{next(sequence):032x}"
    storage.identifier = identifier
    actor = Principal("host", "tenant_api", spaces=frozenset({"*"}),
                      permissions=frozenset({"read", "write", "history:write"}))
    uvicorn.run(create_app(Owner(args.database_url, configuration=Configuration('full', {'authentication': True}), plugin_factory=assemble,
                                identity=StaticBearerIdentity({"contract-owner": actor}),
                                parser=Parser(), publisher=Publisher())), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
