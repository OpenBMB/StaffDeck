"""Registered SOP runtime provider. The old module path only re-exports this class."""

from staffdeck_harness.sop.contracts import SopDependencies


class SopRuntimeModule:
    module_id = "sop.runtime"

    def submission_validator(self, requirement):
        from staffdeck_harness.sop.submission import SopResultValidator
        return SopResultValidator(requirement)

    def build(self, dependencies: SopDependencies):
        from staffdeck_harness.sop.lifecycle import SopRuntime

        return SopRuntime(
            None, dependencies.events, create_handoff=dependencies.create_handoff
        )

    def store(self, db):
        # Existing storage API remains a compatibility export, not the SOP implementation.
        from app.core.task_frame_store import TaskFrameStore

        return TaskFrameStore(db)
