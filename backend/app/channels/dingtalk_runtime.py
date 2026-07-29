from __future__ import annotations

from typing import Any

from dingtalk_stream import AckMessage, CallbackHandler

from app.channels.adapters.dingtalk import normalize_dingtalk_message
from app.channels.service_dingtalk_inbox import stage_dingtalk_inbound


class DingTalkCallbackHandler(CallbackHandler):
    def __init__(self, *, db_engine, binding_id: str, expected_revision: int, client_id: str):
        super().__init__()
        self.db_engine = db_engine
        self.binding_id = binding_id
        self.expected_revision = expected_revision
        self.client_id = client_id

    async def process(self, message):
        raw: dict[str, Any] = dict(message.data or {})
        tenant_key = str(raw.get("chatbotCorpId") or raw.get("senderCorpId") or "").strip()
        inbound = normalize_dingtalk_message(raw, account_scope=tenant_key)
        if inbound is None:
            return AckMessage.STATUS_OK, "ignored"
        result = stage_dingtalk_inbound(
            db_engine=self.db_engine,
            binding_id=self.binding_id,
            expected_revision=self.expected_revision,
            client_id=self.client_id,
            tenant_key=tenant_key,
            inbound=inbound,
        )
        if result.should_ack:
            from app.channels.service_intake import wake_staged_inbound_worker

            wake_staged_inbound_worker()
            return AckMessage.STATUS_OK, result.disposition.value
        return AckMessage.STATUS_SYSTEM_EXCEPTION, result.error_code or "staging_failed"
