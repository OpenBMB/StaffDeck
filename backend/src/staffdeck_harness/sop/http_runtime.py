"""StaffDeck lifecycle adapter; graph decisions and revisions belong to Runtime."""

from __future__ import annotations

from copy import deepcopy

from .runtime_client import RuntimeClient, RuntimeError


class HttpSopRuntime:
    def __init__(self, client: RuntimeClient, dependencies):
        self.client, self.dependencies = client, dependencies

    def list_published_skills(self, tenant_id, agent_id=None):
        raise RuntimeError(
            "SOURCE_UNAVAILABLE", "Definitions must be supplied by the authorized host"
        )

    def get_active_skill(self, tenant_id, skill_id, agent_id=None):
        raise RuntimeError(
            "SOURCE_UNAVAILABLE", "Definitions must be supplied by the authorized host"
        )

    def drop_unavailable_skill_state(self, tenant_id, session, skills):
        if session.active_skill_id and not any(
            s.skill_id == session.active_skill_id for s in skills
        ):
            raise RuntimeError("SOP_NOT_AVAILABLE", "Active SOP is not authorized")
        return False

    @staticmethod
    def _id(session, task_id):
        import json

        return json.dumps(
            ["staffdeck", session.tenant_id, session.id, task_id], separators=(",", ":")
        )

    @staticmethod
    def _project(session, state):
        pointer = dict(session.awaiting_input_json or {}).get("runtime_session_id")
        task_id = dict(session.awaiting_input_json or {}).get("task_id")
        local_id = (
            dict(session.awaiting_input_json or {}).get("local_sop_id") or session.active_skill_id
        )
        session.active_skill_id = local_id
        session.active_step_id = state["active_node_id"]
        session.slots_json = deepcopy(state["slots"])
        session.status = state["status"]
        session.awaiting_input_json = {
            **deepcopy(state.get("pending_wait") or {}),
            "runtime_session_id": pointer,
            "local_sop_id": local_id,
            "task_id": task_id,
            "runtime_revision": state["revision"],
        }
        if session.awaiting_input_json.get("kind") == "approval":
            session.awaiting_input_json["kind"] = "handoff"

    def activate_frame(self, session, row, skills):
        if (row.tenant_id, row.session_id) != (session.tenant_id, session.id):
            raise RuntimeError("SESSION_SCOPE_MISMATCH", "Task belongs to another session")
        skill = next((s for s in skills if s.skill_id == row.skill_id), None)
        if skill is None or row.kind != "sop":
            return None
        runtime_id = self._id(session, row.task_id)
        from app.skills.sop_management import runtime_sop_id

        self.client.operation(
            runtime_id,
            "prepare",
            request_id=f"activate:{row.task_id}",
            turn_id=row.task_id,
            sopId=runtime_sop_id(session.tenant_id, session.agent_id, skill.skill_id),
            version=skill.version,
        )
        session.awaiting_input_json = {
            "task_id": row.task_id,
            "runtime_session_id": runtime_id,
            "local_sop_id": skill.skill_id,
        }
        self._project(session, self.client.session(runtime_id)["state"])
        # The compiler sees the Runtime's pinned, expanded graph.
        pinned = self.client.session(runtime_id)["definition"]
        skill.content_json = deepcopy(pinned["content"])
        return skill

    def restore_task_frame(self, session, frame):
        runtime_id = self._id(session, frame["task_id"])
        snapshot = self.client.session(runtime_id)
        session.awaiting_input_json = {
            "runtime_session_id": runtime_id,
            "task_id": frame["task_id"],
            "local_sop_id": frame["skill_id"],
        }
        self._project(session, snapshot["state"])
        return session

    def complete_current_skill(self, session):
        pointer = dict(session.awaiting_input_json or {}).get("runtime_session_id")
        if not pointer or self.client.session(pointer)["state"]["status"] != "completed":
            raise RuntimeError("SOP_STATE_INACTIVE", "Only Runtime can complete a SOP")
        session.active_skill_id = session.active_step_id = None
        session.slots_json, session.awaiting_input_json = {}, None
        return session

    def after_execution(
        self, tenant_id, session, skill, requirement, result, router_decision, *, remaining_actions
    ):
        from .contracts import SopAdvance
        from .results import step_result

        if tenant_id != session.tenant_id:
            raise RuntimeError("SESSION_SCOPE_MISMATCH", "Result belongs to another tenant")
        if result.status == "action_budget":
            return SopAdvance(step_result(result), False)
        runtime_id = dict(session.awaiting_input_json or {}).get("runtime_session_id") or self._id(
            session, requirement.task_frame_id
        )
        snapshot = self.client.session(runtime_id)
        state = snapshot["state"]
        executed_node = (requirement.sop_context.get("step") or {}).get("node_id")
        if executed_node and executed_node != state["active_node_id"]:
            raise RuntimeError(
                "SOP_REVISION_CONFLICT", "Executed node no longer matches Runtime state"
            )
        turn_id = requirement.execution_loop_id
        if not turn_id:
            raise RuntimeError("SOP_SCOPE_REQUIRED", "Host execution identifier is required")
        if state["status"] == "awaiting_user":
            resumed = self.client.operation(
                runtime_id,
                "resume",
                request_id=f"user-resume:{turn_id}:{state['revision']}",
                turn_id=turn_id,
                revision=state["revision"],
                waitId=state["pending_wait"]["wait_id"],
                response={"source": "user", "message": requirement.current_user_message},
            )
            state = resumed["state"]
        receipts = []
        declared = {
            r["capability_id"]
            for r in snapshot["definition"].get("capability_requirements", [])
            if r.get("node_id") in {None, state["active_node_id"]}
        }
        node = next(
            n
            for n in snapshot["definition"]["content"]["nodes"]
            if n["node_id"] == state["active_node_id"]
        )
        if node.get("type") == "handoff" and result.status == "awaiting_user":
            result.status = "handoff"
        declared.update(
            a.split(":", 1)[1]
            for a in node.get("allowed_actions", [])
            if a.startswith("call_tool:")
        )
        for item in result.capability_results:
            name = item.get("capability_id") or item.get("tool_name")
            descriptor = next(
                (d for d in [*requirement.capability_manifest.available,
                             *requirement.capability_manifest.catalog]
                 if name in {d.name, d.capability_id}), None
            )
            aliases = [name]
            if descriptor:
                aliases.extend([descriptor.capability_id, descriptor.name])
            names = [alias for alias in dict.fromkeys(aliases) if alias in declared]
            if not names:
                continue
            operation = item.get("operation_id")
            if not operation:
                raise RuntimeError(
                    "RECEIPT_INVALID", "Capability result lacks host operation identifier"
                )
            for index, name in enumerate(names):
                receipts.append(
                    {
                        "capability_id": name,
                        "operation_id": operation if index == 0 else f"{operation}:{name}",
                        "node_id": state["active_node_id"],
                        "session_id": runtime_id,
                        "turn_id": turn_id,
                        "status": "succeeded" if item.get("success") else "failed",
                        "result": item.get("data") if isinstance(item.get("data"), dict) else None,
                        "evidence": {"host_operation_id": operation},
                    }
                )
        knowledge_operations = [
            item.get("operation_id")
            for item in result.capability_results
            if item.get("tool_name") == "knowledge_search"
            and item.get("success") is True
            and item.get("operation_id")
        ]
        if knowledge_operations:
            evidence = [*result.citations]
            for pack in result.evidence_results:
                evidence.extend([*(pack.get("chunks") or []), *(pack.get("evidence_pack") or [])])
            for identifier in sorted(
                {
                    str(item["knowledge_base_id"])
                    for item in evidence
                    if item.get("knowledge_base_id")
                }
                & declared
            ):
                receipts.append(
                    {
                        "capability_id": identifier,
                        "operation_id": f"{knowledge_operations[0]}:{identifier}",
                        "session_id": runtime_id,
                        "turn_id": turn_id,
                        "node_id": state["active_node_id"],
                        "status": "succeeded",
                        "evidence": {"knowledge_base_id": identifier},
                    }
                )
        response = self.client.operation(
            runtime_id,
            "submit",
            request_id=f"submit:{turn_id}:{state['active_node_id']}:{state['revision']}:{result.action_count}",
            turn_id=turn_id,
            revision=state["revision"],
            proposal={
                "status": result.status,
                "reply": result.reply_fragment,
                "slot_updates": result.slot_updates,
                "next_node_id": result.next_step_id,
                "structured_result": result.structured_result,
            },
            receipts=receipts,
        )
        next_state = response["state"]
        session.awaiting_input_json = {
            **dict(session.awaiting_input_json or {}),
            "runtime_session_id": runtime_id,
        }
        self._project(session, next_state)
        projected = step_result(result)
        if next_state["status"] == "handoff":
            mapping = dict(session.awaiting_input_json or {})
            self.dependencies.create_handoff(tenant_id, session, skill, projected)
            session.awaiting_input_json = {**mapping, **dict(session.awaiting_input_json or {})}
            from .results import append_session_handoff_artifact

            append_session_handoff_artifact(result, session)
        elif next_state["status"] == "completed":
            self.complete_current_skill(session)
        elif next_state["status"] == "active" and remaining_actions <= 0:
            # The node completed, but the task still has a successor to execute.
            result.status = "action_budget"
        return SopAdvance(projected, next_state["status"] == "active" and remaining_actions > 0)
