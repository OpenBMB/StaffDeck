"""Decode engine/MCP result blocks into one observable tool outcome."""
import json


def tool_outcome(block, engine_error=None):
    text = '\n'.join(str(item.get('text') or '') for item in block.get('content') or []
                     if isinstance(item, dict) and item.get('type') == 'text')
    encoded = text.strip()
    if encoded.startswith('Error: '):
        encoded = encoded[len('Error: '):].lstrip()
    try:
        payload = json.loads(encoded)
    except (TypeError, ValueError):
        payload = None
    payload = payload if isinstance(payload, dict) else {}
    success = not block.get('isError') and payload.get('success') is not False
    error = payload.get('error') if isinstance(payload.get('error'), dict) else {}
    if not success and not error:
        error = {**(engine_error if isinstance(engine_error, dict) else {}),
                 'message': text[:4000] or 'Engine tool call failed'}
        error.setdefault('code', 'ENGINE_TOOL_ERROR')
    if error.get('code') == 'UNKNOWN_TOOL':
        error = {**error, 'executed': False, 'retryable': False}
    return {'success': success, 'result': payload.get('data', payload) if payload else text,
            'error': error or None, 'receipt': payload.get('receipt')}
