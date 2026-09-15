"""Trusted orchestration of configured sources; never imports local resource models."""
from dataclasses import replace

from staffdeck_harness.contracts.errors import ModuleSdkError, PermissionDenied
from staffdeck_harness.contracts.manifest import SlotName
from staffdeck_harness.contracts.sources import SourceContext


def source_context(request, *, session_id=None, staff_id=None):
    return SourceContext(request.tenant_id, staff_id or request.agent_id,
        session_id or request.session_id, request.channel or "web", request.user_id,
        getattr(request, "_trusted_execution", None), getattr(request, "_control_subject", None))


def resolve_source(registry, slot, services, *, module_id=None):
    item = registry.get(module_id) if registry and module_id else registry.provider(slot) if registry else None
    if item is None or not item.enabled or item.slot != slot:
        raise ModuleSdkError(f"缺少来源模块 {module_id or slot.value}", code="SOURCE_UNAVAILABLE")
    source = item.provider.build(services)
    methods = {SlotName.STAFF_SOURCE: ("reference", "resolve", "model"),
               SlotName.SOP_SOURCE: ("resolve", "reference"), SlotName.IDENTITY_SOURCE: ("resolve",),
               SlotName.RESOURCE_CATALOG: ("resolve",)}[slot]
    if any(not callable(getattr(source, name, None)) for name in methods):
        raise ModuleSdkError(f"来源模块 {item.manifest.module_id} 不符合契约", code="SOURCE_CONTRACT_INVALID")
    info = getattr(services, "info", {})
    class BoundSource:
        def __getattr__(self, name):
            method = getattr(source, name)
            if name not in (*methods, "profile"):
                return method
            def call(context, *args, **kwargs):
                execution = context.execution or info.get("staffdeck_execution")
                # A worker may resolve another team member in the same transaction.
                # Its current workload must never be silently lent to that member.
                if execution is not None and (execution.tenant_id, execution.staff_id) != (
                        context.tenant_id, context.staff_id):
                    if context.execution is not None:
                        raise PermissionDenied("source execution does not match target Staff")
                    execution = None
                context = replace(context,
                    subject=context.subject or info.get("staffdeck_control_subject"),
                    execution=execution)
                return method(context, *args, **kwargs)
            return call
    return BoundSource()


def authorize_staff(registry, db, context, profile):
    from staffdeck_harness.security.profile import Guard

    identity = resolve_source(registry, SlotName.IDENTITY_SOURCE, db).resolve(context, profile.identity)
    actor = identity.actor_user_id if identity.principal_type == "workload" else identity.principal_id
    if identity.tenant_id != context.tenant_id or (context.user_id and actor != context.user_id):
        raise PermissionDenied("source identity does not match request")
    source = resolve_source(registry, SlotName.STAFF_SOURCE, db)
    ref = source.reference(context)
    if ref.type != "agent" or ref.tenant_id != context.tenant_id or (context.staff_id and ref.id != context.staff_id):
        raise PermissionDenied("source Staff does not match request")
    Guard("staff", profile).require(identity, "staff.use/v1", ref)
    return identity, source, ref


def resolve_staff(registry, db, context, profile):
    identity, source, ref = authorize_staff(registry, db, context, profile)
    return load_composition(registry, db, context, source=source, ref=ref), identity


def load_composition(registry, db, context, *, source=None, ref=None):
    """Internal module selection only; caller must enforce its operation's PEP."""
    source = source or resolve_source(registry, SlotName.STAFF_SOURCE, db)
    ref = ref or source.reference(context)
    if ref.type != "agent" or ref.tenant_id != context.tenant_id or (context.staff_id and ref.id != context.staff_id):
        raise PermissionDenied("source Staff does not match request")
    staff = source.resolve(context)
    if staff.tenant_id != ref.tenant_id or staff.staff_id != ref.id or staff.status != "active":
        raise PermissionDenied("source composition does not match authorized Staff")
    sops = resolve_source(registry, SlotName.SOP_SOURCE, db).resolve(context, staff)
    if any(s.ref.tenant_id != context.tenant_id for s in sops):
        raise PermissionDenied("SOP source crossed tenant boundary")
    if any(c.ref.tenant_id != context.tenant_id or c.ref.id != c.resource_id for c in staff.capabilities):
        raise PermissionDenied("resource binding source crossed tenant boundary")
    return replace(staff, sops=tuple(sops), ref=ref, capabilities=combined_capabilities(staff, sops))


def combined_capabilities(staff, sops):
    """Project the resource visibility union, not SOP-local execution defaults.

    Staff bindings keep their configuration. Resources delegated only by a SOP
    get a neutral visibility entry; their provider, fixed version and binding
    remain on that SopView and are resolved for its own slots by the compiler.
    Otherwise the first SOP referencing an id silently configures every sibling.
    """
    values = {(item.resource_type, item.resource_id): item for item in staff.capabilities}
    for sop in sops:
        for item in sop.capabilities:
            if item.capability_scope != "sop_specific" or item.ref.tenant_id != staff.tenant_id or item.ref.id != item.resource_id:
                raise PermissionDenied("SOP source returned an invalid delegated capability")
            # Custom resource types still need their operation contract to be
            # recognized; it is not a provider/configuration choice.
            contract = {"operation": item.metadata["operation"]} if "operation" in item.metadata else {}
            values.setdefault((item.resource_type, item.resource_id),
                              replace(item, binding_id=None, name=item.resource_id, metadata=contract))
    return tuple(values.values())


def validate_configuration(registry, db, context, *, sop_id=None):
    """Management-side validation may edit inactive Staff; it grants no execution rights."""
    from staffdeck_harness.composition.compiler import CompositionCompiler
    source = resolve_source(registry, SlotName.STAFF_SOURCE, db)
    staff = source.resolve(context)
    if staff.tenant_id != context.tenant_id or (context.staff_id and staff.staff_id != context.staff_id):
        raise PermissionDenied("configuration source returned a different Staff")
    sops = resolve_source(registry, SlotName.SOP_SOURCE, db).resolve(context, staff)
    if sop_id is not None:
        sops = tuple(s for s in sops if s.skill_id == sop_id)
        if not sops:
            raise ModuleSdkError("待发布的 SOP 不在员工配置中", code="SOP_NOT_AVAILABLE")
    if any(s.ref.tenant_id != context.tenant_id for s in sops):
        raise PermissionDenied("SOP configuration crossed tenant boundary")
    return CompositionCompiler().compile(replace(staff, sops=tuple(sops),
        capabilities=combined_capabilities(staff, sops)), generation=registry.generation)
