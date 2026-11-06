import json
from copy import deepcopy

import pytest
from app.core.slot_hydration_policy import SlotHydrationPolicy
from app.core.task_request_compiler import CapabilityManifest, TaskRequestCompiler
from app.db.models import ChatSession, Skill
from app.session.session_schema import PlannedTaskFrame
from staffdeck_harness.sop.fields import slot_field_names
from staffdeck_harness.sop.graph import GraphRules

DESCRIPTOR = {'field': 'reimbursement_amount', 'name': '报销金额', 'type': 'number', 'required': True}


@pytest.mark.parametrize('field', [DESCRIPTOR, str(DESCRIPTOR), json.dumps(DESCRIPTOR), 'reimbursement_amount'])
def test_field_descriptors_resolve_to_the_declared_key(field):
    assert slot_field_names([field, 'reimbursement_amount']) == ['reimbursement_amount']


@pytest.mark.parametrize('field', ["{'field': ''}", '{"name":"amount"}', '{not a descriptor}', "{'field': __import__('os').system('false')}"])
def test_invalid_descriptors_remain_required_and_are_never_executed(field):
    assert slot_field_names([field]) == [field]


def test_field_descriptors_never_execute_python_expressions(tmp_path):
    marker = tmp_path / 'must-not-exist'
    text = "{'field': __import__('pathlib').Path(" + repr(str(marker)) + ").write_text('unexpected')}"
    assert slot_field_names([text]) == [text]
    assert not marker.exists()


@pytest.mark.parametrize('amount', [80, 100, 120])
def test_amount_fields_compile_and_complete_without_mutating_the_definition(amount):
    raw = str(DESCRIPTOR)
    content = {'skill_id': 'expense', 'name': 'Expense', 'start_node_id': 'collect',
        'required_info': [raw], 'terminal_node_ids': ['reply'],
        'nodes': [{'node_id': 'collect', 'name': 'Amount', 'expected_user_info': [raw]},
                  {'node_id': 'reply', 'name': 'Reply', 'allowed_actions': ['answer_user']}],
        'edges': [{'source_node_id': 'collect', 'next_node_id': 'reply'}]}
    original = deepcopy(content)
    skill = Skill(id='expense', tenant_id='tenant', skill_id='expense', name='Expense', content_json=content)
    session = ChatSession(id='session', tenant_id='tenant', active_skill_id='expense', active_step_id='collect',
        slots_json={'reimbursement_amount': amount})
    frame = PlannedTaskFrame(task_id='frame', kind='sop', target_skill_id='expense', target_step_id='collect')
    requirement = TaskRequestCompiler().compile(frame, session, skill, CapabilityManifest())
    assert requirement.expected_slots == ['reimbursement_amount']
    assert requirement.required_slots == []
    assert requirement.known_slots == {'reimbursement_amount': amount}
    assert SlotHydrationPolicy.skill_expected_fields(skill) == {'reimbursement_amount'}
    assert GraphRules.terminal_position(content, 'reply', session.slots_json)
    assert content == original
    session.slots_json = {}
    missing = TaskRequestCompiler().compile(frame, session, skill, CapabilityManifest())
    assert missing.required_slots == ['reimbursement_amount']
    assert not GraphRules.terminal_position(content, 'reply', {})
