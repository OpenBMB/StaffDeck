from __future__ import annotations

import logging
from typing import Any

from app.channels.adapters.discord import normalize_discord_message
from app.channels.service_discord_inbox import stage_discord_inbound

logger = logging.getLogger(__name__)


class DiscordEventHandler:
    """把 discord.py 网关线程收到的 Message 归一化并暂存到 durable inbox。"""

    def __init__(
        self,
        *,
        db_engine,
        binding_id: str,
        expected_revision: int,
        bot_id: str,
    ) -> None:
        self.db_engine = db_engine
        self.binding_id = binding_id
        self.expected_revision = expected_revision
        self.bot_id = bot_id

    async def handle_message(self, message: Any) -> None:
        """message 为 discord.py 的 Message 对象或已序列化的 dict。"""
        if hasattr(message, "to_dict") or not isinstance(message, dict):
            raw = self._serialize(message)
        else:
            raw = message
        inbound = normalize_discord_message(raw, account_scope="")
        if inbound is None:
            return
        result = stage_discord_inbound(
            db_engine=self.db_engine,
            binding_id=self.binding_id,
            expected_revision=self.expected_revision,
            bot_id=self.bot_id,
            inbound=inbound,
        )
        if result.should_ack:
            from app.channels.service_intake import wake_staged_inbound_worker

            wake_staged_inbound_worker()

    def _serialize(self, message: Any) -> dict[str, Any]:
        """把 discord.py Message 对象序列化为 normalize 需要的 dict。"""
        author = getattr(message, "author", None)
        channel = getattr(message, "channel", None)
        guild = getattr(message, "guild", None)
        mentions = getattr(message, "mentions", None) or []
        return {
            "id": str(getattr(message, "id", "") or ""),
            "channel_id": str(getattr(channel, "id", "") or ""),
            "guild_id": str(getattr(guild, "id", "") or "") if guild else "",
            "author_id": str(getattr(author, "id", "") or ""),
            "author_name": str(getattr(author, "name", "") or ""),
            "content": str(getattr(message, "content", "") or ""),
            "mentions": [str(getattr(u, "id", "") or "") for u in mentions],
            "bot_user_id": self.bot_id,
            "is_group": guild is not None,
        }
