"""Request-local facts for employee summaries. No cross-request authorization cache."""
from sqlmodel import select
from app.db.models import (Skill, GeneralSkill, KnowledgeBase, Tool, AgentSkillBranch,
    AgentKnowledgeBranch, KnowledgeDocument, KnowledgeBucket, KnowledgeChunk)
from app.agents.branching import get_overall_agent, _binding_is_private


def summary_facts(db, tenant_id, bindings):
    resources = {}
    for kind, model in (('skill', Skill), ('general_skill', GeneralSkill),
                        ('knowledge_base', KnowledgeBase), ('tool', Tool)):
        ids = {b.resource_id for b in bindings if b.resource_type == kind}
        if ids:
            resources.update({(kind, row.id): row for row in db.exec(select(model).where(
                model.tenant_id == tenant_id, model.id.in_(ids))).all()})
    agents = {b.agent_id for b in bindings}
    skill_branches = {(b.agent_id, b.skill_id): b for b in db.exec(select(AgentSkillBranch).where(
        AgentSkillBranch.tenant_id == tenant_id, AgentSkillBranch.agent_id.in_(agents))).all()} if agents else {}
    kb_branches = {(b.agent_id, b.knowledge_base_id): b for b in db.exec(select(AgentKnowledgeBranch).where(
        AgentKnowledgeBranch.tenant_id == tenant_id, AgentKnowledgeBranch.agent_id.in_(agents),
        AgentKnowledgeBranch.status != 'deleted')).all()} if agents else {}
    kb_ids = {rid for kind, rid in resources if kind == 'knowledge_base'}
    populated = set()
    if kb_ids:
        for model in (KnowledgeDocument, KnowledgeBucket, KnowledgeChunk):
            populated.update(db.exec(select(model.knowledge_base_id).where(
                model.tenant_id == tenant_id, model.knowledge_base_id.in_(kb_ids)).distinct()).all())
    overall = get_overall_agent(db, tenant_id)
    public = {(b.resource_type, b.resource_id) for b in bindings if overall and b.agent_id == overall.id
              and b.status != 'deleted' and not _binding_is_private(b)}
    return {'resources': resources, 'skill_branches': skill_branches, 'kb_branches': kb_branches,
            'populated': populated, 'public': public}
