from types import SimpleNamespace as NS
import pytest

from staffdeck_harness.bridge.tool_stream import normalize_tool_stream_chunk
from staffdeck_harness.events.relay import SessionEventRelay
from staffdeck_harness.events.tool_results import tool_outcome


@pytest.mark.parametrize('empty', ['', None])
def test_empty_continuations_cannot_erase_a_valid_name_or_id(empty):
    first = {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'call-1', 'function': {'name': 'capability_describe', 'arguments': ''}}]}}]}
    follow = {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': empty, 'function': {'name': empty, 'arguments': '{"kind":"all"}'}}]}}]}
    result = normalize_tool_stream_chunk(follow)
    assert 'name' not in result['choices'][0]['delta']['tool_calls'][0]['function']
    assert 'id' not in result['choices'][0]['delta']['tool_calls'][0]
    assert follow['choices'][0]['delta']['tool_calls'][0]['function']['name'] == empty
    assert normalize_tool_stream_chunk(first) == first


def test_empty_first_chunk_does_not_guess_a_tool_from_its_arguments():
    chunk = {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'name': '', 'arguments': '{"kind":"knowledge_base"}'}}]}}]}
    assert 'name' not in normalize_tool_stream_chunk(chunk)['choices'][0]['delta']['tool_calls'][0]['function']


def test_engine_errors_retain_text_code_identity_and_elapsed_time(monkeypatch):
    monkeypatch.setattr('staffdeck_harness.events.relay._fanout', lambda *args: None)
    seen = []
    relay = SessionEventRelay('tenant', 'session', lambda event, data: seen.append((event, data)))
    relay({'type': 'tool/call', 'time': 1000, 'data': {'callId': 'c', 'name': '', 'arguments': '{"kind":"all"}'}})
    event = {'type': 'tool/result', 'time': 1025, 'data': {'error': {'code': 'UNKNOWN_TOOL'}, 'message': {'content': [
        {'type': 'tool-result', 'toolCallId': 'c', 'isError': True, 'content': [{'type': 'text', 'text': 'Error: unknown tool ""'}]}
    ]}}}
    relay(event)
    relay(event)
    finished = [data for name, data in seen if name == 'harness_tool_result']
    assert len(finished) == 1
    assert finished[0]['success'] is False
    assert finished[0]['error']['code'] == 'UNKNOWN_TOOL'
    assert finished[0]['error']['message'] == 'Error: unknown tool ""'
    assert finished[0]['duration_ms'] == 25 and finished[0]['iteration'] == 1
    assert finished[0]['call_id'] == 'c' and finished[0]['error']['executed'] is False


def test_pre_dispatch_failures_count_and_stop_after_three_attempts():
    from staffdeck_harness.bridge.control import ExecutionHost
    from app.core.capability_recovery import CapabilityRecovery
    host = object.__new__(ExecutionHost)
    host.capabilities = NS(slot=NS(finish=None))
    host.actions = host.engine_actions = 0
    host.max_actions = 32
    host.engine_calls = {}
    host.engine_results = set()
    host.engine_failure = None
    host.recovery = CapabilityRecovery()
    for i in range(3):
        host.observe_engine_event({'type': 'tool/call', 'data': {'callId': str(i), 'name': '', 'arguments': '{}'}})
        host.observe_engine_event({'type': 'tool/result', 'data': {'error': {'code': 'UNKNOWN_TOOL'},
            'message': {'content': [{'type': 'tool-result', 'toolCallId': str(i), 'isError': True}]}}})
    assert host.actions == 0 and host.total_actions == 3
    assert host.engine_failure['details']['attempts'] == 3
    host.actions = 3
    assert host.total_actions == 3


def test_persisted_history_uses_the_same_call_identity_and_failure_as_live_trace():
    from app.api.chat import _event_trace_line
    from app.db.models import AgentEvent
    from app.observability.session_timings import _trace_line_id
    common = {'task_frame_id': 'frame', 'call_id': 'call', 'tool_name': 'capability_describe'}
    start = AgentEvent(id='start', tenant_id='t', session_id='s', event_type='harness_action_created',
                       payload_json={**common, 'action': 'tool', 'iteration': 1})
    result = AgentEvent(id='end', tenant_id='t', session_id='s', event_type='harness_tool_result',
                        payload_json={**common, 'is_error': True, 'success': False, 'duration_ms': 500,
                                      'arguments': {'kind': 'all'}, 'result': 'failed',
                                      'error': {'code': 'UNKNOWN_TOOL', 'message': 'unknown tool'}})
    a, b = _event_trace_line(start, {}), _event_trace_line(result, {})
    assert a['id'] == b['id'] == _trace_line_id(start) == _trace_line_id(result)
    assert b['state'] == 'failed' and 'UNKNOWN_TOOL' in b['detail']
    assert b['language'] == 'json' and 'kind' in b['code']
