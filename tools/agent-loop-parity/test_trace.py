"""Focused contracts for deterministic AgentLoop trace comparison."""
from __future__ import annotations

import unittest

from trace import compare_traces, validate_trace_expectations


def lifecycle(kind: str, name: str, call_id: str, sequence: int, *, concurrency_safe: bool) -> dict[str, object]:
    record: dict[str, object] = {
        "kind": kind,
        "scenarioId": "parallel-tools",
        "q": "compare",
        "sequence": sequence,
        "toolCallId": call_id,
        "concurrencySafe": concurrency_safe,
    }
    if kind in {"tool.call", "tool.start", "tool.finish"}:
        record["name"] = name
    else:
        record["result"] = {"type": "success", "toolName": name, "toolCallId": call_id}
    return record


class ConcurrentToolTraceTests(unittest.TestCase):
    def test_concurrent_permission_interleaving_preserves_per_tool_causality(self) -> None:
        def permission(name: str, sequence: int) -> dict[str, object]:
            return {
                "kind": "permission.decision",
                "scenarioId": "parallel-tools",
                "q": "compare",
                "sequence": sequence,
                "toolName": name,
                "allowed": True,
            }

        native = [
            permission("lookup", 0),
            permission("summarize", 1),
            lifecycle("tool.call", "lookup", "call-lookup", 2, concurrency_safe=True),
            lifecycle("tool.call", "summarize", "call-summarize", 3, concurrency_safe=True),
            lifecycle("tool.result", "lookup", "call-lookup", 4, concurrency_safe=True),
            lifecycle("tool.result", "summarize", "call-summarize", 5, concurrency_safe=True),
        ]
        sidecar = [
            permission("lookup", 0),
            lifecycle("tool.call", "lookup", "call-lookup", 1, concurrency_safe=True),
            permission("summarize", 2),
            lifecycle("tool.call", "summarize", "call-summarize", 3, concurrency_safe=True),
            lifecycle("tool.result", "summarize", "call-summarize", 4, concurrency_safe=True),
            lifecycle("tool.result", "lookup", "call-lookup", 5, concurrency_safe=True),
        ]
        self.assertEqual(compare_traces(native, sidecar), [])

    def test_permission_after_its_tool_call_is_not_normalized(self) -> None:
        native = [
            {"kind": "permission.decision", "toolName": "lookup", "allowed": True},
            lifecycle("tool.call", "lookup", "call-lookup", 1, concurrency_safe=True),
            lifecycle("tool.call", "summarize", "call-summarize", 2, concurrency_safe=True),
            {"kind": "permission.decision", "toolName": "summarize", "allowed": True},
            lifecycle("tool.result", "lookup", "call-lookup", 4, concurrency_safe=True),
            lifecycle("tool.result", "summarize", "call-summarize", 5, concurrency_safe=True),
        ]
        sidecar = [
            {"kind": "permission.decision", "toolName": "lookup", "allowed": True},
            lifecycle("tool.call", "lookup", "call-lookup", 1, concurrency_safe=True),
            {"kind": "permission.decision", "toolName": "summarize", "allowed": True},
            lifecycle("tool.call", "summarize", "call-summarize", 3, concurrency_safe=True),
            lifecycle("tool.result", "lookup", "call-lookup", 4, concurrency_safe=True),
            lifecycle("tool.result", "summarize", "call-summarize", 5, concurrency_safe=True),
        ]
        self.assertTrue(compare_traces(native, sidecar))

    def test_concurrent_completion_order_is_compared_by_call_identity(self) -> None:
        native = [
            lifecycle("tool.call", "lookup", "call-lookup", 0, concurrency_safe=True),
            lifecycle("tool.call", "summarize", "call-summarize", 1, concurrency_safe=True),
            lifecycle("tool.result", "lookup", "call-lookup", 2, concurrency_safe=True),
            lifecycle("tool.result", "summarize", "call-summarize", 3, concurrency_safe=True),
        ]
        sidecar = [
            lifecycle("tool.call", "lookup", "call-lookup", 0, concurrency_safe=True),
            lifecycle("tool.call", "summarize", "call-summarize", 1, concurrency_safe=True),
            lifecycle("tool.result", "summarize", "call-summarize", 2, concurrency_safe=True),
            lifecycle("tool.result", "lookup", "call-lookup", 3, concurrency_safe=True),
        ]
        self.assertEqual(compare_traces(native, sidecar), [])

    def test_non_concurrent_completion_order_remains_semantic(self) -> None:
        native = [
            lifecycle("tool.call", "lookup", "call-lookup", 0, concurrency_safe=False),
            lifecycle("tool.call", "summarize", "call-summarize", 1, concurrency_safe=False),
            lifecycle("tool.result", "lookup", "call-lookup", 2, concurrency_safe=False),
            lifecycle("tool.result", "summarize", "call-summarize", 3, concurrency_safe=False),
        ]
        sidecar = [
            lifecycle("tool.call", "lookup", "call-lookup", 0, concurrency_safe=False),
            lifecycle("tool.call", "summarize", "call-summarize", 1, concurrency_safe=False),
            lifecycle("tool.result", "summarize", "call-summarize", 2, concurrency_safe=False),
            lifecycle("tool.result", "lookup", "call-lookup", 3, concurrency_safe=False),
        ]
        self.assertTrue(compare_traces(native, sidecar))

    def test_duplicate_identity_is_not_normalized(self) -> None:
        native = [
            lifecycle("tool.call", "lookup", "call-duplicate", 0, concurrency_safe=True),
            lifecycle("tool.call", "summarize", "call-duplicate", 1, concurrency_safe=True),
            lifecycle("tool.result", "lookup", "call-duplicate", 2, concurrency_safe=True),
            lifecycle("tool.result", "summarize", "call-duplicate", 3, concurrency_safe=True),
        ]
        sidecar = [
            lifecycle("tool.call", "lookup", "call-duplicate", 0, concurrency_safe=True),
            lifecycle("tool.call", "summarize", "call-duplicate", 1, concurrency_safe=True),
            lifecycle("tool.result", "summarize", "call-duplicate", 2, concurrency_safe=True),
            lifecycle("tool.result", "lookup", "call-duplicate", 3, concurrency_safe=True),
        ]
        self.assertTrue(compare_traces(native, sidecar))


