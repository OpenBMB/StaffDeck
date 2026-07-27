import pytest

from app.core.agent_loop import _agent_identity_prompt
from app.db.models import AgentProfile


def _make_agent(metadata, name, desc, persona):
    agent=AgentProfile()
    agent.metadata_json=metadata
    agent.name=name
    agent.description=desc
    agent.persona_prompt=persona
    return agent

class TestAgentIdentityPrompt:
    def test_single_label_single_value(self):
        md={'role_name': '客服'}
        a=_make_agent(md,'客服A','','')
        result=_agent_identity_prompt(a)
        assert '\u5c97\u4f4d\uff1a\u5ba2\u670d' in result

    def test_dedup_merges_multi_values(self):
        md={'role_name': '\u5ba2\u670d', 'position': '\u9ad8\u7ea7\u5ba2\u670d', 'job_title': '\u7ec4\u957f'}
        a=_make_agent(md,'\u5ba2\u670dB','','')
        result=_agent_identity_prompt(a)
        assert '\u5c97\u4f4d\uff1a\u5ba2\u670d\uff1b\u9ad8\u7ea7\u5ba2\u670d\uff1b\u7ec4\u957f' in result

    def test_partial_values_skip_empty(self):
        md={'role_name': '\u5ba2\u670d', 'position': '', 'job_title': '\u7ec4\u957f'}
        a=_make_agent(md,'\u5ba2\u670dC','','')
        result=_agent_identity_prompt(a)
        assert '\u5c97\u4f4d\uff1a\u5ba2\u670d\uff1b\u7ec4\u957f' in result
        assert '\u5c97\u4f4d\uff1a\u5ba2\u670d\uff1b\uff1b\u7ec4\u957f' not in result

    def test_empty_metadata_returns_persona(self):
        a=_make_agent({},'\u6d4b\u8bd5Agent','','\u6211\u662f\u667a\u80fd\u52a9\u624b')
        result=_agent_identity_prompt(a)
        assert '\u6211\u662f\u667a\u80fd\u52a9\u624b' in result
        assert '\u5c97\u4f4d' not in result
