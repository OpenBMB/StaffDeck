from __future__ import annotations

from datetime import timedelta
from typing import Any, Protocol

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from .models import KnowledgeDiscoverySuggestion, Publication, utc_now


class SuggestionPublisher(Protocol):
    def publish(self, suggestion: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...


class PublicationConflict(RuntimeError):
    pass


def publish_suggestion(
    db: Session,
    suggestion: KnowledgeDiscoverySuggestion,
    publisher: SuggestionPublisher,
) -> dict[str, Any]:
    if suggestion.status not in {"reviewed", "confirmed"}:
        raise PublicationConflict("Review the suggestion before publishing")
    receipt = db.exec(select(Publication).where(Publication.suggestion_id == suggestion.id)).first()
    if receipt is None:
        if suggestion.status == "confirmed":
            raise PublicationConflict("Historical publication has no known target; do not republish")
        receipt = Publication(
            tenant_id=suggestion.tenant_id,
            suggestion_id=suggestion.id,
            idempotency_key=f"km:{suggestion.tenant_id}:{suggestion.id}",
        )
        db.add(receipt)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            receipt = db.exec(
                select(Publication).where(
                    Publication.suggestion_id == suggestion.id,
                )
            ).one()
        db.refresh(receipt)
    if receipt.status == "confirmed":
        return receipt.target_reference
    if receipt.status == "publishing" and receipt.updated_at > utc_now() - timedelta(seconds=60):
        raise PublicationConflict("Publication in progress; retry after the recovery lease")
    claimed = db.exec(
        update(Publication)
        .where(
            Publication.id == receipt.id,
            Publication.updated_at == receipt.updated_at,
        )
        .values(status="publishing", updated_at=utc_now(), error=None)
    )
    if claimed.rowcount != 1:
        db.rollback()
        raise PublicationConflict("Publication claimed by another request")
    db.commit()
    db.refresh(receipt)
    db.refresh(suggestion)
    try:
        reference = publisher.publish(suggestion.model_dump(mode="json"), receipt.idempotency_key)
        if not isinstance(reference, dict) or not reference:
            raise ValueError("Publisher returned no target reference")
    except Exception as exc:
        receipt.status, receipt.error, receipt.updated_at = "retryable", str(exc), utc_now()
        db.add(receipt)
        db.commit()
        raise
    # A failed commit leaves the durable publishing lease recoverable. Every retry
    # passes the same key, so the host must return its prior creation receipt.
    receipt.status, receipt.target_reference, receipt.updated_at = "confirmed", reference, utc_now()
    suggestion.status, suggestion.updated_at = "confirmed", utc_now()
    db.add(receipt)
    db.add(suggestion)
    db.commit()
    return reference
