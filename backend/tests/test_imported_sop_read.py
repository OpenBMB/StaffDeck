import pytest
from pydantic import ValidationError
from app.api.skills import _read_persisted_skill_content
from app.skills.skill_schema import skill_card_from_persisted


def test_incomplete_imported_sop_can_be_inspected_but_not_executed():
    value={"skill_id":"example","name":"Example","version":"1.0.0","business_domain":"test",
           "nodes":[{"node_id":"collection","name":"Collect","type":"subflow","instruction":"original draft"}],
           "start_node_id":"collection","terminal_node_ids":["collection"],"edges":[]}
    content,errors=_read_persisted_skill_content(value)
    assert content==value and errors
    assert "sub_sop_id" in errors[0]
    with pytest.raises(ValidationError):
        skill_card_from_persisted(value)
