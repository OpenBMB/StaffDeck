import logging
from types import SimpleNamespace as NS
from app.channels.feishu_runtime import _ingress_diagnostic, _normalize_event


def test_diagnostics_do_not_log_raw_identifiers_codes_or_content():
    lines = []
    class Capture(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())
    logger = logging.getLogger('staffdeck.feishu.ingress')
    saved = logger.handlers[:], logger.level, logger.propagate
    logger.handlers = [Capture()]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        event = NS(header=NS(event_id='private-event-id', app_id='private-app', tenant_key='private-tenant', token='private-token'),
                   event=NS(sender=NS(sender_type='user', sender_id=NS(open_id='private-user')),
                            message=NS(message_type='text', chat_type='p2p', content='/绑定 123456')))
        _ingress_diagnostic('binding-test', 'event_received', event=event)
        _ingress_diagnostic('binding-test', 'normalize_drop', event=event, reason='private-token')
        text = '\n'.join(lines)
        assert 'event_received' in text and 'p2p' in text and 'event_ref' in text
        for secret in ('private-event-id', 'private-app', 'private-tenant', 'private-token', 'private-user', '123456', '/绑定'):
            assert secret not in text
    finally:
        logger.handlers, logger.level, logger.propagate = saved


def test_normalization_reports_missing_structure_without_changing_drop_behavior():
    reasons = []
    assert _normalize_event(NS(header=None, event=None), bot_open_id='bot', on_drop=reasons.append) is None
    assert reasons == ['missing_structure']
