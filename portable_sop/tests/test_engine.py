import pytest

from app.session.slot_policy import slot_is_filled
from staffdeck_sop_runtime.original_runtime import SopRuntimeError, prepare, submit


BUNDLE = {
    "sops": [
        {
            "id": "onboard",
            "version": "1",
            "name": "Onboard",
            "content": {
                "start_node_id": "collect",
                "nodes": [
                    {
                        "node_id": "collect",
                        "instruction": "Collect the user's name.",
                        "expected_user_info": ["name"],
                        "allowed_actions": ["call_tool:read_file"],
                    },
                    {"node_id": "done", "instruction": "Finish."},
                ],
                "edges": [{"source_node_id": "collect", "next_node_id": "done"}],
                "terminal_node_ids": ["done"],
            },
        }
    ]
}


def test_prepare_activates_selected_sop():
    prepared = prepare(BUNDLE, {"selected_skill_id": "onboard"})

    assert prepared["state"]["active_step_id"] == "collect"
    assert prepared["step"]["requiredToolNames"] == ["read_file"]


def test_prepare_projects_existing_sub_sop_metadata_without_implementing_a_new_lifecycle():
    bundle = {
        "sops": [{
            "id": "parent",
            "content": {
                "start_node_id": "delegate",
                "nodes": [{"node_id": "delegate", "sub_sop_id": "child"}],
            },
        }, {
            "id": "child",
            "content": {"start_node_id": "child_step", "nodes": [{"node_id": "child_step"}]},
        }],
    }

    prepared = prepare(bundle, {"selected_skill_id": "parent"})

    assert prepared["step"]["subSopId"] == "child"
    assert prepared["state"]["active_skill_id"] == "parent"


def test_submit_uses_staffdeck_slot_policy_before_advancing():
    state = prepare(BUNDLE, {"selected_skill_id": "onboard"})["state"]

    # StaffDeck's existing submission validator treats whitespace as unfilled.
    # The adapter must expose that rejection, rather than maintaining a copy.
    try:
        submit(
            BUNDLE, state,
            {"status": "completed", "replyFragment": "done", "slotUpdates": {"name": "   "}},
            ["read_file"],
        )
    except SopRuntimeError as error:
        assert error.code == "REQUIRED_SLOT_MISSING"
    else:
        raise AssertionError("whitespace-only required slots must be rejected")

    advanced = submit(
        BUNDLE,
        state,
        {"status": "completed", "replyFragment": "done", "slotUpdates": {"name": "Ada"}},
        ["read_file"],
    )

    assert advanced["state"]["active_step_id"] == "done"
    assert advanced["result"]["nextStepId"] == "done"


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", 0, False, [], {}, ["Ada"], {"name": "Ada"}, "Ada"],
)
def test_submit_matches_original_slot_filled_contract(value):
    """The sidecar must expose StaffDeck's slot policy without coercion."""
    state = prepare(BUNDLE, {"selected_skill_id": "onboard"})["state"]
    proposal = {"status": "completed", "replyFragment": "done", "slotUpdates": {"name": value}}

    if not slot_is_filled(value):
        with pytest.raises(SopRuntimeError) as error:
            submit(BUNDLE, state, proposal, ["read_file"])
        assert error.value.code == "REQUIRED_SLOT_MISSING"
        return

    advanced = submit(BUNDLE, state, proposal, ["read_file"])
    assert advanced["state"]["active_step_id"] == "done"
    assert advanced["state"]["slots_json"]["name"] == value


def test_submit_preserves_staffdeck_required_capability_rejection():
    state = prepare(BUNDLE, {"selected_skill_id": "onboard"})["state"]

    try:
        submit(BUNDLE, state, {"status": "completed", "replyFragment": "done", "slotUpdates": {"name": "Ada"}})
    except SopRuntimeError as error:
        assert error.code == "REQUIRED_CAPABILITY_NOT_INVOKED"
    else:
        raise AssertionError("existing StaffDeck validator must reject a missing required tool")


