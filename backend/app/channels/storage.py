"""Shared transaction boundary for channel adapters, inbox and outbox workers.

An explicit alternate engine is for standalone integration/tests. Production callers
using app.db.engine resolve the selected Runtime service, including all model binds.
The registry lease covers the transaction so a switch cannot close its storage early.
"""
from contextlib import contextmanager, nullcontext

from sqlmodel import Session

from staffdeck_harness.contracts.errors import ModuleSdkError
from staffdeck_harness.contracts.manifest import SlotName


def applied_fingerprint():
    import hashlib
    import json
    from dataclasses import asdict
    from app.config import get_settings
    from staffdeck_harness.modules.config import load_applied
    return hashlib.sha256(json.dumps(asdict(load_applied(get_settings())), sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def install_connector_assembly(expected_fingerprint):
    """Spawn child loads only storage/identity wiring, never a second AgentLoop."""
    from app.config import get_settings
    from staffdeck_harness.modules.config import load_applied, settings_view
    from staffdeck_harness.modules.registry import build_registry, install_registry
    if not expected_fingerprint or expected_fingerprint != applied_fingerprint():
        raise ModuleSdkError("渠道子进程装配已过期", code="CHANNEL_ASSEMBLY_CHANGED")
    settings = get_settings()
    registry = build_registry(settings_view(settings, load_applied(settings)))
    install_registry(registry)
    return registry


@contextmanager
def channel_session(db_engine=None):
    from app.db import engine
    from staffdeck_harness.modules.registry import peek_registry
    if db_engine is not None and db_engine is not engine:
        with Session(db_engine) as db:
            yield db
        return
    registry = peek_registry()
    with registry.work_lease() if registry else nullcontext():
        with Session(engine) as control:
            if registry is None:
                yield control
                return
            item = registry.provider(SlotName.RUNTIME_SERVICES)
            factory = getattr(item.provider, "background_services", None) if item else None
            if not callable(factory):
                raise ModuleSdkError("渠道后台运行服务未装配", code="CHANNEL_STORAGE_UNAVAILABLE")
            control.info["staffdeck_registry"] = registry
            services = factory(control)
            with services.session(None, purpose="channel") as db:
                yield db
