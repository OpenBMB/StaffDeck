"""Portable default-staff graph. Never export users, channels, sessions or jobs.

The local adapter preserves branch/version IDs so SOP and knowledge references
remain intact. Other storage implementations must expose an export adapter rather
than reading local shadow tables.
"""
from sqlmodel import select
from sqlalchemy import or_

from app.db import models as M
from staffdeck_harness.contracts.errors import ModuleSdkError
from staffdeck_harness.distribution.manifest import portable

RESOURCE_MODELS = {
    'AgentProfile': M.AgentProfile, 'AgentResourceBinding': M.AgentResourceBinding,
    'AgentModelBinding': M.AgentModelBinding, 'ModelConfig': M.ModelConfig,
    'Tool': M.Tool, 'MCPServer': M.MCPServer, 'GeneralSkill': M.GeneralSkill,
    'Skill': M.Skill, 'SkillVersion': M.SkillVersion,
    'AgentSkillBranch': M.AgentSkillBranch, 'AgentSkillBranchVersion': M.AgentSkillBranchVersion,
    'KnowledgeBase': M.KnowledgeBase, 'KnowledgeBaseVersion': M.KnowledgeBaseVersion,
    'AgentKnowledgeBranch': M.AgentKnowledgeBranch, 'KnowledgeDocument': M.KnowledgeDocument,
    'KnowledgeBucket': M.KnowledgeBucket, 'KnowledgeChunk': M.KnowledgeChunk,
    'KnowledgeConcept': M.KnowledgeConcept,
}
KINDS = {'tool': M.Tool, 'general_skill': M.GeneralSkill, 'skill': M.Skill, 'knowledge_base': M.KnowledgeBase}
REFS = {'tool_ids': 'tool', 'required_tool_ids': 'tool', 'general_skill_ids': 'general_skill',
        'knowledge_base_ids': 'knowledge_base', 'sop_ids': 'skill'}


def export_local(db, tenant_id, employee_ids):
    collected, required = {}, []

    def add(row):
        if row is None or row.tenant_id != tenant_id:
            raise ModuleSdkError('默认员工引用了不存在或不属于当前租户的资源', code='PACKAGE_RESOURCE_MISSING')
        if row.__class__.__name__ not in RESOURCE_MODELS:
            raise ValueError('unsupported resource model')
        collected.setdefault(row.__class__.__name__, {})[row.id] = row
        if sum(len(items) for items in collected.values()) > 20000:
            raise ModuleSdkError('员工资源超过单包记录限制，请拆分打包', code='PACKAGE_TOO_LARGE')

    def selected(model, **conditions):
        return db.exec(select(model).where(model.tenant_id == tenant_id,
            *(getattr(model, key).in_(value) for key, value in conditions.items())).limit(20001)).all()

    for employee_id in employee_ids:
        row = db.get(M.AgentProfile, employee_id)
        add(row)
        if row.is_overall:
            raise ModuleSdkError('不能将全局资源池当作默认员工', code='PACKAGE_STAFF_INVALID')
    for model in (M.AgentResourceBinding, M.AgentModelBinding, M.AgentSkillBranch,
                  M.AgentSkillBranchVersion, M.AgentKnowledgeBranch):
        for row in selected(model, agent_id=employee_ids):
            add(row)

    seen = set()
    def resource(kind, identifier):
        # The shipped Feishu wrapper is a builtin, never a row containing credentials.
        if kind == 'tool' and identifier == 'lark_cli':
            return
        if (kind, identifier) in seen:
            return
        seen.add((kind, identifier))
        model = KINDS[kind]
        keys = [model.id == identifier]
        for key in ('slug', 'skill_id', 'name'):
            if hasattr(model, key):
                keys.append(getattr(model, key) == identifier)
        exact = db.get(model, identifier)
        found = [exact] if exact and exact.tenant_id == tenant_id else db.exec(
            select(model).where(model.tenant_id == tenant_id, or_(*keys))).all()
        if len(found) != 1:
            raise ModuleSdkError(f'资源标识缺失或存在歧义：{kind}', code='PACKAGE_RESOURCE_MISSING')
        row = found[0]
        add(row)
        if kind == 'tool' and row.mcp_server_id:
            add(db.get(M.MCPServer, row.mcp_server_id))
        if kind == 'skill':
            scan(row.content_json)
            for version in selected(M.SkillVersion, skill_id=[row.skill_id]):
                add(version)
                scan(version.content_json)

    def scan(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in REFS and isinstance(item, list):
                    for ref in item:
                        resource(REFS[key], str(ref))
                elif key == 'sub_sop_id' and item:
                    resource('skill', str(item))
                else:
                    scan(item)
        elif isinstance(value, list):
            for item in value:
                scan(item)

    for binding in list(collected.get('AgentResourceBinding', {}).values()):
        if binding.resource_type not in KINDS:
            raise ModuleSdkError('员工含未提供打包契约的资源类型', code='PACKAGE_RESOURCE_UNSUPPORTED')
        resource(binding.resource_type, binding.resource_id)
    for binding in list(collected.get('AgentModelBinding', {}).values()):
        add(db.get(M.ModelConfig, binding.model_config_id))
    for row in db.exec(select(M.ModelConfig).where(M.ModelConfig.tenant_id == tenant_id, M.ModelConfig.is_default.is_(True))).all():
        add(row)
    for name in ('AgentSkillBranch', 'AgentSkillBranchVersion'):
        for branch in list(collected.get(name, {}).values()):
            resource('skill', branch.source_skill_id)
            scan(branch.content_json)
    for branch in list(collected.get('AgentKnowledgeBranch', {}).values()):
        resource('knowledge_base', branch.knowledge_base_id)
    kb_ids = list(collected.get('KnowledgeBase', {}))
    for model in (M.KnowledgeBaseVersion, M.KnowledgeDocument, M.KnowledgeBucket, M.KnowledgeChunk, M.KnowledgeConcept):
        for row in selected(model, knowledge_base_id=kb_ids):
            add(row)
    records = {}
    for name, values in collected.items():
        records[name] = []
        for row in values.values():
            value = portable(row.model_dump(mode='json'), f'resources.{name}.{row.id}', required)
            if name in {'Tool', 'MCPServer'} and any(path.startswith(f'resources.{name}.{row.id}.') for path in required):
                value['enabled'] = False
            if name == 'ModelConfig':
                value.update(api_key_encrypted='', trust_status='unverified', verified_at=None,
                             verified_fingerprint=None, verification_attempt_id=None)
                if any(path.startswith(f'resources.{name}.{row.id}.') for path in required):
                    value['enabled'] = False
            records[name].append(value)
    persona = db.get(M.PersonaConfig, tenant_id)
    return {'schema_version': 1, 'records': records, 'persona_prompt': persona.system_prompt if persona else '',
            'required_configuration': sorted(set(required))}


def export_staff(db, tenant_id, employee_ids, registry):
    if not employee_ids:
        return {'schema_version': 1, 'records': {}, 'required_configuration': []}
    from staffdeck_harness.contracts.manifest import SlotName
    source = registry.provider(SlotName.STAFF_SOURCE) if registry else None
    if source is not None and source.manifest.module_id != 'source.staff.local':
        export = getattr(source.provider, 'export_distribution_staff', None)
        if not callable(export):
            raise ModuleSdkError('当前员工来源没有提供员工及资源导出契约，不能改读本地表', code='PACKAGE_EXPORT_ADAPTER_REQUIRED')
        return export(db, tenant_id=tenant_id, employee_ids=employee_ids)
    return export_local(db, tenant_id, employee_ids)
