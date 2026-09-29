import hashlib
import os
from datetime import datetime

from sqlalchemy import exists
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.identity_human_review import (
    HUMAN_REVIEW_DECISIONS,
    HUMAN_REVIEW_REASON_CODES,
    IdentityHumanReviewDecision,
)
from app.models.identity_provider import IdentityVerificationAttempt, IdentityVerificationEvent
from app.models.identity_verification import IdentityVerification
from app.models.user import User


REVIEW_ADMIN_ROLES = frozenset({"ROOT", "GERENTE"})
TERMINAL_DECISIONS = frozenset({"APPROVE", "REJECT"})


def human_review_enabled() -> bool:
    return os.getenv("IDENTITY_HUMAN_REVIEW_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _hash_idempotency_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_review_enabled() -> None:
    if not human_review_enabled():
        raise RuntimeError("human review unavailable")


def _get_scoped_attempt(db: Session, *, attempt_id: int, reviewer: User) -> IdentityVerificationAttempt:
    attempt = (
        db.query(IdentityVerificationAttempt)
        .filter(IdentityVerificationAttempt.id == attempt_id)
        .with_for_update()
        .first()
    )
    if not attempt or not attempt.organization_id or attempt.organization_id != reviewer.organization_id:
        raise LookupError("review item not found")
    if reviewer.role not in REVIEW_ADMIN_ROLES or reviewer.id == attempt.user_id:
        raise PermissionError("review not permitted")
    return attempt


def _safe_attempt_payload(db: Session, attempt: IdentityVerificationAttempt) -> dict:
    decisions = (
        db.query(IdentityHumanReviewDecision)
        .filter(IdentityHumanReviewDecision.attempt_id == attempt.id)
        .order_by(IdentityHumanReviewDecision.created_at.asc())
        .all()
    )
    return {
        "id": attempt.id,
        "reference": f"attempt-{attempt.id}",
        "provider": attempt.provider,
        "status": attempt.status,
        "policy": attempt.policy,
        "provider_mode": attempt.provider_mode,
        "created_at": attempt.created_at.isoformat() if attempt.created_at else None,
        "updated_at": attempt.updated_at.isoformat() if attempt.updated_at else None,
        "expires_at": attempt.expires_at.isoformat() if attempt.expires_at else None,
        "reviewable": attempt.status == "PENDING_REVIEW",
        "review_state": decisions[-1].decision if decisions else "PENDING",
        "decisions": [
            {
                "id": decision.id,
                "decision": decision.decision,
                "reason_code": decision.reason_code,
                "created_at": decision.created_at.isoformat() if decision.created_at else None,
            }
            for decision in decisions
        ],
    }


def list_review_queue(db: Session, *, reviewer: User) -> list[dict]:
    _require_review_enabled()
    if reviewer.role not in REVIEW_ADMIN_ROLES or not reviewer.organization_id:
        raise PermissionError("review not permitted")
    rows = (
        db.query(IdentityVerificationAttempt)
        .filter(
            IdentityVerificationAttempt.organization_id == reviewer.organization_id,
            IdentityVerificationAttempt.status == "PENDING_REVIEW",
            IdentityVerificationAttempt.user_id != reviewer.id,
            ~exists().where(
                (IdentityHumanReviewDecision.attempt_id == IdentityVerificationAttempt.id)
                & IdentityHumanReviewDecision.decision.in_(TERMINAL_DECISIONS)
            ),
        )
        .order_by(IdentityVerificationAttempt.created_at.asc())
        .all()
    )
    return [_safe_attempt_payload(db, row) for row in rows]


def get_review_item(db: Session, *, attempt_id: int, reviewer: User) -> dict:
    _require_review_enabled()
    attempt = _get_scoped_attempt(db, attempt_id=attempt_id, reviewer=reviewer)
    if attempt.status != "PENDING_REVIEW":
        raise ValueError("review item is not pending")
    return _safe_attempt_payload(db, attempt)


def decide_review(
    db: Session,
    *,
    attempt_id: int,
    reviewer: User,
    decision: str,
    reason_code: str,
    idempotency_key: str,
) -> dict:
    _require_review_enabled()
    decision = decision.strip().upper()
    reason_code = reason_code.strip().upper()
    idempotency_key = idempotency_key.strip()
    if decision not in HUMAN_REVIEW_DECISIONS or reason_code not in HUMAN_REVIEW_REASON_CODES or not idempotency_key:
        raise ValueError("invalid review decision")

    key_hash = _hash_idempotency_key(idempotency_key)
    existing = db.query(IdentityHumanReviewDecision).filter_by(idempotency_key_hash=key_hash).first()
    if existing:
        if existing.attempt_id != attempt_id or existing.reviewer_user_id != reviewer.id:
            raise ValueError("idempotency key conflict")
        return {"decision_id": existing.id, "decision": existing.decision, "status": "IDEMPOTENT_REPLAY"}

    attempt = _get_scoped_attempt(db, attempt_id=attempt_id, reviewer=reviewer)
    prior_terminal = (
        db.query(IdentityHumanReviewDecision)
        .filter(
            IdentityHumanReviewDecision.attempt_id == attempt.id,
            IdentityHumanReviewDecision.decision.in_(TERMINAL_DECISIONS),
        )
        .first()
    )
    if prior_terminal:
        raise ValueError("review item already decided")
    if attempt.status != "PENDING_REVIEW":
        raise ValueError("review item is not pending")
    if decision == "APPROVE":
        approved_provider_event = (
            db.query(IdentityVerificationEvent)
            .filter(
                IdentityVerificationEvent.identity_verification_attempt_id == attempt.id,
                IdentityVerificationEvent.reason_code == "PROVIDER_APPROVED",
            )
            .first()
        )
        if attempt.provider_mode != "live" or not approved_provider_event:
            raise ValueError("provider approval unavailable")

    identity = (
        db.query(IdentityVerification)
        .filter(
            IdentityVerification.id == attempt.identity_verification_id,
            IdentityVerification.user_id == attempt.user_id,
            IdentityVerification.organization_id == attempt.organization_id,
        )
        .with_for_update()
        .first()
    )
    if not identity:
        raise ValueError("review item not found")

    row = IdentityHumanReviewDecision(
        attempt_id=attempt.id,
        user_id=attempt.user_id,
        organization_id=attempt.organization_id,
        reviewer_user_id=reviewer.id,
        reviewer_organization_id=reviewer.organization_id,
        decision=decision,
        reason_code=reason_code,
        idempotency_key_hash=key_hash,
    )
    db.add(row)
    if decision == "REJECT":
        identity.status = "REJECTED"
        identity.next_action_code = None
        attempt.status = "REJECTED"
    elif decision == "REQUEST_CHANGES":
        identity.status = "NEEDS_ACTION"
        identity.next_action_code = "UPDATE_INFORMATION"
        attempt.status = "IN_PROGRESS"
    else:
        identity.status = "VERIFIED"
        identity.verified_at = datetime.utcnow()
        identity.next_action_code = None
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("review item already decided") from exc
    return {"decision_id": row.id, "decision": decision, "status": "APPLIED"}
