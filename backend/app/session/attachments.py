from __future__ import annotations

import base64
import csv
import io
import json
import hashlib
import mimetypes
import re
from collections.abc import Iterable
from typing import Any

from app.db.models import new_id
from app.session.attachment_store import sandbox_attachment_path
from app.session.session_schema import ChatAttachmentRead


MAX_EXTRACTED_TEXT_CHARS = 24_000
MAX_PREVIEW_CHARS = 600
IMAGE_DATA_URL_LIMIT_BYTES = 4 * 1024 * 1024
SUPPORTED_IMAGE_EXTENSIONS = {".gif", ".png", ".svg", ".jpg", ".jpeg", ".webp", ".bmp"}
SUPPORTED_IMAGE_CONTENT_TYPES = {
    "image/gif",
    "image/png",
    "image/svg+xml",
    "image/jpeg",
    "image/webp",
    "image/bmp",
}
TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".log",
    ".xml",
    ".html",
    ".htm",
    ".yaml",
    ".yml",
}


def parse_chat_attachment(filename: str, content_type: str | None, data: bytes) -> ChatAttachmentRead:
    safe_name = _safe_filename(filename)
    detected_type = content_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    lower_name = safe_name.lower()
    if _is_supported_image_file(lower_name, detected_type):
        detected_type = _image_content_type_for(lower_name, detected_type)
        return _image_attachment(safe_name, detected_type, data)
    if lower_name.endswith(".pdf") or detected_type == "application/pdf":
        return _pdf_attachment(safe_name, detected_type, data)
    if _is_text_file(lower_name, detected_type):
        return _text_attachment(safe_name, detected_type, data)
    return ChatAttachmentRead(
        id=new_id("file"),
        filename=safe_name,
        content_type=detected_type,
        size=len(data),
        kind="binary",
        preview="暂不支持直接读取该二进制文件内容。",
        python_summary=_python_file_summary(safe_name, detected_type, data, ""),
    )


def attachment_context_lines(attachments: Iterable[ChatAttachmentRead | dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    normalized = [_coerce_attachment(item) for item in attachments]
    normalized = [item for item in normalized if item]
    if not normalized:
        return lines
    lines.append("上传附件上下文：")
    for index, attachment in enumerate(normalized, start=1):
        lines.append(
            f"{index}. 文件名：{attachment.filename}；类型：{attachment.kind}/{attachment.content_type}；"
            f"大小：{attachment.size} bytes"
        )
        if attachment.python_summary:
            lines.append(f"Python理解摘要：{attachment.python_summary}")
        if attachment.text:
            lines.append("可读取正文：")
            lines.append(_trim_text(attachment.text, MAX_EXTRACTED_TEXT_CHARS))
        elif attachment.preview:
            lines.append(f"预览：{attachment.preview}")
        elif attachment.kind == "image":
            lines.append("图片附件已上传，可在前端消息中查看；如当前模型支持视觉输入，请结合图片内容回答。")
    return lines


def message_content_with_attachment_context(content: str, metadata: dict[str, Any] | None) -> str:
    attachments = []
    if isinstance(metadata, dict):
        raw = metadata.get("attachments")
        if isinstance(raw, list):
            attachments = raw
    lines = attachment_context_lines(attachments)
    if not lines:
        return content
    return "\n\n".join([content.strip() or "（用户仅上传了附件）", "\n".join(lines)])


def image_payloads_from_attachments(attachments: Iterable[ChatAttachmentRead | dict[str, Any]]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    normalized = [_coerce_attachment(item) for item in attachments]
    for attachment in normalized:
        if not attachment or not _attachment_is_supported_image(attachment) or not attachment.data_url:
            continue
        payloads.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": attachment.data_url,
                    "detail": "auto",
                },
            }
        )
    return payloads


