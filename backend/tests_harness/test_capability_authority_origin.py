"""SOP execution must not erase a capability's direct Staff authorization."""
from dataclasses import replace
from types import SimpleNamespace as NS
import json
import pytest

from staffdeck_harness.capabilities.host import CapabilityHost, ActivationSlot, LifecycleFence
from staffdeck_harness.composition.compiler import CapabilityGrant, CompositionSnapshot, HookPlan, SopExecutionPlan
from staffdeck_harness.contracts.security import SecurityContext, ResourceRef, Decision, SecurityProfile
from staffdeck_harness.contracts.sources import ResourceDescriptor
from staffdeck_harness.contracts.errors import PermissionDenied
from staffdeck_harness.security.profile import Guard
from staffdeck_harness.events.tool_results import tool_outcome

OP = 'knowledge.search/v1'


def host(monkeypatch, *, own=True, override=False):
    monkeypatch.setattr('staffdeck_harness.modules.registry.peek_registry', lambda: None)
    grant = CapabilityGrant(OP, 'knowledge_base', 'kb', 'KB')
    scoped = replace(grant, scope='sop_specific', sop_id='sop', node_id='query')
    grants = ((grant,) if own else ()) + ((scoped,) if override or not own else ())
    plan = SopExecutionPlan('sop', 'v1', 'SOP', {}, (), (), {'sop_id':'sop','definition_hash':'fixed'})
    snapshot = CompositionSnapshot('snapshot','t','staff',{},None,{}, {}, grants, (plan,), HookPlan({}),(),None,())
    calls = []
    def authorize(ctx, module, action, resource):
        calls.append((resource.id, ctx.sop_authorization_ref))
        # Staff resource is permitted; this SOP's delegation list is empty.
        return Decision.deny('missing SOP delegation') if ctx.sop_authorization_ref else Decision.allow('staff allowed')
    profile = SecurityProfile('OSS_LOCAL', None, NS(authorize=authorize), None)
    ctx = SecurityContext('u','t',sop_authorization_ref={'sop_id':'stale','definition_hash':'wrong'})
    instance = CapabilityHost(None, Guard('test',profile),ctx,
        ActivationSlot(snapshot,0,'turn',active_sop_id='sop',active_node_id='query'),LifecycleFence(0))
    return instance, calls, ResourceDescriptor(ResourceRef('knowledge_base','kb','t'),'KB',OP)


@pytest.mark.parametrize('override', [False, True])
def test_staff_knowledge_uses_live_staff_auth_even_inside_sop_slot(monkeypatch, override):
    h, calls, descriptor = host(monkeypatch, override=override)
    h._authorize_descriptor(OP,descriptor)
    assert calls == [('kb',None)]
    assert h.security_context.sop_authorization_ref['sop_id'] == 'stale'  # no shared mutation


def test_sop_only_resource_cannot_fall_back_to_staff_auth(monkeypatch):
    h, calls, descriptor = host(monkeypatch,own=False)
    with pytest.raises(PermissionDenied): h._authorize_descriptor(OP,descriptor)
    assert calls == [('kb',{'sop_id':'sop','definition_hash':'fixed'})]


def test_staff_revocation_remains_an_error(monkeypatch):
    h, calls, descriptor = host(monkeypatch)
    h.guard.profile.pep.authorize = lambda *args: Decision.deny('revoked')
    with pytest.raises(PermissionDenied,match='revoked'): h._authorize_descriptor(OP,descriptor)


@pytest.mark.parametrize('own', [True, False])
def test_business_pep_selects_staff_or_signed_sop_authorization_without_fallback(monkeypatch, own):
    BusinessPep = pytest.importorskip('staffdeck_business_modules.security').BusinessPep
    BusinessSettings = pytest.importorskip('staffdeck_business_modules.configuration').BusinessSettings
    from staffdeck_harness.contracts.runtime_services import ExecutionIdentity
    h, _, descriptor = host(monkeypatch, own=own)
    calls = []
    def check(request):
        calls.append('staff')
        return NS(allowed=True,reason='allowed',decision_id='d',revision='r')
    def delegated(request):
        calls.append('sop')
        return NS(decisions=[NS(sop_id='sop',resource_type='sop',resource_id='sop',action='use',allowed=True)],revision='r')
    definition = NS(sop_id='sop',definition_hash='fixed',dependency_manifest={
        'tenant_id':'t','sop_id':'sop','definition_hash':'fixed','manifest_id':'m','dependencies':[]})
    pep = BusinessPep(BusinessSettings(tenant_id='t'),NS(check=check,authorize_sop_workload=delegated),
        NS(resolve_definitions=lambda **kw: NS(definitions=[definition])))
    h.guard = Guard('test',SecurityProfile('BUSINESS_BASE',None,pep,None))
    h.security_context = replace(h.security_context,provider='base_identity',principal_type='workload',actor_user_id='u',
        execution=ExecutionIdentity('t','u','staff','session','run',1,'trace'))
    if own:
        h._authorize_descriptor(OP,descriptor)
        assert calls == ['staff']
    else:
        with pytest.raises(PermissionDenied,match='exact requested resource'):
            h._authorize_descriptor(OP,descriptor)
        assert calls == ['sop']
        h.slot.snapshot = replace(h.slot.snapshot, sops=(replace(h.slot.snapshot.sops[0],authorization_ref={}),))
        with pytest.raises(PermissionDenied,match='reference is incomplete'):
            h._authorize_descriptor(OP,descriptor)
        assert calls == ['sop']  # Missing delegation cannot fall back to user permission.


def test_related_resource_does_not_inherit_primary_staff_authority(monkeypatch):
    h, calls, descriptor = host(monkeypatch)
    descriptor = replace(descriptor,related_resources=(ResourceRef('knowledge_base','delegated','t'),))
    with pytest.raises(PermissionDenied): h._authorize_descriptor(OP,descriptor)
    assert calls[0] == ('kb',None) and calls[1][1]['sop_id'] == 'sop'
    with pytest.raises(PermissionDenied,match='crossed tenant'):
        h._authorize_descriptor(OP,replace(descriptor,related_resources=(ResourceRef('knowledge_base','kb','other'),)))


def test_mcp_error_prefix_preserves_structured_permission_error_and_receipt():
    payload = {'success':False,'error':{'code':'PERMISSION_DENIED','message':'denied'},
               'receipt':{'invocation_id':'i','status':'denied'}}
    outcome = tool_outcome({'isError':True,'content':[{'type':'text','text':'Error: '+json.dumps(payload)}]})
    assert outcome['success'] is False
    assert outcome['error']['code'] == 'PERMISSION_DENIED'
    assert outcome['receipt']['status'] == 'denied'
    plain = tool_outcome({'isError':True,'content':[{'type':'text','text':'Error: network offline'}]})
    assert plain['error']['code'] == 'ENGINE_TOOL_ERROR'
