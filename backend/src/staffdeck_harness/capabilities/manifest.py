"""Project the shared activation/catalog into the existing task-planning contract."""
from staffdeck_harness.capabilities.host import ActivationSlot, CapabilityHost, LifecycleFence


class SnapshotManifestBuilder:
    def __init__(self, engine):
        self.engine = engine

    def build(self, tenant_id, agent_id, skill, step_id):
        from app.core.task_request_compiler import CapabilityDescriptor, CapabilityManifest
        from staffdeck_harness.contracts.errors import ModuleSdkError

        engine = self.engine
        slot = ActivationSlot(engine.snapshot, engine.registry.generation,
            engine.user_message_id or "prepare", active_sop_id=skill.skill_id if skill else None,
            active_node_id=step_id, session_id=engine.session.id if engine.session else None)
        host = CapabilityHost(engine.db, engine.guard, engine.security_context, slot,
                              LifecycleFence(engine.registry.generation))
        available, unavailable, display_names = [], [], {}
        for grant in slot.grants():
            kind = {"knowledge_base": "knowledge", "general_skill": "general_skill"}.get(grant.resource_type, "tool")
            item = CapabilityDescriptor(capability_id=grant.resource_id, name=grant.name,
                kind=kind, capability_scope=grant.scope,
                metadata={"resource_id": grant.resource_id, "operation": grant.operation})
            try:
                descriptor = host._descriptor(grant)
                host._authorize_descriptor(grant.operation, descriptor)
                item.name = descriptor.name
                item.description = descriptor.description
                item.input_schema = dict(descriptor.input_schema)
                item.metadata.update(dict(descriptor.metadata))
                item.metadata.update({"content_digest": descriptor.digest,
                    "tool_id": grant.resource_id, "resource_ids": [grant.resource_id]})
                if kind == "general_skill":
                    item.name = f"general_skill.{descriptor.metadata.get('slug', descriptor.name)}"
                    item.metadata["general_skill_id"] = grant.resource_id
                elif kind == "knowledge":
                    item.name = "knowledge_search"
                    item.metadata["knowledge_base_ids"] = [grant.resource_id]
                display_names[item.name] = str(descriptor.metadata.get('display_name') or descriptor.name)
                available.append(item)
            except ModuleSdkError as exc:
                item.available = False
                item.unavailable_reason = exc.code
                unavailable.append(item)
        events = getattr(engine, 'events', None)
        session = getattr(engine, 'session', None)
        if display_names and session is not None and callable(getattr(events, 'record', None)):
            events.record(tenant_id, session.id, 'capability_manifest_resolved',
                          {'turn_id': engine.user_message_id, 'trace_names': {'tools': display_names}})
        return CapabilityManifest(available=available, unavailable_references=unavailable,
                                  snapshot_revision=engine.snapshot.snapshot_id)
