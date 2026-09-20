from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import ValidationError
from sqlmodel import Session

from app.config import get_settings
from app.db import get_session
from app.public_api.auth import PublicPrincipal, enforce_agent_access, require_scopes
from app.public_api.errors import PublicAPIError
from app.public_api.sessions import ensure_public_agent
from app.session.attachment_store import read_staged_chat_attachment, stage_chat_attachment
from app.session.attachments import (
    MAX_CHAT_ATTACHMENTS,
    parse_chat_attachment,
    validate_chat_turn_attachments,
)
from app.session.session_schema import ChatAttachmentRead

router = APIRouter(tags=["attachments"])


@router.post("/agents/{agent_id}/attachments", response_model=list[ChatAttachmentRead])
async def upload_public_attachments(
    agent_id: str,
    files: list[UploadFile] = File(default=[], alias="files[]"),
    principal: PublicPrincipal = Depends(require_scopes("runs:create")),
    db: Session = Depends(get_session),
) -> list[ChatAttachmentRead]:
    enforce_agent_access(principal, agent_id)
    ensure_public_agent(db, principal, agent_id)
    if not files:
        raise PublicAPIError(400, "NO_FILES_UPLOADED", "At least one file is required.")
    if len(files) > MAX_CHAT_ATTACHMENTS:
        raise PublicAPIError(
            400,
            "TOO_MANY_ATTACHMENTS",
            f"At most {MAX_CHAT_ATTACHMENTS} attachments may be uploaded at once.",
        )

    max_attachment_bytes = get_settings().chat_attachment_max_bytes
    parsed: list[tuple[ChatAttachmentRead, bytes]] = []
    for file in files:
        data = await file.read()
        if len(data) > max_attachment_bytes:
            raise PublicAPIError(
                413,
                "ATTACHMENT_TOO_LARGE",
                f"{file.filename or 'Attachment'} exceeds the upload size limit.",
            )
        parsed.append(
            (
                parse_chat_attachment(
                    file.filename or "uploaded-file",
                    file.content_type,
                    data,
                    extract_text=False,
                ),
                data,
            )
        )

    staged: list[ChatAttachmentRead] = []
    for attachment, data in parsed:
        try:
            staged.append(
                stage_chat_attachment(
                    attachment,
                    data,
                    tenant_id=principal.tenant_id,
                    user_id=principal.actor_user.id,
                )
            )
        except OSError as exc:
            raise PublicAPIError(
                500,
                "ATTACHMENT_STAGING_FAILED",
                "The attachment could not be staged.",
            ) from exc
    return staged


def validate_staged_run_attachments(
    raw_attachments: list[dict[str, Any]],
    *,
    principal: PublicPrincipal,
) -> list[ChatAttachmentRead]:
    if not raw_attachments:
        return []
    try:
        attachments = [ChatAttachmentRead.model_validate(item) for item in raw_attachments]
        normalized = validate_chat_turn_attachments(
            attachments,
            max_attachments=MAX_CHAT_ATTACHMENTS,
            max_attachment_bytes=get_settings().chat_attachment_max_bytes,
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise PublicAPIError(
            400,
            "INVALID_ATTACHMENT",
            "One or more attachments are invalid.",
        ) from exc

    for attachment in normalized:
        staged_data = read_staged_chat_attachment(
            attachment,
            tenant_id=principal.tenant_id,
            user_id=principal.actor_user.id,
        )
        if staged_data is None:
            raise PublicAPIError(
                400,
                "INVALID_ATTACHMENT",
                "One or more attachments are missing or do not match their staged content.",
            )
    return normalized


__all__ = ["router", "validate_staged_run_attachments"]
