"""Registered SOP runtime provider. The old module path only re-exports this class."""

from staffdeck_harness.sop.contracts import SopDependencies


class SopRuntimeModule:
    module_id = "sop.runtime"

    def build(self, dependencies: SopDependencies):
        from staffdeck_harness.sop.lifecycle import SopRuntime

        return SopRuntime(
            dependencies.db, dependencies.events, create_handoff=dependencies.create_handoff
        )

    def store(self, db):
        # Existing storage API remains a compatibility export, not the SOP implementation.
        from app.core.task_frame_store import TaskFrameStore

        return TaskFrameStore(db)
