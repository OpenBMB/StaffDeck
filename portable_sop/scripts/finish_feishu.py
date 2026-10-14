"""Approve only the real marker-matched Feishu acceptance session and save evidence."""

import json
import os
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from dotenv import dotenv_values

root = Path(os.environ["SOP_ACCEPTANCE_ROOT"])
credentials = json.loads((root / "credentials.json").read_text())
binding = json.loads((root / "staffdeck-feishu-binding.json").read_text())["bindingId"]
with sqlite3.connect(root.parent / "staffdeck.sqlite") as db:
    db.row_factory = sqlite3.Row
    events = db.execute(
        "select * from channel_inbound_events where binding_id=? order by rowid desc",
        (binding,),
    ).fetchall()
    event = next(
        row
        for row in events
        if "SOP1014" in json.loads(row["payload_json"])["inbound"]["text"]
        and not json.loads(row["payload_json"])["inbound"]["text"].startswith("/")
    )
    inbound = json.loads(event["payload_json"])["inbound"]
    waits = db.execute(
        "select h.* from human_handoff_requests h join sessions s on s.id=h.session_id where s.channel_binding_id=? order by h.rowid desc",
        (binding,),
    ).fetchall()
    wait = next(row for row in waits if "SOP-1014" in row["pending_question"])
    session = db.execute(
        "select * from sessions where id=?", (wait["session_id"],)
    ).fetchone()
    assert session["channel_binding_id"] == binding
    projection = json.loads(session["awaiting_input_json"]) or {}
    pointer = projection.get("runtime_session_id")
    if not pointer:
        with sqlite3.connect(root.parent / "runtime.sqlite") as runtime_db:
            pointer = next(
                row[0]
                for row in runtime_db.execute("select session_id from sop_sessions")
                if session["id"] in row[0]
            )

with httpx.Client(timeout=300) as client:
    if wait["status"] == "pending":
        response = client.post(
            f"http://127.0.0.1:26390/api/chat/handoffs/{wait['id']}/reply",
            headers={"Authorization": f"Bearer {credentials['sop-reviewer']['token']}"},
            json={
                "tenant_id": credentials["sop-reviewer"]["tenantId"],
                "reply": "Approved SOP-1014. Complete the release and return RELEASE_ACCEPTANCE_COMPLETE.",
            },
        )
        assert response.is_success, response.status_code
    else:
        assert wait["status"] == "answered"
    for _ in range(180):
        state = client.get(
            f"http://127.0.0.1:26391/v1/sessions/{quote(pointer, safe='')}"
        ).json()["state"]
        with sqlite3.connect(root.parent / "staffdeck.sqlite") as db:
            db.row_factory = sqlite3.Row
            deliveries = [
                dict(row)
                for row in db.execute(
                    "select id,session_id,status,text,last_error from channel_deliveries where session_id=?",
                    (session["id"],),
                )
            ]
        if state["status"] == "completed" and any(
            row["status"] == "delivered"
            and "RELEASE_ACCEPTANCE_COMPLETE" in row["text"]
            for row in deliveries
        ):
            break
        time.sleep(1)
    assert state["status"] == "completed", state
    assert any(
        row["status"] == "delivered" and "RELEASE_ACCEPTANCE_COMPLETE" in row["text"]
        for row in deliveries
    ), deliveries
    dotenv = dotenv_values("/Users/a1/Desktop/.env")
    token = client.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={
            "app_id": dotenv["FEISHU_APP_ID"],
            "app_secret": dotenv["FEISHU_APP_SECRET"],
        },
    ).json()["tenant_access_token"]
    chat_id = inbound["raw"]["message"]["chat_id"]
    history = client.get(
        "https://open.feishu.cn/open-apis/im/v1/messages",
        headers={"Authorization": f"Bearer {token}"},
        params={
            "container_id_type": "chat",
            "container_id": chat_id,
            "sort_type": "ByCreateTimeDesc",
            "page_size": 20,
        },
    ).json()
    messages = [
        {"messageId": row["message_id"], "content": row.get("body", {}).get("content")}
        for row in history.get("data", {}).get("items", [])
    ]
    report = {
        "status": "PASS",
        "bindingId": binding,
        "sessionId": session["id"],
        "handoffId": wait["id"],
        "inboundEventId": event["event_id"],
        "inbound": inbound,
        "runtimeState": state,
        "deliveries": deliveries,
        "providerHistoryCode": history.get("code"),
        "providerMessages": messages,
    }
    (root / "staffdeck-feishu.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False)
    )
    print(
        "StaffDeck real Feishu acceptance PASS",
        session["id"],
        "revision",
        state["revision"],
    )
