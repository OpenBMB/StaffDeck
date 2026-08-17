from __future__ import annotations

from pathlib import PurePath

MAX_CHANNEL_MEDIA_BYTES = 25 * 1024 * 1024
MAX_ENCRYPTED_CHANNEL_MEDIA_BYTES = MAX_CHANNEL_MEDIA_BYTES + 32


class ChannelMediaTooLargeError(ValueError):
    pass


def ensure_channel_media_size(size: int, *, encrypted: bool = False) -> None:
    limit = MAX_ENCRYPTED_CHANNEL_MEDIA_BYTES if encrypted else MAX_CHANNEL_MEDIA_BYTES
    if size > limit:
        raise ChannelMediaTooLargeError(f"渠道附件超过大小上限: size={size} limit={limit}")


def collect_limited_media(chunks, *, encrypted: bool = False) -> bytes:
    limit = MAX_ENCRYPTED_CHANNEL_MEDIA_BYTES if encrypted else MAX_CHANNEL_MEDIA_BYTES
    parts: list[bytes] = []
    total = 0
    for chunk in chunks:
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise ChannelMediaTooLargeError(
                f"渠道附件超过大小上限: size>{limit} limit={limit}"
            )
        parts.append(chunk)
    return b"".join(parts)


def normalize_image_media(data: bytes) -> tuple[bytes, str, str] | None:
    if data.startswith(b"\xff\xd8\xff"):
        eoi = data.rfind(b"\xff\xd9")
        if eoi >= 3:
            return data[: eoi + 2], "image/jpeg", ".jpg"
    detected = detect_image_media_type(data)
    if detected is None:
        return None
    content_type, extension = detected
    return data, content_type, extension


def detect_image_media_type(data: bytes) -> tuple[str, str] | None:
    if data.startswith(b"\xff\xd8\xff") and b"\xff\xd9" in data[3:]:
        return "image/jpeg", ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if data.startswith(b"BM"):
        return "image/bmp", ".bmp"
    sample = data[:1024].lstrip().lower()
    if sample.startswith(b"<svg") or (sample.startswith(b"<?xml") and b"<svg" in sample):
        return "image/svg+xml", ".svg"
    return None


def filename_with_extension(filename: str, extension: str) -> str:
    name = PurePath(filename or "image").name
    stem = PurePath(name).stem or "image"
    return f"{stem}{extension}"


__all__ = [
    "MAX_CHANNEL_MEDIA_BYTES",
    "MAX_ENCRYPTED_CHANNEL_MEDIA_BYTES",
    "ChannelMediaTooLargeError",
    "collect_limited_media",
    "detect_image_media_type",
    "ensure_channel_media_size",
    "filename_with_extension",
    "normalize_image_media",
]
