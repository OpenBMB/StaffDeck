"""Adapt the releases task tracker to selected modules, identity and Runtime storage.

The shared tracker owns submission/polling. Providers only resolve current HTTP
definitions. No credentials or copied local resource rows are persisted here.
"""
from contextlib import contextmanager
from types import SimpleNamespace

from staffdeck_harness.contracts.errors import ModuleSdkError
from staffdeck_harness.contracts.http_task import HttpTaskDefinition


def local_definition(db, inv, *, active_sop_id=None):
    from app.db.models import Tool
    row = db.get(Tool, inv.binding_id)
    if row is None or row.tenant_id != inv.context.tenant_id or not row.enabled:
        raise ModuleSdkError("工具已停用或不存在", code="RESOURCE_UNAVAILABLE")
    if row.tool_type != "http":
        return None
    from app.tools.tool_executor import ToolExecutor
    denied = ToolExecutor(db).scope_error(row, active_skill_id=active_sop_id,
        agent_id=None if inv.context.agent_id.endswith(':overall') else inv.context.agent_id)
    if denied is not None:
        raise ModuleSdkError(denied.error.message, code=denied.error.code)
    return HttpTaskDefinition(row.name, row.url, row.method, row.headers_json or {},
        row.auth_json or {}, (row.config_json or {}).get("execution") or {})


def transient_tool(definition, tenant_id, tool_id):
    from staffdeck_harness.contracts.http_task import ResolvedHttpTool
    from app.tools.tool_schema import ToolExecutionPolicy
    from app.config import get_settings
    raw_policy = dict(definition.execution_policy)
    raw_policy.setdefault('timeout_seconds', get_settings().tool_timeout_seconds)
    policy = ToolExecutionPolicy.model_validate(raw_policy).model_dump()
    # This is an in-memory transport value, never added to any resource database.
    return ResolvedHttpTool(id=tool_id, tenant_id=tenant_id, name=definition.name,
        method=definition.method, url=definition.url,
        headers_json=dict(definition.headers), auth_json=dict(definition.auth),
        config_json={"execution": policy})


def execute_http(inv, authorized, *, db, active_sop_id=None, active_node_id=None,
                 timeout_seconds=None):
    """Both modes use the same transport; deferred mode adds the existing tracker."""
    from app.tools.tool_executor import ToolExecutor
    from staffdeck_harness.contracts.invocation import ModuleResult
    from staffdeck_harness.runtime.actors import capture_actor
    definition = authorized.definition
    tool = transient_tool(definition, inv.context.tenant_id, inv.binding_id)
    arguments = dict(inv.arguments)
    arguments.pop("tool_id", None)
    if definition.execution_policy.get("execution_mode") != "detached":
        result = ToolExecutor(db).execute_sync_http(tool, arguments,
            timeout_seconds_override=timeout_seconds)
        return ModuleResult.ok(result.data) if result.success else ModuleResult.fail(result.error.code, result.error.message)
    if not authorized.digest:
        raise ModuleSdkError("异步工具缺少可校验的配置版本", code="ASYNC_CONTRACT_INCOMPLETE")
    runtime = {
        "actor": capture_actor(db, legacy_owner=inv.context.user_id),
        "module_id": authorized.module_id, "module_version": authorized.module_version,
        "digest": authorized.digest, "operation": inv.operation,
        "sop_id": active_sop_id, "node_id": active_node_id,
        "channel": inv.context.channel,
        "proxy_name": str((inv.metadata or {}).get('proxy_name') or inv.operation),
    }
    result = ToolExecutor(db).enqueue_http(tool, arguments,
        user_id=inv.context.user_id, agent_id=inv.context.agent_id,
        session_id=inv.context.session_id, invocation_id=inv.invocation_id,
        task_frame_id=inv.context.task_frame_id, resume_step_id=None,
        timeout_seconds_override=None, runtime_context=runtime)
    return ModuleResult.ok(result.data) if result.success else ModuleResult.fail(result.error.code, result.error.message)


@contextmanager
def resolve_task_tool(db, task):
    with task_activation(db, task) as activation:
        if isinstance(activation, tuple):
            yield activation[2]
        else:
            yield activation


