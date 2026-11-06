"""Default workspace adapter; both editions retain the same file/tool contracts."""
from pathlib import Path
from types import SimpleNamespace
from staffdeck_harness.contracts.security import SecurityContext


def facade_dependencies(activation):
    from staffdeck_harness.capabilities.facade import FacadeDeps
    context = activation.context
    guard = SimpleNamespace(require=lambda ignored, operation, ref: activation.require(operation, ref))
    return FacadeDeps(None, guard, SecurityContext(context.user_id, context.tenant_id,
        execution=context.execution), trace=activation.emit, remaining_seconds=activation.remaining_seconds)


class LocalWorkspaceModule:
    def build(self, activation):
        from staffdeck_harness.capabilities.facade import SandboxFacade
        context, policy = activation.context, activation.policy
        return SandboxFacade(facade_dependencies(activation), workspace_root=Path(activation.local_root),
            run_id=context.run_id or context.turn_id, task_frame_id=context.task_frame_id or context.turn_id,
            sandbox_enabled=bool(policy.get("sandbox_enabled")), network_mode=policy.get("sandbox_network_mode", "all"),
            allowed_domains=tuple(policy.get("sandbox_allowed_domains", ())))
