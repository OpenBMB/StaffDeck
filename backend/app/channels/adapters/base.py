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
    # 渠道账号作用域:wechat 置空;wecom 为 corp_id/bot_id/binding.id(intake 以绑定配置为准重算)
    account_scope: str = ""

    @property
    def conv_key(self) -> str:
        return self.group_id or self.session_id

    @property
    def external_conv_id(self) -> str:
        if self.is_group:
            if self.account_scope:
                return f"{self.channel}_{self.account_scope}_group_{self.conv_key}"
            return f"{self.channel}_group_{self.conv_key}"
        if self.account_scope:
            return f"{self.channel}_{self.account_scope}_p2p_{self.from_user_id}"
        return f"{self.channel}_p2p_{self.from_user_id}"


class ChannelAdapter(Protocol):
    """渠道适配器协议:归一化 + 出站 + 可选 typing + ingress 生命周期。"""

    def normalize(self, raw: dict[str, Any]) -> ChannelInbound | None: ...

    def send(
        self,
        binding: ChannelBinding,
        target: dict[str, Any],
        text: str,
        *,
        idempotency_key: str | None = None,
    ) -> None: ...

    def start_ingress(self, binding_id: str) -> None: ...

    def stop_ingress(self, binding_id: str) -> None: ...


class ChannelReactionAdapter(Protocol):
    """可选能力:给入站消息挂"处理中"标记,最终回复送达后撤回。

    target 传整个投递目标字典而非单个消息 ID:飞书只需要 message_id,钉钉的
    emotion 接口还要求 openConversationId。

    reaction_attach_idempotent 决定重试语义。为 False 时适配器必须另外提供
    find_own_reaction(),重试前回查远端已挂上的标记;为 True 时表示重复挂同一
    标记无副作用,重试直接重发。
    """

    reaction_token: str
    reaction_attach_idempotent: bool

    def add_reaction(
        self,
        binding: ChannelBinding,
        target: dict[str, Any],
        token: str,
    ) -> str | None: ...

    def remove_reaction(
        self,
        binding: ChannelBinding,
        target: dict[str, Any],
        handle: str,
    ) -> None: ...


_adapters: dict[str, ChannelAdapter] = {}


def register_channel_adapter(channel: str, adapter: ChannelAdapter) -> None:
    _adapters[channel] = adapter


def get_channel_adapter(channel: str) -> ChannelAdapter:
    adapter = _adapters.get(channel)
    if adapter is None:
        raise ValueError(f"未注册的渠道适配器: {channel}")
    return adapter


def channel_reaction_token(channel: str) -> str | None:
    """该渠道"处理中"标记的标识;不支持 reaction 时返回 None。

    intake 与 outbox 都以此作为能力门禁,不再按渠道名字判断。
    """
    adapter = _adapters.get(channel)
    if adapter is None:
        return None
    if not callable(getattr(adapter, "add_reaction", None)) or not callable(
        getattr(adapter, "remove_reaction", None)
    ):
        return None
    token = str(getattr(adapter, "reaction_token", "") or "").strip()
    return token or None


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
