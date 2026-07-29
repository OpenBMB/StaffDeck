from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.db.models import ChannelInboundEvent

# reaction 目标除消息定位外还需要从入站事件继承的 provider 字段:
# 钉钉 emotion 接口要求 openConversationId,飞书只用 message_id 会忽略它。
REACTION_TARGET_INHERITED_KEYS = ("conversation_id",)


class StageDisposition(StrEnum):
    STAGED = "staged"
    DUPLICATE = "duplicate"
    SECURITY_DROP = "security_drop"
    NACK = "nack"


@dataclass(frozen=True)
class StageResult:
    disposition: StageDisposition
    event_pk: str | None = None
    error_code: str | None = None

    @property
    def should_ack(self) -> bool:
        return self.disposition is not StageDisposition.NACK


def reaction_target(event: ChannelInboundEvent) -> dict[str, Any]:
    """reaction 投递目标:锚定入站事件本身,不读取可变的会话目标。

    message_id 取事件的 event_id,event_pk 用于回写 reaction 句柄时校验事件边界。
    """
    target: dict[str, Any] = {"message_id": event.event_id, "event_pk": event.id}
    source = event.target_json or {}
    for key in REACTION_TARGET_INHERITED_KEYS:
        value = str(source.get(key) or "").strip()
        if value:
            target[key] = value
    return target
