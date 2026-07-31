from __future__ import annotations

from dataclasses import dataclass, field
import re
from pathlib import PurePath
from typing import Any

from app.core.harness_session_cleanup import (
    harness_path_segment,
    harness_task_workspace_path,
)
from app.harness import (
    HarnessExecutor,
    HarnessToolCall,
    HarnessToolContext,
    build_file_tool_registry,
)
from app.session.attachments import (
    IMAGE_DATA_URL_LIMIT_BYTES,
    image_payloads_from_attachments,
    validate_chat_turn_attachments,
)
from app.session.session_schema import ChatAttachmentRead


@dataclass(frozen=True)
class ValidatedTaskImagePayload:
    """Ephemeral vision payload; never serialize it into TaskRequirement."""

    attachment_id: str
    filename: str
    content_type: str
    size: int
    data_url: str = field(repr=False)


def materialize_task_attachments(
    attachments: list[ChatAttachmentRead],
    *,
    tenant_id: str,
    session_id: str,
    task_frame_id: str,
) -> list[dict[str, Any]]:
    if not attachments:
        return []
    workspace = harness_task_workspace_path(
        tenant_id=tenant_id,
        session_id=session_id,
        task_frame_id=task_frame_id,
    )
    context = HarnessToolContext(
        run_id=f"attachments-{harness_path_segment(task_frame_id)}",
        tenant_id=tenant_id,
        task_frame_id=task_frame_id,
        workspace_root=workspace,
    )
    executor = HarnessExecutor(build_file_tool_registry())
    descriptors: list[dict[str, Any]] = []
    for index, attachment in enumerate(attachments, start=1):
        descriptor: dict[str, Any] = {
            "attachment_id": attachment.id,
            "filename": attachment.filename,
            "content_type": attachment.content_type,
            "kind": attachment.kind,
            "size": attachment.size,
            "preview": attachment.preview,
        }
        if attachment.kind == "image" and attachment.data_url:
            image_data_url, image_error = _validated_image_data_url(attachment)
            if image_data_url:
                descriptor["vision_available"] = True
            elif image_error:
                descriptor.update(
                    {
                        "vision_available": False,
                        "image_error": image_error,
                    }
                )
        content = attachment.text
        if content:
            path = _attachment_path(attachment, index)
            result = executor.execute(
                context,
                HarnessToolCall(
                    call_id=f"attachment-{index}",
                    name="write_file",
                    arguments={
                        "path": path,
                        "content": content,
                        "create_parents": True,
                    },
                ),
            )
            if result.success:
                data = dict(result.data or {})
                descriptor.update(
                    {
                        "workspace_path": data.get("path"),
                        "sha256": data.get("sha256"),
                        "materialized": True,
                        "note": (
                            "PDF 附件保存的是服务端提取文本。"
                            if attachment.kind == "pdf"
                            else "附件文本已保存到当前 TaskFrame workspace。"
                        ),
                    }
                )
            else:
                descriptor.update(
                    {
                        "materialized": False,
                        "error": (
                            result.error.message
                            if result.error is not None
                            else "附件写入失败。"
                        ),
                    }
                )
        else:
            descriptor.update(
                {
                    "materialized": False,
                    "note": (
                        "图片通过当前 TaskFrame 的隔离视觉输入提供。"
                        if descriptor.get("vision_available")
                        else (
                            "当前聊天附件协议未保留可写入 workspace 的原始二进制；"
                            "只能使用这里的元数据和预览。"
                        )
                    ),
                }
            )
        descriptors.append(descriptor)
    return descriptors


def validated_task_image_payloads(
    attachments: list[ChatAttachmentRead],
) -> list[ValidatedTaskImagePayload]:
    """Build in-memory-only image payloads using the upload round-trip validator."""

    payloads: list[ValidatedTaskImagePayload] = []
    for attachment in attachments:
        if attachment.kind != "image" or not attachment.data_url:
            continue
        data_url, _error = _validated_image_data_url(attachment)
        if not data_url:
            continue
        payloads.append(
            ValidatedTaskImagePayload(
                attachment_id=attachment.id,
                filename=attachment.filename,
                content_type=attachment.content_type,
                size=attachment.size,
                data_url=data_url,
            )
        )
    return payloads


