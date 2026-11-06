"""Select employee-binding management independently of the resource implementation."""
from contextlib import contextmanager

from staffdeck_harness.contracts.errors import ModuleSdkError
from staffdeck_harness.contracts.manifest import SlotName


@contextmanager
def selected_bindings(db, request, agent_id):
    from staffdeck_harness.modules.registry import peek_registry
    registry = db.info.get('staffdeck_registry') or peek_registry()
    item = registry.provider(SlotName.STAFF_SOURCE) if registry else None
    build = getattr(item.provider, 'binding_manager', None) if item else None
    if not callable(build):
        raise ModuleSdkError('员工来源未提供绑定管理契约', code='BINDING_MANAGEMENT_UNAVAILABLE')
    with build(db, request, agent_id) as manager:
        if any(not callable(getattr(manager, name, None)) for name in ('list', 'require', 'change')):
            raise ModuleSdkError('员工绑定管理契约无效', code='BINDING_MANAGEMENT_INVALID')
        yield manager


class LocalEmployeeBindings:
    def __init__(self, db, request, agent_id):
        from app.db.models import User
        from staffdeck_harness.contracts.errors import PermissionDenied
        self.db, self.agent_id, self.tenant_id = db, agent_id, request.subject.tenant_id
        self.user = db.get(User, request.subject.user_id)
        if self.user is None or self.user.tenant_id != self.tenant_id:
            raise PermissionDenied('员工绑定身份不匹配')

    def list(self, kind=None):
        from app.api.agents import get_agent_resources
        rows = get_agent_resources(self.agent_id, self.tenant_id, self.db, self.user)
        return [row.model_dump() for row in rows if kind is None or row.resource_type == kind]

    def require(self, kind, resource_id):
        from fastapi import HTTPException
        rows = [row for row in self.list(kind) if row['resource_id'] == resource_id]
        if len(rows) != 1:
            raise HTTPException(404, '资源未绑定到当前员工')
        return rows[0]

    def change(self, kind, resource_id, *, active, remove=False):
        from fastapi import HTTPException
        from sqlalchemy import delete, update
        from app.api.agents import _get_agent, _ensure_can_manage_agent
        from app.db.models import AgentResourceBinding, utc_now
        agent = _get_agent(self.db, self.tenant_id, self.agent_id)
        _ensure_can_manage_agent(agent, self.user)
        if agent.is_overall:
            raise HTTPException(400, 'Overall agent uses the global resource pool')
        row = self.require(kind, resource_id)
        table = AgentResourceBinding
        query = delete(table) if remove else update(table).values(
            status='active' if active else 'inactive', updated_at=utc_now())
        result = self.db.exec(query.where(table.id == row['id'], table.tenant_id == self.tenant_id,
            table.agent_id == self.agent_id, table.resource_type == kind, table.resource_id == resource_id))
        if result.rowcount != 1:
            self.db.rollback()
            raise HTTPException(409, '员工绑定已改变，请刷新后重试')
        self.db.commit()
        return {'status': 'removed' if remove else 'active' if active else 'inactive'}
