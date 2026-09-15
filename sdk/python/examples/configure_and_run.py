"""Configure an existing agent with a tool and SOP; publish only with --publish.

Run from the repository root after installing sdk/python. Each write is explicit;
on interruption, use the emitted IDs to reconcile rather than rerunning blindly.
"""

from __future__ import annotations

import argparse
import json
import os
from contextlib import closing
from uuid import uuid4

from staffdeck import StaffDeck


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-id", required=True, help="Use a disposable development agent")
    parser.add_argument("--tool-url", required=True, help="Your approved, read-only HTTPS endpoint")
    parser.add_argument("--publish", action="store_true", help="Explicitly publish then run the SOP")
    args = parser.parse_args()
    suffix = uuid4().hex[:12]
    with StaffDeck(
        base_url=os.environ["STAFFDECK_BASE_URL"], api_key=os.environ["STAFFDECK_API_KEY"],
    ) as sdk:
        tool = sdk.tools.create(args.agent_id, {
            "name": f"partner_lookup_{suffix}", "method": "GET", "url": args.tool_url,
            "capability_scope": "general",
        })
        print(json.dumps({"tool_id": tool.data["id"]}), flush=True)
        card = {
            "skill_id": f"partner_policy_{suffix}", "name": "Partner policy lookup", "version": "1.0.0",
            "description": "Answer policy questions with the configured lookup tool",
            "trigger_intents": ["Look up partner policy"],
            "nodes": [{
                "node_id": "answer", "type": "respond", "name": "Answer",
                "instruction": "Look up the policy and answer using the returned facts.",
                "capability_refs": {
                    "tool_ids": [tool.data["id"]], "general_skill_ids": [], "knowledge_base_ids": [],
                },
            }],
            "edges": [], "start_node_id": "answer", "terminal_node_ids": ["answer"],
        }
        draft = sdk.sops.create(args.agent_id, card, idempotency_key=f"draft-{suffix}")
        sop_id, draft_id = draft.data["sop_id"], draft.data["id"]
        print(json.dumps({"sop_id": sop_id, "draft_id": draft_id, "etag": draft.etag}), flush=True)
        checked = sdk.sops.validate(args.agent_id, sop_id, draft_id)
        if not checked.data["valid"]:
            raise SystemExit("Draft validation failed; inspect it before publishing.")
        if not args.publish:
            print("Draft validated but NOT published. Review it in StaffDeck before publishing.")
            return
        sdk.sops.publish(args.agent_id, sop_id, draft_id)
        session = sdk.sessions.create(args.agent_id, {"title": "Partner SDK example"})
        receipt = sdk.runs.create(args.agent_id, {
            "input": "Look up partner policy", "session_id": session.data["id"],
            "session_mode": "stateful",
        }, idempotency_key=f"run-{suffix}")
        run_id = receipt.data["id"]
        print(json.dumps({"run_id": run_id, "status": receipt.data["status"]}), flush=True)
        with closing(sdk.runs.events(run_id)) as events:
            for event in events:
                print(json.dumps({"id": event.id, "event": event.event}), flush=True)
        print(json.dumps(sdk.runs.wait(run_id).data), flush=True)


if __name__ == "__main__":
    main()
