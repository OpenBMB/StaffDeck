"""One execution lifecycle seam after the common turn claim, before any model/tool work."""
from dataclasses import replace

from staffdeck_harness.contracts.errors import PermissionDenied
from staffdeck_harness.contracts.manifest import SlotName
from staffdeck_harness.contracts.runtime_services import ExecutionIdentity


def begin(coordinator, request, session, turn):
    registry = coordinator.registry
    item = registry.provider(SlotName.RUNTIME_EXECUTION) if registry else None
    if item is None:
        return
    subject = getattr(request, "_control_subject", None) or coordinator.db.info.get("staffdeck_control_subject")
    identity = item.provider.begin(request, session_id=session.id, turn_id=turn.id, subject=subject)
    if not isinstance(identity, ExecutionIdentity) or (
        identity.tenant_id, identity.actor_user_id, identity.staff_id, identity.session_id
    ) != (request.tenant_id, request.user_id, session.agent_id, session.id):
        raise PermissionDenied("execution module returned a mismatched identity")
    request._trusted_execution = identity
    coordinator.db.info["staffdeck_execution"] = identity
    coordinator._execution_binding = (item.provider, identity)
    coordinator._execution_request = request
    coordinator.security_context = replace(coordinator.security_context, execution=identity,
        actor_user_id=identity.actor_user_id, principal_type="workload", agent_id=identity.staff_id)


def end(coordinator):
    binding = getattr(coordinator, "_execution_binding", None)
    if binding is not None:
        coordinator._execution_binding = None
        try:
            binding[0].end(binding[1])
        except Exception:
            # Cleanup failure must not turn a committed business result into a retryable action.
            import logging
            logging.getLogger(__name__).exception("execution credential cleanup failed")
        finally:
            coordinator.db.info.pop("staffdeck_execution", None)
            coordinator._execution_request._trusted_execution = None