class TimelineTraceTests(unittest.TestCase):
    def test_empty_slots_are_distinct_from_an_unrecorded_slot_state(self) -> None:
        records = [{
            "kind": "terminal",
            "scenarioId": "empty-slots",
            "q": "compare",
            "sequence": 0,
            "outcome": "completed",
            "taskFrame": {"slots": {}},
        }]
        self.assertEqual(
            validate_trace_expectations(
                records,
                {"expected": {"slots": {}}},
                "staffdeck",
            ),
            [],
        )

    def test_terminal_forced_sop_version_is_an_oracle_value(self) -> None:
        records = [{
            "kind": "terminal",
            "scenarioId": "scheduled-sop",
            "q": "compare",
            "sequence": 0,
            "outcome": "completed",
            "forcedSopVersion": "7",
        }]
        self.assertEqual(
            validate_trace_expectations(
                records,
                {"expected": {"forcedSopVersion": "7"}},
                "staffdeck",
            ),
            [],
        )

    def test_terminal_task_frame_budget_is_an_oracle_fallback(self) -> None:
        records = [{
            "kind": "taskframe",
            "scenarioId": "knowledge-budget",
            "q": "compare",
            "sequence": 0,
            "taskFrame": {"status": "completed"},
        }, {
            "kind": "terminal",
            "scenarioId": "knowledge-budget",
            "q": "compare",
            "sequence": 1,
            "outcome": "completed",
            "taskFrame": {"knowledgeBudget": {"successfulCalls": 1, "remaining": 1}},
        }]
        self.assertEqual(
            validate_trace_expectations(
                records,
                {"expected": {"knowledgeBudget": {"successfulCalls": 1, "remaining": 1}}},
                "staffdeck",
            ),
            [],
        )

    def test_generated_pending_task_source_turn_is_not_semantic(self) -> None:
        native = [{
            "kind": "session.state",
            "scenarioId": "pending-task",
            "q": "compare",
            "sequence": 0,
            "pendingTasks": [{
                "task_id": "task-1",
                "status": "pending",
                "source_turn_id": "msg_aaaaaaaaaaaaaaaa",
                "created_at": "2026-09-20T00:00:00",
                "updated_at": "2026-09-20T00:00:01",
            }],
        }]
        sidecar = [{
            **native[0],
            "pendingTasks": [{
                "task_id": "task-1",
                "status": "pending",
                "source_turn_id": "msg_bbbbbbbbbbbbbbbb",
                "created_at": "2026-09-20T01:00:00",
                "updated_at": "2026-09-20T01:00:01",
            }],
        }]
        self.assertEqual(compare_traces(native, sidecar), [])

    def test_generated_timeline_turn_id_is_not_semantic(self) -> None:
        native = [{
            "kind": "model.request",
            "scenarioId": "timeline",
            "q": "compare",
            "sequence": 0,
            "messages": [{
                "role": "assistant",
                "content": [{
                    "type": "tool_call",
                    "id": "call-1",
                    "timeline": {
                        "version": 1,
                        "turnId": "native-generated-turn",
                        "id": "tool:call-1",
                        "order": 0,
                        "revision": 1,
                    },
                }],
            }],
        }]
        sidecar = [{
            **native[0],
            "messages": [{
                "role": "assistant",
                "content": [{
                    "type": "tool_call",
                    "id": "call-1",
                    "timeline": {
                        "version": 1,
                        "turnId": "sidecar-generated-turn",
                        "id": "tool:call-1",
                        "order": 0,
                        "revision": 1,
                    },
                }],
            }],
        }]
        self.assertEqual(compare_traces(native, sidecar), [])

    def test_generated_model_block_identity_is_not_semantic(self) -> None:
        native = [{
            "kind": "model.request",
            "scenarioId": "timeline",
            "q": "compare",
            "sequence": 0,
            "messages": [{
                "role": "assistant",
                "content": [{
                    "type": "text",
                    "text": "same",
                    "blockId": "11111111-1111-4111-8111-111111111111:text:0",
                    "timeline": {
                        "version": 1,
                        "turnId": "native-turn",
                        "id": "11111111-1111-4111-8111-111111111111:text:0",
                        "order": 0,
                        "revision": 1,
                    },
                }],
            }],
        }]
        sidecar = [{
            **native[0],
            "messages": [{
                "role": "assistant",
                "content": [{
                    "type": "text",
                    "text": "same",
                    "blockId": "22222222-2222-4222-8222-222222222222:text:0",
                    "timeline": {
                        "version": 1,
                        "turnId": "sidecar-turn",
                        "id": "22222222-2222-4222-8222-222222222222:text:0",
                        "order": 0,
                        "revision": 1,
                    },
                }],
            }],
        }]
        self.assertEqual(compare_traces(native, sidecar), [])

    def test_non_timeline_turn_id_remains_semantic(self) -> None:
        native = [{
            "kind": "checkpoint",
            "scenarioId": "turn-identity",
            "q": "compare",
            "sequence": 0,
            "messages": [{"turnId": "native-turn"}],
        }]
        sidecar = [{
            **native[0],
            "messages": [{"turnId": "sidecar-turn"}],
        }]
        self.assertTrue(compare_traces(native, sidecar))


if __name__ == "__main__":
    unittest.main()
