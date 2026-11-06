"""Tool-less inference service. Model resolution and correction belong to Runtime."""
from staffdeck_harness.runtime.structured_output import generate_structured


def resolve_model(db, tenant_id, staff_id):
    from staffdeck_harness.modules.registry import peek_registry
    from staffdeck_harness.runtime.staff_directory import directory_context
    from staffdeck_harness.composition.sources import authorize_staff
    from staffdeck_harness.security.profile import get_profile
    from app.config import get_settings
    registry = db.info.get('staffdeck_registry') or peek_registry()
    context = directory_context(db, tenant_id, staff_id)
    if registry is None:
        from staffdeck_harness.composition.local_sources import LocalStaffSource
        return LocalStaffSource(db).model(context)
    _, source, _ = authorize_staff(registry, db, context, get_profile(get_settings()))
    return source.model(context)


class RuntimeInference:
    def __init__(self, db):
        self.db = db

    def structured(self, *, tenant_id, staff_id, phase, system, payload, validate):
        from app.llm import LLMClient
        model = resolve_model(self.db, tenant_id, staff_id)
        if model is None:
            raise ValueError('缺少默认模型配置')
        client = LLMClient(model)
        def call(current, attempt):
            return client.generate_text(system, current, response_format={'type':'json_object'})
        return generate_structured(call, payload, validate, phase=phase)
