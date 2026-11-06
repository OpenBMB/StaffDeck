"""Shared runtime wiring for API admission, queued workers, relays and recovery."""
from sqlmodel import Session
from staffdeck_harness.runtime.services import runtime_services, bind_authenticated_session
from staffdeck_harness.runtime.jobs import enqueue_runtime_job

REALM_KEY = "_runtime_binding"


def realm_client_name(db, name):
    import hashlib, json
    from staffdeck_harness.runtime.session_binding import current_binding
    binding = current_binding(db)
    if binding is None or binding.get("namespace") == "oss-local" and binding.get("source.staff") == "source.staff.local":
        return name
    return name + ":" + hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()[:16]


def bind_actor(db, actor):
    from fastapi import HTTPException
    from app.public_api.errors import PublicAPIError
    from staffdeck_harness.runtime.actors import bind_actor as shared_bind_actor
    try:
        return shared_bind_actor(db, actor)
    except HTTPException as exc:
        raise PublicAPIError(exc.status_code, "API_SUBJECT_DENIED" if exc.status_code < 500 else "API_IDENTITY_UNAVAILABLE", str(exc.detail)) from exc


def stamp_client(db, client):
    from staffdeck_harness.runtime.session_binding import current_binding
    binding = current_binding(db)
    if binding:
        client.metadata_json = {**(client.metadata_json or {}), REALM_KEY: binding}


def check_client(db, client):
    from staffdeck_harness.runtime.session_binding import current_binding
    from app.public_api.errors import PublicAPIError
    binding = current_binding(db)
    saved = (client.metadata_json or {}).get(REALM_KEY)
    legacy_local = binding is None or binding.get("source.staff") == "source.staff.local" and binding.get("namespace") == "oss-local"
    if saved != binding and not (saved is None and legacy_local):
        raise PublicAPIError(403, "API_REALM_MISMATCH", "This credential belongs to another runtime assembly; issue a key for this assembly.")


def enqueue(db, name, func, *args, submit=None):
    services = runtime_services(db)
    return enqueue_runtime_job(name, func, *args, data_services=services,
                               registry=db.info.get("staffdeck_registry"), queue_submit=submit)


def maintenance_sessions(engine):
    from staffdeck_harness.modules.registry import peek_registry
    from staffdeck_harness.contracts.manifest import SlotName
    from staffdeck_harness.runtime.services import LocalRuntimeServicesModule
    from contextlib import nullcontext
    registry = peek_registry()
    with registry.turn_lease() if registry else nullcontext():
        with Session(engine) as control:
            module = registry.provider(SlotName.RUNTIME_SERVICES).provider if registry else LocalRuntimeServicesModule()
            yield from module.maintenance_sessions(control)
