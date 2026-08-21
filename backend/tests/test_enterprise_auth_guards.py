from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.agents import enterprise_router as agents_router
from app.api.agents import scope_router as agent_scope_router
from app.api.feedback import router as feedback_router
from app.api.general_skills import router as general_skills_router
from app.api.knowledge import router as knowledge_router
from app.api.knowledge_bases import router as knowledge_bases_router
from app.api.memories import router as memories_router
from app.api.model_configs import router as model_configs_router
from app.api.persona import router as persona_router
from app.api.scheduled_tasks import enterprise_router as scheduled_tasks_router
from app.api.sessions import router as sessions_router
from app.api.skills import router as skills_router
from app.api.tools import mcp_router, router as tools_router
from app.api.traces import router as traces_router
from app.api.ui_config import enterprise_router as ui_config_router


def test_enterprise_read_endpoints_require_authentication() -> None:
    app = FastAPI()
    app.include_router(memories_router)
    app.include_router(tools_router)
    app.include_router(mcp_router)
    app.include_router(general_skills_router)
    app.include_router(knowledge_router)
    app.include_router(knowledge_bases_router)
    app.include_router(model_configs_router)
    app.include_router(persona_router)
    app.include_router(skills_router)
    app.include_router(traces_router)
    app.include_router(ui_config_router)
    app.include_router(agents_router)
    app.include_router(agent_scope_router)
    app.include_router(feedback_router)
    app.include_router(scheduled_tasks_router)
    app.include_router(sessions_router)
    client = TestClient(app)

    paths = [
        "/api/enterprise/memories?tenant_id=tenant_demo",
        "/api/enterprise/tools?tenant_id=tenant_demo",
        "/api/enterprise/tools/buckets?tenant_id=tenant_demo",
        "/api/enterprise/tools/a2a/codex-adapter",
        "/api/enterprise/tools/tool_demo?tenant_id=tenant_demo",
        "/api/enterprise/tools/tool_demo/a2a-runs?tenant_id=tenant_demo",
        "/api/enterprise/mcp-servers?tenant_id=tenant_demo",
        "/api/enterprise/mcp-servers/server_demo?tenant_id=tenant_demo",
        "/api/enterprise/general-skills?tenant_id=tenant_demo",
        "/api/enterprise/knowledge/jobs?tenant_id=tenant_demo",
        "/api/enterprise/knowledge-bases?tenant_id=tenant_demo",
        "/api/enterprise/model-configs?tenant_id=tenant_demo",
        "/api/enterprise/persona?tenant_id=tenant_demo",
        "/api/enterprise/skills?tenant_id=tenant_demo",
        "/api/enterprise/traces?tenant_id=tenant_demo",
        "/api/enterprise/ui-config?tenant_id=tenant_demo",
        "/api/enterprise/agents?tenant_id=tenant_demo",
        "/api/enterprise/agent-scope?tenant_id=tenant_demo",
        "/api/enterprise/feedback/summary?tenant_id=tenant_demo",
        "/api/enterprise/scheduled-tasks?tenant_id=tenant_demo",
        "/api/enterprise/sessions?tenant_id=tenant_demo",
    ]

    for path in paths:
        response = client.get(path)
        assert response.status_code == 401
        assert response.json() == {"detail": "Not authenticated"}