@contextmanager
def task_activation(db, task):
    """Recheck the original actor, selected provider, binding and PEP before network I/O."""
    from app.db.models import Tool, ChatSession, new_id
    from staffdeck_harness.modules.registry import peek_registry
    registry = db.info.get("staffdeck_registry") or peek_registry()
    saved = (task.status_config_json or {}).get("_runtime")
    if not saved:
        if registry is not None:
            raise ModuleSdkError("任务缺少模块执行身份，禁止自动重放", code="ASYNC_CONTEXT_MISSING")
        tool = db.get(Tool, task.tool_id)
        if tool is None or tool.tenant_id != task.tenant_id or not tool.enabled:
            raise ModuleSdkError("工具已停用或不存在", code="RESOURCE_UNAVAILABLE")
        yield tool
        return
    if registry is None:
        raise ModuleSdkError("异步任务所属模块尚未装配", code="ENGINE_UNAVAILABLE")
    from staffdeck_harness.runtime.actors import restore_actor
    from staffdeck_harness.composition.sources import resolve_staff
    from staffdeck_harness.composition.compiler import CompositionCompiler
    from staffdeck_harness.contracts.sources import SourceContext
    from staffdeck_harness.security.profile import get_profile, Guard
    from staffdeck_harness.runtime.execution import begin, end
    from staffdeck_harness.capabilities.host import CapabilityHost, ActivationSlot, LifecycleFence
    from staffdeck_harness.contracts.invocation import InvocationContext, ModuleInvocation
    from app.session.session_schema import ChatTurnRequest
    actor = restore_actor(db, task.tenant_id, saved["actor"])
    session = db.get(ChatSession, task.session_id)
    if session is None or (session.tenant_id, session.agent_id, session.user_id) != (task.tenant_id, task.agent_id, actor.id):
        raise ModuleSdkError("任务会话身份已改变", code="ASYNC_CONTEXT_MISMATCH")
    profile = get_profile()
    staff, identity = resolve_staff(registry, db, SourceContext(task.tenant_id, task.agent_id,
        task.session_id, saved["channel"], task.user_id), profile)
    snapshot = CompositionCompiler().compile(staff, generation=registry.generation, strict=False)
    owner = SimpleNamespace(registry=registry, db=db, staff_composition=staff, security_context=identity)
    attempt = new_id("extattempt")
    request = ChatTurnRequest(tenant_id=task.tenant_id, agent_id=task.agent_id,
        session_id=task.session_id, user_id=task.user_id, message="异步任务跟踪",
        client_turn_id=attempt, channel=saved["channel"])
    begin(owner, request, session, SimpleNamespace(id=attempt))
    try:
        from dataclasses import replace
        plan = snapshot.sop(saved.get('sop_id')) if saved.get('sop_id') else None
        owner.security_context = replace(owner.security_context,
            sop_authorization_ref=dict(plan.authorization_ref) if plan else None)
        slot = ActivationSlot(snapshot, registry.generation, attempt, task.session_id,
            active_sop_id=saved.get("sop_id"), active_node_id=saved.get("node_id"))
        host = CapabilityHost(db, Guard("external.task", profile), owner.security_context,
            slot, LifecycleFence(registry.generation))
        host.registry = registry
        inv = ModuleInvocation(task.invocation_id or task.id, saved["module_id"], saved["operation"],
            {**dict(task.request_json), "tool_id": task.tool_id},
            InvocationContext(task.tenant_id, task.agent_id, task.user_id, task.session_id,
                attempt, saved["channel"], task_frame_id=task.task_frame_id,
                step_id=saved.get("node_id"), run_id=task.id,
                execution=getattr(request, "_trusted_execution", None)), binding_id=task.tool_id)
        authorized = host.authorize_http_task(inv)
        if (authorized.module_id != saved["module_id"]
                or authorized.module_version != saved["module_version"] or authorized.digest != saved["digest"]):
            raise ModuleSdkError("任务工具或模块版本已改变，未继续外部调用", code="CAPABILITY_SNAPSHOT_CHANGED")
        from staffdeck_harness.composition.compiler import compile_hooks
        from staffdeck_harness.interactions.pipeline_host import InteractionPipelineHost, PipelineState
        from staffdeck_harness.contracts.hooks import HookContext
        from app.db.models import HarnessTaskFrameRecord
        from sqlmodel import select
        frame = db.exec(select(HarnessTaskFrameRecord).where(
            HarnessTaskFrameRecord.tenant_id == task.tenant_id,
            HarnessTaskFrameRecord.session_id == task.session_id,
            HarnessTaskFrameRecord.task_id == task.task_frame_id)).first() if task.task_frame_id else None
        pipeline = InteractionPipelineHost(compile_hooks(registry.hooks(snapshot, sop_id=saved.get('sop_id'))),
            handlers=registry.hook_handlers(snapshot, sop_id=saved.get('sop_id')))
        state = PipelineState(snapshot=snapshot, session_slots=dict(frame.slots_json or {}) if frame else {},
            active_sop_id=saved.get('sop_id'), active_node_id=saved.get('node_id'))
        def hooks(point, invocation, result):
            payload = {'name':saved.get('proxy_name') or {'tool.invoke/v1':'tool_invoke'}.get(invocation.operation, invocation.operation), 'operation':invocation.operation,
                'arguments':dict(invocation.arguments), 'binding_id':invocation.binding_id,
                'success':result.success, 'data':result.data, 'error':result.error,
                'citations':list(result.citations), 'receipt':{
                    'invocation_id':task.invocation_id, 'task_id':task.id,
                    'status':'completed' if result.success else 'failed', 'deferred':True},
                'external_task_id':task.id}
            ctx = HookContext(point=point, tenant_id=task.tenant_id, agent_id=task.agent_id,
                session_id=task.session_id, turn_id=attempt, step=1, snapshot_id=snapshot.snapshot_id,
                payload=payload, generation=registry.generation)
            return pipeline.run(point, ctx, state)
        host.hooks = hooks
        prior = db.info.get('staffdeck_external_activation')
        db.info['staffdeck_external_activation'] = (task.id, host, inv)
        try:
            yield host, inv, transient_tool(authorized.definition, task.tenant_id, task.tool_id)
        finally:
            if prior is None:
                db.info.pop('staffdeck_external_activation', None)
            else:
                db.info['staffdeck_external_activation'] = prior
    finally:
        end(owner)


def project_external_result(db, task, result):
    """Callbacks, progress and terminal results all use the same selected output policy."""
    active = db.info.get('staffdeck_external_activation')
    if active and active[0] == task.id:
        return active[1].project_result(active[2], result)
    # Standalone release helpers have no selected policy pipeline.
    from staffdeck_harness.modules.registry import peek_registry
    registry = db.info.get('staffdeck_registry') or peek_registry()
    if registry is None and not (task.status_config_json or {}).get('_runtime'):
        from staffdeck_harness.runtime.result_policy import project_result
        return project_result(None, None, result)
    with task_activation(db, task) as activation:
        return activation[0].project_result(activation[1], result)
