"""Configure a Feishu test binding in the isolated local acceptance host."""

import json
import os
from pathlib import Path

import httpx
from dotenv import dotenv_values

root = Path(os.environ["SOP_ACCEPTANCE_ROOT"])
assert root.parent.joinpath("staffdeck.sqlite").exists()
credentials = json.loads((root / "credentials.json").read_text())
dotenv = dotenv_values(
    os.environ.get("SOP_ACCEPTANCE_DOTENV", "/Users/a1/Desktop/.env")
)
actor = credentials["sop-actor"]
with httpx.Client(
    base_url=os.environ.get("SOP_ACCEPTANCE_HOST", "http://127.0.0.1:26390"),
    headers={"Authorization": f"Bearer {actor['token']}"},
    timeout=60,
) as client:

    def post(path, body):
        response = client.post(path, json=body)
        if not response.is_success:
            raise RuntimeError(f"Feishu setup {path}: HTTP {response.status_code}")
        return response.json()

    prefix = "/api/enterprise/channels"
    binding = post(
        prefix,
        {
            "tenant_id": actor["tenantId"],
            "agent_id": credentials["agentId"],
            "channel": "feishu",
            "name": "SOP1014 isolated acceptance",
        },
    )
    configured = post(
        f"{prefix}/{binding['id']}/feishu/credentials",
        {
            "tenant_id": actor["tenantId"],
            "app_id": dotenv["FEISHU_APP_ID"],
            "app_secret": dotenv["FEISHU_APP_SECRET"],
        },
    )
    (root / "staffdeck-feishu-binding.json").write_text(
        json.dumps(
            {
                "bindingId": binding["id"],
                "status": configured["status"],
                "botName": configured.get("bot_name"),
                "connected": configured.get("connected"),
            },
            indent=2,
        )
    )
    print("Feishu test binding configured:", binding["id"], configured["status"])