def test_submit_uses_existing_graph_default_transition_rules():
    bundle = {
        "sops": [{
            "id": "branch",
            "content": {
                "start_node_id": "start",
                "nodes": [{"node_id": "start"}, {"node_id": "yes"}, {"node_id": "no"}],
                "edges": [
                    {"source_node_id": "start", "next_node_id": "yes", "condition": "yes"},
                    {"source_node_id": "start", "next_node_id": "no", "condition": "no"},
                ],
            },
        }]
    }
    state = prepare(bundle, {"selected_skill_id": "branch"})["state"]

    submitted = submit(bundle, state, {"status": "completed", "replyFragment": "choose", "nextStepId": "yes"})

    assert submitted["state"]["active_step_id"] == "yes"


def test_submit_can_wait_for_user_without_all_required_slots():
    state = prepare(BUNDLE, {"selected_skill_id": "onboard"})["state"]

    waiting = submit(
        BUNDLE,
        state,
        {"status": "awaiting_user", "replyFragment": "What is your name?"},
    )

    assert waiting["state"]["status"] == "awaiting_user"
    assert waiting["state"]["awaiting_input_json"]["expected_fields"] == ["name"]

    advanced = submit(
        BUNDLE,
        waiting["state"],
        {"status": "completed", "replyFragment": "Thanks", "slotUpdates": {"name": "Ada"}},
        ["read_file"],
    )
    assert advanced["state"]["status"] == "active"
    assert advanced["state"]["active_step_id"] == "done"


def test_submit_rejects_undeclared_handoff():
    state = prepare(BUNDLE, {"selected_skill_id": "onboard"})["state"]

    rejected = submit(BUNDLE, state, {"status": "handoff", "replyFragment": "Escalating."})

    assert rejected["result"]["status"] == "failed"


def test_submit_preserves_declared_handoff_lifecycle_state():
    bundle = {
        "sops": [{
            "id": "handoff",
            "content": {
                "start_node_id": "approval",
                "nodes": [{"node_id": "approval", "type": "handoff"}],
                "terminal_node_ids": ["approval"],
            },
        }],
    }
    state = prepare(bundle, {"selected_skill_id": "handoff"})["state"]
    result = submit(bundle, state, {"status": "handoff", "replyFragment": "Approval required."})

    assert result["result"]["status"] == "handoff"
    assert result["state"]["status"] == "handoff"
    assert result["state"]["awaiting_input_json"] == {
        "kind": "handoff", "skill_id": "handoff", "step_id": "approval",
    }


@pytest.mark.parametrize("status", ["waiting_external_task", "blocked"])
def test_submit_preserves_nonterminal_wait_statuses(status):
    state = prepare(BUNDLE, {"selected_skill_id": "onboard"})["state"]
    result = submit(BUNDLE, state, {"status": status, "replyFragment": "Wait for a host result."})

    assert result["result"]["status"] == status
    assert result["state"]["status"] == status
    assert result["state"]["active_step_id"] == "collect"


def test_submit_rejects_invalid_next_step_from_original_graph_validator():
    state = prepare(BUNDLE, {"selected_skill_id": "onboard"})["state"]
    with pytest.raises(SopRuntimeError) as error:
        submit(
            BUNDLE,
            state,
            {"status": "completed", "replyFragment": "done", "slotUpdates": {"name": "Ada"}, "nextStepId": "missing"},
            ["read_file"],
        )
    assert error.value.code == "INVALID_TRANSITION"


def test_submit_does_not_reuse_a_successful_tool_receipt_from_a_prior_step():
    state = prepare(BUNDLE, {"selected_skill_id": "onboard", "successful_tool_names": ["read_file"]})["state"]
    with pytest.raises(SopRuntimeError) as error:
        submit(BUNDLE, state, {"status": "completed", "replyFragment": "done", "slotUpdates": {"name": "Ada"}})
    assert error.value.code == "REQUIRED_CAPABILITY_NOT_INVOKED"