def isolated_attachment_context(
    attachments: list[dict[str, Any]],
    image_payloads: list[ValidatedTaskImagePayload] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Split attachment metadata from this TaskFrame's validated vision input.

    The returned descriptors are safe to serialize in ``task_requirement``.
    Image data URLs are projected through a one-message conversation context,
    which lets ``LLMClient`` use its existing multimodal protocol without
    importing any outer conversation history.
    """

    descriptors: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    for raw_descriptor in attachments:
        if not isinstance(raw_descriptor, dict):
            continue
        descriptor = dict(raw_descriptor)
        descriptor.pop("data_url", None)
        descriptors.append(descriptor)
    descriptor_index = {
        (
            str(descriptor.get("attachment_id") or ""),
            str(descriptor.get("filename") or ""),
            str(descriptor.get("content_type") or ""),
            int(descriptor.get("size") or 0),
        ): descriptor
        for descriptor in descriptors
        if descriptor.get("kind") == "image"
    }
    delivered_keys: set[tuple[str, str, str, int]] = set()
    for payload in image_payloads or []:
        if not isinstance(payload, ValidatedTaskImagePayload):
            continue
        descriptor = descriptor_index.get(
            (
                payload.attachment_id,
                payload.filename,
                payload.content_type,
                payload.size,
            )
        )
        if descriptor is None or not descriptor.get("vision_available"):
            continue
        attachment = _descriptor_image_attachment(
            descriptor,
            payload.data_url,
        )
        if attachment is None:
            descriptor["vision_available"] = False
            descriptor["image_error"] = "图片附件描述无效。"
            continue
        validated_data_url, _error = _validated_image_data_url(attachment)
        if not validated_data_url:
            descriptor["vision_available"] = False
            descriptor["image_error"] = "图片附件未通过发送前校验。"
            continue
        image_parts = image_payloads_from_attachments(
            [
                attachment.model_copy(
                    update={"data_url": validated_data_url}
                )
            ]
        )
        images.extend(image_parts)
        delivered_keys.add(
            (
                payload.attachment_id,
                payload.filename,
                payload.content_type,
                payload.size,
            )
        )
    for key, descriptor in descriptor_index.items():
        if descriptor.get("vision_available") and key not in delivered_keys:
            descriptor["vision_available"] = False
            descriptor["image_error"] = "本轮图片数据不可用。"
    if not images:
        return descriptors, None
    return descriptors, {
        "messages": [
            {
                "role": "user",
                "content": "当前 TaskRequirement 本轮上传的图片附件。",
                "images": images,
            }
        ]
    }


def _validated_image_data_url(
    attachment: ChatAttachmentRead,
) -> tuple[str | None, str | None]:
    try:
        normalized = validate_chat_turn_attachments(
            [attachment],
            max_attachments=1,
            max_attachment_bytes=IMAGE_DATA_URL_LIMIT_BYTES,
        )[0]
    except ValueError as exc:
        return None, str(exc)
    return normalized.data_url, None


def _descriptor_image_attachment(
    descriptor: dict[str, Any],
    data_url: str,
) -> ChatAttachmentRead | None:
    if descriptor.get("kind") != "image":
        return None
    try:
        return ChatAttachmentRead(
            id=str(descriptor.get("attachment_id") or ""),
            filename=str(descriptor.get("filename") or ""),
            content_type=str(descriptor.get("content_type") or ""),
            size=int(descriptor.get("size") or 0),
            kind="image",
            preview=str(descriptor.get("preview") or "") or None,
            data_url=data_url,
        )
    except (TypeError, ValueError):
        return None


def _attachment_path(
    attachment: ChatAttachmentRead,
    index: int,
) -> str:
    basename = PurePath(attachment.filename or f"attachment-{index}").name
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", basename).strip(".-")
    safe_name = safe_name[:96] or f"attachment-{index}.txt"
    if attachment.kind == "pdf":
        safe_name = f"{safe_name}.extracted.txt"
    return (
        "attachments/"
        f"{harness_path_segment(attachment.id)[:32]}-{safe_name}"
    )
