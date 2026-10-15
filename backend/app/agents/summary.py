"""Request-local facts for employee summaries. No cross-request authorization cache."""
from sqlmodel import select
from app.db.models import Skill, GeneralSkill, Tool, AgentSkillBranch
from app.agents.branching import get_overall_agent, _binding_is_private


def summary_facts(db, tenant_id, bindings):
    from app.knowledge.http_adapter import enabled as km_enabled
    resources = {}
    for kind, model in (('skill', Skill), ('general_skill', GeneralSkill),
                        ('tool', Tool)):
        ids = {b.resource_id for b in bindings if b.resource_type == kind}
        if ids:
            resources.update({(kind, row.id): row for row in db.exec(select(model).where(
                model.tenant_id == tenant_id, model.id.in_(ids))).all()})
    agents = {b.agent_id for b in bindings}
    skill_branches = {(b.agent_id, b.skill_id): b for b in db.exec(select(AgentSkillBranch).where(
        AgentSkillBranch.tenant_id == tenant_id, AgentSkillBranch.agent_id.in_(agents))).all()} if agents else {}
    kb_branches = {}
    populated = set()
    overall = get_overall_agent(db, tenant_id)
    public = {(b.resource_type, b.resource_id) for b in bindings if overall and b.agent_id == overall.id
              and b.status != 'deleted' and not _binding_is_private(b)}
    return {'resources': resources, 'skill_branches': skill_branches, 'kb_branches': kb_branches,
            'populated': populated, 'public': public}
