"""Read-only PD admission association with configured native capability authority."""
from __future__ import annotations

from typing import Any
from copy import deepcopy

from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError
from sqlmodel import Session

from app.public_api.auth import PublicPrincipal, enforce_agent_access
from app.public_api.errors import PublicAPIError


class AuthorityContext(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sessionKey: str = Field(min_length=1)
    projectKey: str = Field(min_length=1)
    expectedRevision: StrictInt = Field(ge=1)
    requestId: str = Field(min_length=1)


class KnowledgeExecutionContext(AuthorityContext):
    snapshotId: str = Field(min_length=1)
    registryGeneration: StrictInt = Field(ge=0)


def resolve_execution_authority(db, principal, agent_id, raw):
    try:
        context = KnowledgeExecutionContext.model_validate(raw)
    except ValidationError as exc:
        raise PublicAPIError(422, "SOP_AUTHORITY_INPUT_INVALID", "Invalid host execution context.") from exc
    result = resolve_authority(db, principal, agent_id, context.model_dump(exclude={"snapshotId", "registryGeneration"}))
    if result["snapshotId"] != context.snapshotId or result["registryGeneration"] != context.registryGeneration:
        raise PublicAPIError(409, "SOP_AUTHORITY_STALE", "The native authority projection changed before execution.")
    return result


def resolve_authority(db: Session, principal: PublicPrincipal, agent_id: str,
                      raw_context: Any) -> dict[str, Any]:
    from app.config import get_settings
    from app.public_api.pilotdeck_domain_host import require_pilotdeck_domain_host
    from staffdeck_harness.capabilities.host import ActivationSlot, CapabilityHost, LifecycleFence
    from staffdeck_harness.composition.compiler import CompositionCompiler
    from staffdeck_harness.composition.sources import resolve_staff
    from staffdeck_harness.contracts.errors import ModuleSdkError
    from staffdeck_harness.contracts.sources import SourceContext
    from staffdeck_harness.modules.registry import peek_registry
    from staffdeck_harness.security.profile import Guard, get_profile

    try:
        context = AuthorityContext.model_validate(raw_context)
    except ValidationError as exc:
        raise PublicAPIError(422, "SOP_AUTHORITY_INPUT_INVALID", "Invalid host authority context.") from exc
    enforce_agent_access(principal, agent_id)
    if not principal.credential_id:
        raise PublicAPIError(403, "SOP_AUTHORITY_CREDENTIAL_REQUIRED", "An admitted account credential is required.")
    try:
        host_client = require_pilotdeck_domain_host()
        pinned = host_client.read_sop_authority(
            tenant_id=principal.tenant_id, actor_user_id=principal.actor_user.id,
            agent_id=agent_id, credential_id=principal.credential_id,
            context=context.model_dump())
    except PublicAPIError:
        raise
    except RuntimeError as exc:
        raise PublicAPIError(503, "SOP_AUTHORITY_HOST_UNAVAILABLE", "The admitted SOP host is unavailable.") from exc
    if not isinstance(pinned, dict):
        raise PublicAPIError(502, "SOP_AUTHORITY_HOST_INVALID", "Invalid owner attestation.")
    if not isinstance(pinned.get("content"), dict) or any(not isinstance(pinned.get(key), str) or not pinned[key].strip()
                                                        for key in ("sopId", "sopVersion", "nodeId")):
        raise PublicAPIError(502, "SOP_AUTHORITY_HOST_INVALID", "Invalid owner pin.")
    admission = pinned.get("admission") or {}
    if not isinstance(admission, dict):
        raise PublicAPIError(502, "SOP_AUTHORITY_HOST_INVALID", "Invalid owner admission.")
    if (admission.get("tenantId"), admission.get("actorUserId"), admission.get("agentId"),
            admission.get("credentialId"), admission.get("pilotDeckUserId")) != (
            principal.tenant_id, principal.actor_user.id, agent_id, principal.credential_id,
            host_client.pilotdeck_user_id) or any(pinned.get(k) != v for k, v in context.model_dump().items()):
        raise PublicAPIError(403, "SOP_AUTHORITY_ADMISSION_MISMATCH", "Owner admission does not match this request.")
    registry = peek_registry()
    if registry is None:
        raise PublicAPIError(503, "SOP_AUTHORITY_SOURCE_UNAVAILABLE", "Configured sources are unavailable.")
    try:
        profile = get_profile(get_settings())
        # This is an external PD session association, not a native task or ChatSession.
        staff, identity = resolve_staff(registry, db, SourceContext(
            principal.tenant_id, agent_id, user_id=principal.actor_user.id), profile)
        definitions = [s for s in staff.sops if s.skill_id == pinned.get("sopId")
                       and s.version == pinned.get("sopVersion")]
        from app.skills.nesting import _expand_content, SopNestingError
        # Native sources expose runtime-expanded content. Apply the same pure
        # transformation to the host pin; never strip fields from either side.
        try:
            pin_content = deepcopy(pinned.get("content") or {})
            if len(definitions) == 1 and definitions[0].content.get("runtime_expanded") is True:
                pin_content = _expand_content(str(pinned.get("sopId") or ""), pin_content, {},
                                              path=[str(pinned.get("sopId") or "")])
        except (SopNestingError, TypeError, AttributeError) as exc:
            raise PublicAPIError(409, "SOP_AUTHORITY_PIN_MISMATCH", "The exact published SOP pin is unavailable.") from exc
        if len(definitions) != 1 or dict(definitions[0].content) != pin_content:
            raise PublicAPIError(409, "SOP_AUTHORITY_PIN_MISMATCH", "The exact published SOP pin is unavailable.")
        Guard("staffdeck.sop-authority", profile).require(identity, "sop.execute/v1", definitions[0].ref)
        snapshot = CompositionCompiler().compile(staff, generation=registry.generation, strict=False)
        plan = snapshot.sop(definitions[0].skill_id)
        if plan is None:
            raise PublicAPIError(409, "SOP_AUTHORITY_PIN_UNAVAILABLE", "The selected SOP cannot be composed.")
        nodes = [n for n in plan.content.get("nodes", [])
                 if (n.get("node_id") or n.get("step_id")) == pinned.get("nodeId")]
        if len(nodes) != 1:
            raise PublicAPIError(409, "SOP_AUTHORITY_NODE_MISMATCH", "The pinned node is unavailable.")
        slot = ActivationSlot(snapshot, registry.generation, context.requestId,
                              active_sop_id=plan.skill_id, active_node_id=pinned["nodeId"])
        host = CapabilityHost(db, Guard("staffdeck.sop-authority", profile), identity,
                              slot, LifecycleFence(registry.generation))
        available, unavailable = [], []
        for grant in slot.grants():
            if grant.required:
                continue
            item = {"operation": grant.operation, "resourceType": grant.resource_type,
                    "resourceId": grant.resource_id, "bindingId": grant.binding_id,
                    "scope": grant.scope, "sopId": grant.sop_id, "nodeId": grant.node_id,
                    "required": False, "providerModuleId": grant.provider_module_id,
                    "providerVersion": grant.provider_version}
            try:
                descriptor = host._descriptor(grant)
                host._authorize_descriptor(grant.operation, descriptor)
                # The installed public Knowledge Port implements only this provider.
                if grant.operation != "knowledge.search/v1" or grant.resource_type != "knowledge_base" \
                        or grant.provider_module_id != "knowledge.local" or nodes[0].get("type") != "response":
                    item["reason"] = "SOP_AUTHORITY_PORT_UNMAPPED"
                    unavailable.append(item)
                    continue
                item.update({"descriptorDigest": descriptor.digest, "name": descriptor.name,
                             "sideEffecting": descriptor.side_effecting,
                             "selectionMode": "current"})
                available.append(item)
            except ModuleSdkError as exc:
                item["reason"] = exc.code
                unavailable.append(item)
    except ModuleSdkError as exc:
        raise PublicAPIError(403, exc.code, exc.message) from exc
    return {"context": context.model_dump(), "sopId": plan.skill_id,
            "sopVersion": plan.version, "nodeId": pinned["nodeId"],
            "snapshotId": snapshot.snapshot_id, "registryGeneration": registry.generation,
            "authorizationRevision": None, "optionalCapabilities": available,
            "unavailable": unavailable}


def constrain_query(query, projection, db, principal, agent_id):
    from app.agents.branching import visible_knowledge_base_versions

    bases = {item["resourceId"] for item in projection["optionalCapabilities"]}
    requested = set(query.knowledge_base_ids)
    selected = bases & requested if requested else bases
    versions = visible_knowledge_base_versions(db, principal.tenant_id, agent_id)
    current = {versions[base].id for base in selected if base in versions}
    if query.knowledge_base_version_ids:
        current &= set(query.knowledge_base_version_ids)
    selected = {base for base in selected if base in versions and versions[base].id in current}
    if not selected or not current:
        raise PublicAPIError(403, "SOP_AUTHORITY_EMPTY_SCOPE", "No authorized Knowledge selection remains.")
    query.knowledge_base_ids = sorted(selected)
    query.knowledge_base_version_ids = sorted(current)
    query._sop_authorized = True
    return query
