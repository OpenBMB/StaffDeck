"""Adapt empty identity placeholders without inventing model tool names.

Some compatible Chat Completions gateways send name="" on argument-only
continuations. DSH interprets a present name as a replacement, not a placeholder.
The initial nonempty name/id stay on the wire; subsequent omissions preserve them.
"""
from copy import deepcopy


def normalize_tool_stream_chunk(chunk):
    result = deepcopy(chunk)
    for choice in result.get('choices') or []:
        if not isinstance(choice, dict):
            continue
        delta = choice.get('delta')
        if not isinstance(delta, dict):
            continue
        for call in delta.get('tool_calls') or []:
            if not isinstance(call, dict):
                continue
            if call.get('id') in ('', None):
                call.pop('id', None)
            function = call.get('function')
            if isinstance(function, dict) and function.get('name') in ('', None):
                function.pop('name', None)
    return result