def message_images_from_metadata(metadata: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(metadata, dict):
        return []
    attachments = metadata.get("attachments")
    if not isinstance(attachments, list):
        return []
    return image_payloads_from_attachments(attachments)


def request_has_image_attachments(attachments: Iterable[ChatAttachmentRead | dict[str, Any]]) -> bool:
    normalized = [_coerce_attachment(item) for item in attachments]
    return any(bool(item and _attachment_is_supported_image(item)) for item in normalized)


def validate_chat_turn_attachments(
    attachments: list[ChatAttachmentRead],
    *,
    max_attachments: int,
    max_attachment_bytes: int,
) -> list[ChatAttachmentRead]:
    """Validate and normalize the client round-trip from ``/attachments``.

    The upload response is currently stateless, so every field returning on a
    turn must be treated as untrusted user input.
    """

    if len(attachments) > max_attachments:
        raise ValueError(f"最多携带 {max_attachments} 个附件")
    normalized: list[ChatAttachmentRead] = []
    for attachment in attachments:
        filename = _safe_filename(attachment.filename)[:255]
        content_type = str(attachment.content_type or "").strip()[:128]
        size = int(attachment.size)
        if size < 0 or size > max_attachment_bytes:
            raise ValueError(f"{filename} 超过附件大小限制")
        text = _trim_text(
            str(attachment.text or ""),
            MAX_EXTRACTED_TEXT_CHARS,
        ) or None
        if text and len(text.encode("utf-8")) > max_attachment_bytes:
            raise ValueError(f"{filename} 的文本内容超过附件大小限制")
        preview = _trim_text(
            str(attachment.preview or ""),
            MAX_PREVIEW_CHARS,
        ) or None
        data_url = _validated_image_data_url(
            attachment,
            filename=filename,
            content_type=content_type,
            size=size,
        )
        sha256 = str(attachment.sha256 or "").strip().lower() or None
        if sha256 and not re.fullmatch(r"[a-f0-9]{64}", sha256):
            raise ValueError(f"{filename} 的 SHA-256 无效")
        sandbox_path = str(attachment.sandbox_path or "").strip() or None
        if sandbox_path and sandbox_path != sandbox_attachment_path(
            attachment.model_copy(update={"filename": filename})
        ):
            raise ValueError(f"{filename} 的沙箱路径无效")
        normalized.append(
            attachment.model_copy(
                update={
                    "filename": filename,
                    "content_type": content_type,
                    "size": size,
                    "text": text,
                    "preview": preview,
                    "data_url": data_url,
                    "sandbox_path": sandbox_path,
                    "sha256": sha256,
                    "python_summary": _round_trip_summary(
                        filename,
                        content_type,
                        size,
                        text,
                    ),
                    "error": str(attachment.error or "")[:2_000] or None,
                }
            )
        )
    return normalized


def _validated_image_data_url(
    attachment: ChatAttachmentRead,
    *,
    filename: str,
    content_type: str,
    size: int,
) -> str | None:
    raw = str(attachment.data_url or "").strip()
    if not raw:
        return None
    if attachment.kind != "image" or not _is_supported_image_file(
        filename.lower(),
        content_type,
    ):
        raise ValueError(f"{filename} 不是受支持的图片附件")
    prefix = f"data:{content_type};base64,"
    if not raw.startswith(prefix):
        raise ValueError(f"{filename} 的图片 data URL 无效")
    encoded = raw.removeprefix(prefix)
    if len(encoded) > ((IMAGE_DATA_URL_LIMIT_BYTES + 2) // 3) * 4:
        raise ValueError(f"{filename} 的图片 data URL 超限")
    try:
        decoded = base64.b64decode(
            encoded,
            validate=True,
        )
    except Exception as exc:
        raise ValueError(f"{filename} 的图片 data URL 无效") from exc
    if len(decoded) > IMAGE_DATA_URL_LIMIT_BYTES or len(decoded) != size:
        raise ValueError(f"{filename} 的图片 data URL 大小不一致或超限")
    if attachment.sha256 and hashlib.sha256(decoded).hexdigest() != attachment.sha256.lower():
        raise ValueError(f"{filename} 的图片 data URL 与上传文件不一致")
    return raw


def _round_trip_summary(
    filename: str,
    content_type: str,
    size: int,
    text: str | None,
) -> str:
    return _python_file_summary_from_size(
        filename,
        content_type,
        size,
        text or "",
    )


def _text_attachment(filename: str, content_type: str, data: bytes) -> ChatAttachmentRead:
    text = _decode_text(data)
    trimmed = _trim_text(text, MAX_EXTRACTED_TEXT_CHARS)
    return ChatAttachmentRead(
        id=new_id("file"),
        filename=filename,
        content_type=content_type,
        size=len(data),
        kind="text",
        text=trimmed,
        preview=_trim_text(trimmed, MAX_PREVIEW_CHARS),
        python_summary=_python_file_summary(filename, content_type, data, trimmed),
    )


def _pdf_attachment(filename: str, content_type: str, data: bytes) -> ChatAttachmentRead:
    text = ""
    error: str | None = None
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = []
        for page in reader.pages[:30]:
            pages.append(page.extract_text() or "")
        text = "\n\n".join(page.strip() for page in pages if page.strip())
        if len(reader.pages) > 30:
            text += f"\n\n... PDF 共 {len(reader.pages)} 页，仅提取前 30 页。"
    except Exception as exc:  # noqa: BLE001 - return readable parse error to caller.
        error = f"PDF 解析失败：{exc}"
    trimmed = _trim_text(text, MAX_EXTRACTED_TEXT_CHARS)
    return ChatAttachmentRead(
        id=new_id("file"),
        filename=filename,
        content_type=content_type or "application/pdf",
        size=len(data),
        kind="pdf",
        text=trimmed or None,
        preview=_trim_text(trimmed, MAX_PREVIEW_CHARS) if trimmed else None,
        python_summary=_python_file_summary(filename, content_type, data, trimmed),
        error=error,
    )


def _image_attachment(filename: str, content_type: str, data: bytes) -> ChatAttachmentRead:
    data_url = None
    if len(data) <= IMAGE_DATA_URL_LIMIT_BYTES:
        encoded = base64.b64encode(data).decode("ascii")
        data_url = f"data:{content_type};base64,{encoded}"
    return ChatAttachmentRead(
        id=new_id("file"),
        filename=filename,
        content_type=content_type,
        size=len(data),
        kind="image",
        data_url=data_url,
        preview="图片附件",
        python_summary=_python_file_summary(filename, content_type, data, ""),
    )


def _python_file_summary(filename: str, content_type: str, data: bytes, text: str) -> str:
    return _python_file_summary_from_size(
        filename,
        content_type,
        len(data),
        text,
    )


def _python_file_summary_from_size(
    filename: str,
    content_type: str,
    size: int,
    text: str,
) -> str:
    parts = [f"文件 {filename}，{size} bytes，MIME {content_type}。"]
    if text:
        lines = text.splitlines()
        words = re.findall(r"\S+", text)
        parts.append(f"解析得到 {len(text)} 个字符、{len(lines)} 行、约 {len(words)} 个词。")
        tabular = _tabular_summary(text)
        if tabular:
            parts.append(tabular)
        json_summary = _json_summary(text)
        if json_summary:
            parts.append(json_summary)
    else:
        parts.append("未抽取到可直接阅读的文本正文。")
    return " ".join(parts)


def _tabular_summary(text: str) -> str:
    sample = "\n".join(text.splitlines()[:20])
    if not sample.strip():
        return ""
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        return ""
    rows = list(csv.reader(io.StringIO(sample), dialect))
    if not rows:
        return ""
    columns = rows[0]
    return f"检测到表格结构，约 {len(columns)} 列；前几列：{', '.join(columns[:6])}。"


def _json_summary(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith(("{", "[")):
        return ""
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return ""
    if isinstance(parsed, dict):
        keys = list(parsed.keys())[:8]
        return f"检测到 JSON 对象，顶层字段：{', '.join(map(str, keys))}。"
    if isinstance(parsed, list):
        return f"检测到 JSON 数组，元素数量：{len(parsed)}。"
    return "检测到 JSON 标量。"


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _safe_filename(filename: str) -> str:
    name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].strip()
    return name or "uploaded-file"


def _trim_text(text: str, max_chars: int) -> str:
    normalized = text.replace("\x00", "").strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[:max_chars].rstrip() + "\n...（内容已截断）"


def _is_text_file(lower_name: str, content_type: str) -> bool:
    extension = "." + lower_name.rsplit(".", 1)[-1] if "." in lower_name else ""
    return (
        extension in TEXT_EXTENSIONS
        or content_type.startswith("text/")
        or content_type
        in {
            "application/json",
            "application/xml",
            "application/x-yaml",
            "application/yaml",
        }
    )


def _is_supported_image_file(lower_name: str, content_type: str) -> bool:
    extension = "." + lower_name.rsplit(".", 1)[-1] if "." in lower_name else ""
    return content_type.lower() in SUPPORTED_IMAGE_CONTENT_TYPES or extension in SUPPORTED_IMAGE_EXTENSIONS


def _attachment_is_supported_image(attachment: ChatAttachmentRead) -> bool:
    return attachment.kind == "image" and _is_supported_image_file(attachment.filename.lower(), attachment.content_type)


def _image_content_type_for(lower_name: str, content_type: str) -> str:
    normalized = content_type.lower()
    if normalized in SUPPORTED_IMAGE_CONTENT_TYPES:
        return content_type
    guessed = mimetypes.guess_type(lower_name)[0]
    if guessed and guessed.lower() in SUPPORTED_IMAGE_CONTENT_TYPES:
        return guessed
    if lower_name.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lower_name.endswith(".png"):
        return "image/png"
    if lower_name.endswith(".gif"):
        return "image/gif"
    if lower_name.endswith(".svg"):
        return "image/svg+xml"
    if lower_name.endswith(".webp"):
        return "image/webp"
    if lower_name.endswith(".bmp"):
        return "image/bmp"
    return content_type


def _coerce_attachment(value: ChatAttachmentRead | dict[str, Any]) -> ChatAttachmentRead | None:
    if isinstance(value, ChatAttachmentRead):
        return value
    if not isinstance(value, dict):
        return None
    try:
        return ChatAttachmentRead.model_validate(value)
    except Exception:
        return None
