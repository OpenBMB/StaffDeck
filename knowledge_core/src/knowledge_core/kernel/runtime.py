from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool
from fastapi import HTTPException
from ..identity import LocalIdentity, SpaceAuthorization
from ..ports import LocalParser
from .use_cases import Content, Tasks
from .evidence import Evidence
from .retrieval import Search
from .schema import initialize


class KernelRuntime:
    def __init__(self, database_url, *, configuration, identity=None, authorization=None,
                 model=None, model_config=None, parser=None, executor=None, publisher=None,
                 capabilities=None, read_only=False, span_sink=None, plugin_factory=None):
        options = {"connect_args": {"check_same_thread": False, "timeout": 30}} if database_url.startswith("sqlite") else {}
        if database_url in {"sqlite://", "sqlite:///:memory:"}: options["poolclass"] = StaticPool
        self.engine = create_engine(database_url, **options)
        self.configuration = configuration
        self.identity = identity or LocalIdentity()
        if not configuration.enabled("authentication") and identity is not None:
            raise ValueError("Identity provider configured while authentication is disabled")
        if configuration.enabled("authentication") and (identity is None or isinstance(identity, LocalIdentity)):
            raise ValueError("Authentication requires an identity provider")
        self.authorization = authorization or SpaceAuthorization()
        self.model, self.model_config, self.parser = model, model_config, parser or LocalParser()
        self.publisher, self.capabilities = publisher, capabilities
        self.read_only, self.span_sink = read_only, span_sink
        self.plugins = plugin_factory(self) if plugin_factory else {}
        self.executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="km")
        self.content, self.tasks = Content(self), Tasks(self)
        self.evidence, self.retrieval = Evidence(self), Search(self)

    def initialize(self):
        from ..locking import DatabaseLock
        self.database_lock = DatabaseLock(self.engine.url)
        try:
            initialize(self)
            if self.configuration.full_contract and isinstance(self.identity, LocalIdentity) and not self.read_only:
                spaces = self.plugins['spaces']
                with self.engine.begin() as db:
                    if not db.execute(select(spaces.spaces).where(spaces.spaces.c.id == 'local')).first():
                        db.execute(spaces.spaces.insert().values(id='local', name='Local', tenant_id='local'))
            if not self.read_only: self.tasks.recover()
            for plugin in self.plugins.values(): plugin.start()
        except Exception:
            self.database_lock.close()
            raise

    def authorize(self, actor, context, action, require_scope=True):
        if self.read_only and action == "write": raise HTTPException(503, "Writes frozen for maintenance")
        if action not in actor.permissions: raise HTTPException(403, "Permission denied")
        if "spaces" in self.plugins and require_scope: self.plugins["spaces"].authorize(actor, context, action)
        if "catalog" in self.plugins: self.plugins["catalog"].validate(context)
        if "versions" in self.plugins: self.plugins["versions"].validate(context, action)

    def visible(self, document_id, context):
        return all(plugin.visible(document_id, context) for name, plugin in self.plugins.items() if name in {"spaces", "catalog", "versions"})

    def require_content(self, document_id, context):
        if document_id is None and any(name in self.plugins for name in ("spaces", "catalog", "versions")):
            raise HTTPException(404, "Resource not found")
        if document_id and not self.visible(document_id, context): raise HTTPException(404, "Resource not found")

    def assign_content(self, db, document_id, context):
        for name, plugin in self.plugins.items():
            if name in {"spaces", "catalog", "versions"}: plugin.assign(db, document_id, context)

    def ports(self, context, model_id=None):
        if model_id and not self.capabilities and (not self.model_config or self.model_config.model != model_id):
            raise HTTPException(422, "Unknown model_id")
        hosted = self.capabilities.scoped(context.get("tenant_id", "local"), context.get("space_id", ""), model_id) if self.capabilities else None
        return hosted or self.model, hosted.model_config if hosted else self.model_config, hosted or self.parser

    def ready(self):
        try:
            with self.engine.connect() as db: db.execute(select(1))
            return True
        except Exception: return False

    def close(self):
        self.executor.shutdown(wait=True)
        for plugin in reversed(list(self.plugins.values())): plugin.close()
        self.engine.dispose()
        if hasattr(self, "database_lock"): self.database_lock.close()
