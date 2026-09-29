import os

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.user import User
from app.services.identity_provider_service import provider_config


IDENTITY_VERIFICATION_STATUSES = frozenset(
    {
        "NOT_STARTED",
        "IN_PROGRESS",
        "PENDING_REVIEW",
        "NEEDS_ACTION",
        "VERIFIED",
        "REJECTED",
        "EXPIRED",
        "SUSPENDED",
    }
)
_SAFE_NEXT_ACTIONS = frozenset({"START", "REVIEW", "UPDATE_INFORMATION", "CONTACT_SUPPORT"})


def identity_verification_ui_enabled() -> bool:
    return os.getenv("IDENTITY_VERIFICATION_UI_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def safe_identity_status(value: str | None) -> str:
    normalized = (value or "NOT_STARTED").strip().upper()
    return normalized if normalized in IDENTITY_VERIFICATION_STATUSES else "NOT_STARTED"


def _progress_for_status(status: str) -> int:
    return {
        "NOT_STARTED": 0,
        "IN_PROGRESS": 25,
        "NEEDS_ACTION": 50,
        "PENDING_REVIEW": 75,
        "VERIFIED": 100,
    }.get(status, 0)


def _unavailable_payload() -> dict:
    return {
        "available": False,
        "status": "NOT_STARTED",
        "progress": 0,
        "steps": [],
        "verified_at": None,
        "expires_at": None,
        "next_action": None,
        "badge": False,
    }


def get_identity_verification_payload(db: Session, user: User) -> dict:
    available = bool(provider_config(user.id, user.role)["enabled"])
    if not available:
        return _unavailable_payload()

    from app.models.identity_verification import IdentityVerification

    try:
        record = (
            db.query(IdentityVerification)
            .filter(
                IdentityVerification.user_id == user.id,
                IdentityVerification.organization_id == user.organization_id,
            )
            .first()
        )
    except SQLAlchemyError:
        # The controlled migration may not be present yet. Fail closed while
        # preserving the rest of the authenticated application.
        db.rollback()
        return _unavailable_payload()
    status = safe_identity_status(record.status if record else None)
    # These are deliberately limited to non-sensitive account signals. No document,
    # CURP, biometric, provider, or external reference data leaves this service.
    steps = [
        {"key": "account", "complete": True},
        {"key": "email", "complete": bool(user.email_verified)},
        {"key": "phone", "complete": bool((user.telefone or "").strip())},
        {"key": "identity", "complete": status == "VERIFIED"},
    ]
    next_action = record.next_action_code if record else ("START" if available else None)
    if next_action not in _SAFE_NEXT_ACTIONS:
        next_action = None

    return {
        "available": available,
        "status": status if available else "NOT_STARTED",
        "progress": _progress_for_status(status) if available else 0,
        "steps": steps if available else [],
        "verified_at": record.verified_at.isoformat() if available and status == "VERIFIED" and record and record.verified_at else None,
        "expires_at": record.expires_at.isoformat() if available and status == "VERIFIED" and record and record.expires_at else None,
        "next_action": next_action if available else None,
        "badge": bool(available and status == "VERIFIED" and record and record.user_id == user.id and record.organization_id == user.organization_id),
    }
