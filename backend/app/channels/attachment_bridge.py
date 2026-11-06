from __future__ import annotations

import logging
import mimetypes
from typing import Any

from app.channels.adapters.base import ChannelInbound
from app.channels.media import (
    MAX_CHANNEL_MEDIA_BYTES,
    filename_with_extension,
    normalize_image_media,
)
from app.db.models import ChannelBinding
from app.session.session_schema import ChatAttachmentRead

logger = logging.getLogger(__name__)


def inbound_attachments_to_chat(
    binding: ChannelBinding,
    inbound: ChannelInbound,
    *,
    db_engine: Any = None,
    tenant_id: str,
    user_id: str,
) -> list[ChatAttachmentRead]:
    """Download channel media and stage it through the web attachment pipeline."""
    from app.channels.adapters.base import get_channel_adapter
    from app.session.attachment_store import stage_chat_attachment
    from app.session.attachments import parse_chat_attachment

    download_media = getattr(get_channel_adapter(inbound.channel), "download_media", None)
    if not callable(download_media):
        logger.warning("渠道 %s 未实现 download_media，跳过附件", inbound.channel)
        return []
    results: list[ChatAttachmentRead] = []
    for descriptor in inbound.attachments:
        try:
            data = download_media(binding, descriptor, max_bytes=MAX_CHANNEL_MEDIA_BYTES)
            if not data or len(data) > MAX_CHANNEL_MEDIA_BYTES:
                logger.warning(
                    "渠道附件为空或超过大小上限 binding=%s media_id=%s size=%s",
                    binding.id,
                    descriptor.media_id,
                    len(data) if data else 0,
                )
                continue
            normalized_image = normalize_image_media(data)
            if descriptor.kind == "image" and normalized_image is None:
                raise ValueError("渠道图片内容不是受支持的图片格式")
            if normalized_image is not None:
                data, descriptor.content_type, extension = normalized_image
                descriptor.kind = "image"
                descriptor.filename = filename_with_extension(
                    descriptor.filename or descriptor.media_id,
                    extension,
                )
            elif descriptor.filename and (
                not descriptor.content_type
                or descriptor.content_type == "application/octet-stream"
            ):
                descriptor.content_type = (
                    mimetypes.guess_type(descriptor.filename)[0]
                    or descriptor.content_type
                )
            parsed = parse_chat_attachment(
                descriptor.filename or descriptor.media_id,
                descriptor.content_type or None,
                data,
            )
            results.append(
                stage_chat_attachment(parsed, data, tenant_id=tenant_id, user_id=user_id)
            )
        except Exception:
            logger.exception(
                "渠道附件处理失败 binding=%s media_id=%s", binding.id, descriptor.media_id
            )
    return results
