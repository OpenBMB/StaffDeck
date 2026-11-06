"""Prevent a stored session from silently becoming a session of another source/identity realm."""
from staffdeck_harness.contracts.errors import ModuleSdkError
from staffdeck_harness.contracts.manifest import SlotName


def current_binding(db):
    from staffdeck_harness.modules.registry import peek_registry
    from staffdeck_harness.runtime.services import runtime_services
    registry = getattr(db, "info", {}).get("staffdeck_registry") or peek_registry()
    if registry is None:
        return None
    if any(registry.provider(slot) is None for slot in (SlotName.STAFF_SOURCE, SlotName.IDENTITY_SOURCE, SlotName.SECURITY_PEP)):
        return None  # Standalone contract tests/helpers do not constitute a deployed runtime.
    return {"namespace": runtime_services(db, registry).namespace,
            **{slot.value: registry.provider(slot).manifest.module_id for slot in
               (SlotName.STAFF_SOURCE, SlotName.IDENTITY_SOURCE, SlotName.SECURITY_PEP) if registry.provider(slot)}}


def bind_session(db, session, *, created=False):
    current = current_binding(db)
    if current is None:
        return
    state = dict(session.context_state_json or {})
    saved = state.get("runtime_binding")
    if saved is not None and saved != current:
        raise ModuleSdkError("此会话属于另一套来源或身份装配，请新建会话；原历史不会被迁移或覆盖。",
                             code="SESSION_ASSEMBLY_MISMATCH")
    if saved is None:
        legacy_local = current.get("source.staff") == "source.staff.local" and current.get("security.pep") == "security.oss_local"
        if not created and not legacy_local:
            raise ModuleSdkError("旧会话尚未建立可信装配归属，请新建会话。", code="SESSION_ASSEMBLY_UNBOUND")
        state["runtime_binding"] = current
        session.context_state_json = state
