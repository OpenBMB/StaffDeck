"""Registered SOP runtime provider. The old module path only re-exports this class."""

from staffdeck_harness.sop.contracts import SopDependencies


class SopRuntimeModule:
    module_id = "sop.runtime"

    def submission_validator(self, requirement):
        from staffdeck_harness.sop.submission import SopResultValidator
        return SopResultValidator(requirement)

    def build(self, dependencies: SopDependencies):
        import os
        endpoint = os.environ.get("SOP_RUNTIME_ENDPOINT")
        if endpoint:
            from .http_runtime import HttpSopRuntime
            from .runtime_client import RuntimeClient
            return HttpSopRuntime(RuntimeClient(endpoint), dependencies)
        from app.skills.sop_management import legacy_sop_writes_disabled
        if legacy_sop_writes_disabled():
            from staffdeck_harness.contracts.errors import ModuleSdkError
            raise ModuleSdkError("SOP Runtime endpoint is required after cutover", code="SOP_RUNTIME_UNAVAILABLE")
        from staffdeck_harness.sop.lifecycle import SopRuntime

        return SopRuntime(
            None, dependencies.events, create_handoff=dependencies.create_handoff
        )

    def store(self, db):
        # Existing storage API remains a compatibility export, not the SOP implementation.
        from app.core.task_frame_store import TaskFrameStore

        return TaskFrameStore(db)
