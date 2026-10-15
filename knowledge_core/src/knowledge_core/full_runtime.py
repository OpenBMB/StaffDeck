from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

from .database import SCHEMA_VERSION, initialize_schema
from .identity import (
    AuthorizationProvider,
    IdentityProvider,
    LocalIdentity,
    SpaceAuthorization,
)
from .models import KnowledgeIngestJob, utc_now
from .ports import KnowledgeModelPort, KnowledgeParserPort, ModelConfig
from .publication import SuggestionPublisher
from .service import KnowledgeService
from .telemetry import SpanSink


class Executor(Protocol):
    def submit(self, fn, *args): ...
    def shutdown(self, wait: bool = True): ...


class Runtime:
    def __init__(
        self,
        database_url: str,
        *,
        identity: IdentityProvider | None = None,
        authorization: AuthorizationProvider | None = None,
        model: KnowledgeModelPort | None = None,
        model_config: ModelConfig | None = None,
        parser: KnowledgeParserPort | None = None,
        executor: Executor | None = None,
        publisher: SuggestionPublisher | None = None,
        read_only: bool = False,
        capabilities=None,
        span_sink: SpanSink | None = None,
        configuration=None,
    ):
        options = {}
        if database_url.startswith("sqlite"):
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if database_url in {"sqlite://", "sqlite:///:memory:"}:
                options["poolclass"] = StaticPool
        self.engine = create_engine(database_url, **options)
        from .configuration import Configuration
        self.configuration = configuration or Configuration("full")
        self.identity = identity or LocalIdentity()
        self.authorization = authorization or SpaceAuthorization()
        self.model, self.model_config, self.parser = model, model_config, parser
        self.publisher = publisher
        self.capabilities = capabilities
        self.span_sink = span_sink
        self.read_only = read_only
        self.executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="km")

    @contextmanager
    def session(self):
        with Session(self.engine) as db:
            if self.read_only:
                db.exec(text("PRAGMA query_only = ON"))
            yield db

    def service(self, db: Session, tenant_id: str | None = None,
                space_id: str = "local", model_id: str | None = None) -> KnowledgeService:
        hosted = self.capabilities.scoped(tenant_id, space_id, model_id) if self.capabilities and tenant_id else None
        return KnowledgeService(
            db,
            model_port=hosted or self.model,
            model_config=hosted.model_config if hosted else self.model_config,
            parser_port=hosted or self.parser,
            span_sink=self.span_sink,
        )

    def initialize(self) -> None:
        from .locking import DatabaseLock
        self.database_lock = DatabaseLock(self.engine.url)
        if self.read_only:
            with self.session() as db:
                if db.exec(text("PRAGMA user_version")).one()[0] != SCHEMA_VERSION:
                    raise ValueError("Upgrade the KM database before freezing writes")
                unfinished = db.exec(select(KnowledgeIngestJob).where(
                    KnowledgeIngestJob.status.in_(["running", "queued", "cancel_requested"])
                )).first()
                if unfinished:
                    raise ValueError("Finish or cancel active ingestion before freezing writes")
            return
        initialize_schema(self.engine)
        with self.session() as db:
            jobs = db.exec(
                select(KnowledgeIngestJob).where(
                    KnowledgeIngestJob.status.in_(["running", "queued", "cancel_requested"])
                )
            ).all()
            service = self.service(db)
            for job in jobs:
                if job.status == "cancel_requested":
                    service._finalize_cancelled_job(job, "Cancelled during restart")
                else:
                    service._delete_partial_ingest_document(job)
                    service._update_job(
                        job,
                        status="failed",
                        stage="interrupted",
                        error="PROCESS_RESTART_INTERRUPTED",
                        finished_at=utc_now(),
                    )
                    service._clear_embedded_content(job)

    def enqueue(self, job_id: str, principal, space_id: str) -> None:
        self.executor.submit(self._run, job_id, principal, space_id)

    def _run(self, job_id: str, principal, space_id: str) -> None:
        with self.session() as db:
            job = db.get(KnowledgeIngestJob, job_id)
            metadata = (job.metadata_json or {}) if job else {}
            service = self.service(db, principal.tenant_id, space_id,
                                   (metadata.get("metadata") or {}).get("km_model_id"))
            try:
                self.authorization.authorize(principal, space_id, "write")
            except Exception:
                job = db.get(KnowledgeIngestJob, job_id)
                if job:
                    service._update_job(
                        job,
                        status="failed",
                        stage="failed",
                        error="BACKGROUND_PERMISSION_DENIED",
                        finished_at=utc_now(),
                    )
                    service._clear_embedded_content(job)
                return
            service.run_ingest_job(job_id)

    def ready(self) -> bool:
        try:
            with self.session() as db:
                db.exec(text("SELECT 1"))
            return True
        except Exception:
            return False

    def close(self) -> None:
        self.executor.shutdown(wait=True)
        self.engine.dispose()
        if hasattr(self, "database_lock"): self.database_lock.close()
