from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.db.models import ChannelBinding

CHANNEL_TEXT_LIMIT = 2000


@dataclass
class ChannelInbound:
    """渠道归一化入站消息(由各适配器从原始帧归一化)。"""

    channel: str
    event_id: str
    from_user_id: str
    to_user_id: str
    session_id: str
    group_id: str
    # 投递回话锚点:微信 iLink 为 context_token;企微无此概念,置 chatid/userid 占位
    context_token: str
    text: str
    is_group: bool
    raw: dict[str, Any]
    # 群内发言人显示名(帧内可获取时;无则 intake 回退 userid 尾段)
    sender_name: str = ""

    @property
    def conv_key(self) -> str:
        return self.group_id or self.session_id

    @property
    def external_conv_id(self) -> str:
        if self.is_group:
            return f"{self.channel}_group_{self.conv_key}"
        return f"{self.channel}_p2p_{self.from_user_id}"


class ChannelAdapter(Protocol):
    """渠道适配器协议:归一化 + 出站 + 可选 typing + ingress 生命周期。"""

    def normalize(self, raw: dict[str, Any]) -> ChannelInbound | None: ...

    def send(self, binding: ChannelBinding, target: dict[str, Any], text: str) -> None: ...

    def start_ingress(self, binding_id: str) -> None: ...

    def stop_ingress(self, binding_id: str) -> None: ...


_adapters: dict[str, ChannelAdapter] = {}


def register_channel_adapter(channel: str, adapter: ChannelAdapter) -> None:
    _adapters[channel] = adapter


def get_channel_adapter(channel: str) -> ChannelAdapter:
    adapter = _adapters.get(channel)
    if adapter is None:
        raise ValueError(f"未注册的渠道适配器: {channel}")
    return adapter


def split_channel_text(text: str, limit: int = CHANNEL_TEXT_LIMIT) -> list[str]:
    """按渠道 2000 字上限拆分长文本，优先 \n\n / \n / 空格边界，找不到则硬切。"""
    if not text:
        return []
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = -1
        for sep in ("\n\n", "\n", " "):
            cut = window.rfind(sep)
            if cut > 0:
                break
        if cut <= 0:
            chunks.append(remaining[:limit])
            remaining = remaining[limit:]
            continue
        chunk = remaining[:cut].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].lstrip("\n ")
    if remaining:
        chunks.append(remaining)
    return chunks
