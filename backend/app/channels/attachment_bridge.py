from __future__ import annotations

import logging
from typing import Any

from app.channels.adapters.base import ChannelInbound
from app.db.models import ChannelBinding
from app.session.session_schema import ChatAttachmentRead

logger = logging.getLogger(__name__)

MAX_CHANNEL_MEDIA_BYTES = 25 * 1024 * 1024  # 25MB

# 图片 magic bytes 签名 → (content_type, extension)
_IMAGE_SIGNATURES: list[tuple[bytes, str, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
    (b"RIFF", "image/webp", ".webp"),  # 需后续确认 WebP 标记
    (b"BM", "image/bmp", ".bmp"),
]


def _detect_image_type(data: bytes) -> tuple[str, str] | None:
    """从字节签名推断图片 content_type 和扩展名。返回 (content_type, ext) 或 None。"""
    if len(data) < 12:
        return None
    for sig, content_type, ext in _IMAGE_SIGNATURES:
        if data.startswith(sig):
            if sig == b"RIFF" and data[8:12] != b"WEBP":
                continue
            return content_type, ext
    return None


def _resolve_content_type(
    att_content_type: str,
    att_filename: str,
    data: bytes,
) -> tuple[str, str]:
    """根据下载字节修正 content_type 和 filename。

    渠道 normalize 阶段可能无法确定真实 MIME(飞书 image_key 不含扩展名,
    钉钉 picture 消息也不提供类型),因此下载后用 magic bytes 覆盖。
    """
    detected = _detect_image_type(data)
    if detected:
        content_type, ext = detected
        filename = att_filename
        if not filename.lower().endswith(ext):
            filename = f"{att_filename}{ext}"
        return content_type, filename
    # 非图片或无法识别:保留渠道侧提供的值,空则传 None 让 parse 自动推断
    ct = (att_content_type or "").strip()
    return (ct or None, att_filename)  # type: ignore[return-value]


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
            data = download_media(binding, att, max_bytes=MAX_CHANNEL_MEDIA_BYTES)
            if not data:
                continue
            content_type, filename = _resolve_content_type(
                att.content_type,
                att.filename or att.media_id,
                data,
            )
            attachment = parse_chat_attachment(
                filename,
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
