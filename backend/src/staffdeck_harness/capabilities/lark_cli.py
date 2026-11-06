"""Preserve the OSS Feishu capability behind the v3 host's PEP and ledger."""
from __future__ import annotations

from app.config import get_settings
from app.db.models import ChatSession, Skill
from staffdeck_harness.contracts.errors import ModuleSdkError, PermissionDenied
from staffdeck_harness.contracts.invocation import ModuleResult
from staffdeck_harness.contracts.security import ResourceRef
from staffdeck_harness.contracts.sources import ResourceDescriptor


def descriptor(context):
    from app.lark_cli.service import LARK_CLI_DESCRIPTION, LARK_CLI_INPUT_SCHEMA
    if not get_settings().lark_cli_enabled:
        raise ModuleSdkError('飞书 CLI 已禁用', code='RESOURCE_UNAVAILABLE')
    return ResourceDescriptor(
        ref=ResourceRef(type='tool', id='lark_cli', tenant_id=context.tenant_id,
                        attributes={'binding_status':'active', 'private_to_agent':True}),
        name='lark_cli', operation='tool.invoke/v1', description=LARK_CLI_DESCRIPTION,
        input_schema=LARK_CLI_INPUT_SCHEMA, side_effecting=True, replayable=False,
        metadata={'tool_type':'builtin'},
    )


def invoke(host, inv):
    """Reuse the existing confirmation/credential service, not another AgentLoop."""
    from app.core.capability_manifest import _lark_cli_descriptor
    from app.core.harness_capability_invoker import HarnessCapabilityInvoker
    from app.core.task_request_compiler import CapabilityManifest
    ctx=inv.context
    session=host.db.get(ChatSession,ctx.session_id)
    if session is None or (session.tenant_id,session.user_id)!=(ctx.tenant_id,ctx.user_id):
        raise PermissionDenied('不能使用其他用户会话的飞书身份')
    capability=_lark_cli_descriptor(host.db,ctx.tenant_id,ctx.agent_id)
    if capability is None:
        return ModuleResult.fail('LARK_CLI_DISABLED','飞书 CLI 已禁用')
    plan=host.slot.snapshot.sop(host.slot.active_sop_id) if host.slot.active_sop_id else None
    skill=Skill(tenant_id=ctx.tenant_id,skill_id=plan.skill_id,name=plan.name,
                content_json=dict(plan.content),status='published') if plan else None
    executor=HarnessCapabilityInvoker(host.db,tenant_id=ctx.tenant_id,session=session,
        task_frame_id=ctx.task_frame_id,model_config=host.model_config,
        manifest=CapabilityManifest(available=[capability]),active_skill=skill,
        active_step_id=host.slot.active_node_id,agent_id=ctx.agent_id,run_id=ctx.run_id,
        trace_sink=host.trace,
        ensure_execution_lease=lambda:host.fence.check(host.slot))
    result=executor.invoke('lark_cli',{k:v for k,v in inv.arguments.items() if k!='tool_id'})
    return ModuleResult(success=bool(result.get('success')),data=result.get('data'),
                        error=result.get('error'))
