from __future__ import annotations

import logging
from typing import Any

from app.channels.adapters.base import ChannelInbound
from app.db.models import ChannelBinding
from app.session.session_schema import ChatAttachmentRead

logger = logging.getLogger(__name__)

MAX_CHANNEL_MEDIA_BYTES = 25 * 1024 * 1024  # 25MB


def inbound_attachments_to_chat(
    binding: ChannelBinding,
    inbound: ChannelInbound,
    *,
    db_engine: Any,
    tenant_id: str,
    user_id: str,
) -> list[ChatAttachmentRead]:
    """下载渠道附件,暂存原始字节,转为 ChatAttachmentRead 列表。

    调用各适配器的 download_media 方法获取原始字节,然后复用 web chat 的
    parse_chat_attachment + stage_chat_attachment 完成解析和暂存。

    单个附件失败不影响其他附件和主链路(intake 侧再降级为纯文本轮)。
    """
    # 延迟 import 避免 app.core -> app.session.attachment_store 的循环依赖
    from app.channels.adapters.base import get_channel_adapter

    adapter = get_channel_adapter(inbound.channel)
    download_media = getattr(adapter, "download_media", None)
    if download_media is None:
        logger.warning("渠道 %s 未实现 download_media,跳过附件", inbound.channel)
        return []

    # 真正需要下载/暂存时才 import,避免循环依赖与无谓加载
    from app.session.attachment_store import stage_chat_attachment
    from app.session.attachments import parse_chat_attachment

    results: list[ChatAttachmentRead] = []
    for att in inbound.attachments:
        try:
            data = download_media(binding, att)
            if not data:
                continue
            if len(data) > MAX_CHANNEL_MEDIA_BYTES:
                logger.warning(
                    "渠道附件超过大小上限 binding=%s media_id=%s size=%d",
                    binding.id,
                    att.media_id,
                    len(data),
                )
                continue
            content_type = att.content_type or None
            attachment = parse_chat_attachment(
                att.filename or att.media_id,
                content_type,
                data,
            )
            staged = stage_chat_attachment(
                attachment,
                data,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            results.append(staged)
        except Exception:
            logger.exception(
                "渠道附件处理失败 binding=%s media_id=%s",
                binding.id,
                att.media_id,
            )
    return results
