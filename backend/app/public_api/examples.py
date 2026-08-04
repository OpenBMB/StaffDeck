from __future__ import annotations

from fastapi import APIRouter


router = APIRouter(prefix="/examples", tags=["developer"])


@router.get("/{language}", response_model=dict)
def get_example(language: str) -> dict:
    if language == "python":
        return {
            "language": "python",
            "code": '''import httpx\n\nbase_url = "https://staffdeck.example/api/v1"\nheaders = {"Authorization": "Bearer sd_live_...", "Idempotency-Key": "order-123"}\njob = httpx.post(\n    f"{base_url}/agents/agent_123/runs",\n    headers=headers,\n    json={"input": "查询报销制度", "session_mode": "stateless"},\n).raise_for_status().json()\nresult = httpx.get(\n    f"{base_url}/runs/{job['id']}/result",\n    headers={"Authorization": headers["Authorization"]},\n).raise_for_status().json()\n''',
        }
    if language == "typescript":
        return {
            "language": "typescript",
            "code": '''const baseUrl = "https://staffdeck.example/api/v1";\nconst authorization = "Bearer sd_live_...";\nconst job = await fetch(`${baseUrl}/agents/agent_123/runs`, {\n  method: "POST",\n  headers: { Authorization: authorization, "Content-Type": "application/json", "Idempotency-Key": "order-123" },\n  body: JSON.stringify({ input: "查询报销制度", session_mode: "stateless" }),\n}).then((response) => response.json());\nconst result = await fetch(`${baseUrl}/runs/${job.id}/result`, { headers: { Authorization: authorization } }).then((response) => response.json());\n''',
        }
    return {"language": language, "code": "", "supported": ["python", "typescript"]}
