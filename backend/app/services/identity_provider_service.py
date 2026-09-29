import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta
from secrets import token_urlsafe

from sqlalchemy.orm import Session

from app.models.identity_provider import IdentityVerificationAttempt, IdentityVerificationEvent
from app.models.identity_verification import IdentityVerification
from app.models.user import User


CONSENT_VERSION = "identity-verification-v1"
ROLLOUT_MODES = frozenset({"off", "canary", "all"})
IDENTITY_VERIFICATION_ELIGIBLE_ROLES = frozenset({"BROKER", "TECNICO", "NETWORK_PARTNER"})


def provider_enabled() -> bool:
    return os.getenv("IDENTITY_VERIFICATION_UI_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def rollout_config() -> tuple[str, frozenset[int], bool]:
    mode = os.getenv("IDENTITY_VERIFICATION_ROLLOUT_MODE", "off").strip().lower()
    if mode not in ROLLOUT_MODES:
        return mode, frozenset(), False
    raw_ids = os.getenv("IDENTITY_VERIFICATION_CANARY_USER_IDS", "")
    if not raw_ids.strip():
        return mode, frozenset(), True
    normalized_ids = []
    for token in raw_ids.split(","):
        value = token.strip()
        if not value or not value.isascii() or not value.isdigit() or int(value) <= 0:
            return mode, frozenset(), False
        normalized_ids.append(int(value))
    return mode, frozenset(normalized_ids), True


def user_is_rollout_eligible(*, user_id: int, role: str | None) -> bool:
    mode, canary_user_ids, valid = rollout_config()
    if not valid or mode == "off" or (role or "").strip().upper() not in IDENTITY_VERIFICATION_ELIGIBLE_ROLES:
        return False
    if mode == "all":
        return True
    return user_id in canary_user_ids


def provider_config(user_id: int | None = None, user_role: str | None = None) -> dict:
    provider = os.getenv("IDENTITY_PROVIDER", "").strip().lower()
    mode = os.getenv("IDENTITY_PROVIDER_MODE", "disabled").strip().lower()
    configured = all(os.getenv(name, "").strip() for name in ("METAMAP_CLIENT_ID", "METAMAP_FLOW_ID"))
    rollout_mode, _, rollout_valid = rollout_config()
    rollout_allowed = user_id is not None and user_is_rollout_eligible(user_id=user_id, role=user_role)
    fully_configured = provider_enabled() and provider == "metamap" and mode == "sandbox" and configured and rollout_valid and rollout_allowed
    return {
        "enabled": fully_configured,
        "provider": provider or None,
        "mode": mode,
        "client_id": os.getenv("METAMAP_CLIENT_ID", "").strip() if fully_configured else None,
        "flow_id": os.getenv("METAMAP_FLOW_ID", "").strip() if fully_configured else None,
        "consent_version": CONSENT_VERSION,
        "rollout_mode": rollout_mode if rollout_valid else "off",
    }


def _hash_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def create_attempt(db: Session, *, user: User, organization_id: int | None, policy: str, consent_version: str) -> tuple[IdentityVerificationAttempt, str, dict]:
    if user.organization_id != organization_id:
        raise ValueError("identity verification tenant mismatch")
    config = provider_config(user.id, user.role)
    if not config["enabled"]:
        raise RuntimeError("identity provider unavailable")
    if consent_version != CONSENT_VERSION:
        raise ValueError("unsupported consent version")
    identity = db.query(IdentityVerification).filter(
        IdentityVerification.user_id == user.id,
        IdentityVerification.organization_id == organization_id,
    ).first()
    if not identity:
        identity = IdentityVerification(user_id=user.id, organization_id=organization_id, status="NOT_STARTED")
        db.add(identity)
        db.flush()
    attempt_key = token_urlsafe(32)
    attempt = IdentityVerificationAttempt(
        user_id=user.id,
        organization_id=organization_id,
        identity_verification_id=identity.id,
        attempt_key_hash=_hash_key(attempt_key),
        provider="metamap",
        provider_mode="sandbox",
        policy=policy,
        status="CREATED",
        consented_at=datetime.utcnow(),
        consent_version=consent_version,
        expires_at=datetime.utcnow() + timedelta(minutes=20),
    )
    db.add(attempt)
    db.flush()
    return attempt, attempt_key, {
        "provider": "metamap",
        "mode": "sandbox",
        "metadata": {"attempt_key": attempt_key},
        "client_id": config["client_id"],
        "flow_id": config["flow_id"],
        "consent_version": CONSENT_VERSION,
    }


def verify_metamap_signature(raw_body: bytes, signature: str | None, secret: str | None) -> bool:
    if not raw_body or not signature or not secret or len(signature) != 64:
        return False
    try:
        bytes.fromhex(signature)
    except ValueError:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.lower())


def parse_json_body(raw_body: bytes) -> dict:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid webhook JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid webhook payload")
    return payload


def process_metamap_event(db: Session, *, raw_body: bytes, payload: dict) -> str:
    event_name = str(payload.get("eventName") or "").strip().lower()
    allowed_events = {
        "verification_started",
        "step_completed",
        "verification_inputs_completed",
        "verification_completed",
        "verification_updated",
    }
    if event_name not in allowed_events:
        raise ValueError("unsupported identity provider event")
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("invalid identity provider metadata")
    attempt_key = str(metadata.get("attempt_key") or "").strip()
    if not attempt_key:
        raise ValueError("invalid identity provider event")
    payload_hash = hashlib.sha256(raw_body).hexdigest()
    provider_reference = str(
        payload.get("verificationId") or payload.get("identityId") or payload.get("resource") or ""
    ).strip()
    event_timestamp = str(payload.get("timestamp") or payload.get("createdAt") or "").strip()
    dedupe_material = "|".join(("metamap", provider_reference, event_name, event_timestamp, attempt_key, payload_hash))
    event_id = "derived:" + hashlib.sha256(dedupe_material.encode("utf-8")).hexdigest()
    existing = db.query(IdentityVerificationEvent).filter_by(provider="metamap", provider_event_id=event_id).first()
    if existing:
        return "DUPLICATE"
    attempt = db.query(IdentityVerificationAttempt).filter_by(attempt_key_hash=_hash_key(attempt_key)).with_for_update().first()
    if not attempt or attempt.provider != "metamap" or attempt.provider_mode != "sandbox":
        raise ValueError("unknown identity verification attempt")
    if attempt.expires_at <= datetime.utcnow():
        attempt.status = "EXPIRED"
        raise ValueError("identity verification attempt expired")
    identity = db.query(IdentityVerification).filter_by(
        id=attempt.identity_verification_id,
        user_id=attempt.user_id,
        organization_id=attempt.organization_id,
    ).with_for_update().one()
    final_status = str(payload.get("verificationStatus") or payload.get("status") or "").lower()
    if event_name in {"verification_started", "step_completed", "verification_inputs_completed"}:
        mapped = "IN_PROGRESS"
    elif event_name == "verification_completed" and final_status in {"verified", "success", "approved"}:
        mapped = "PENDING_REVIEW"
    elif final_status in {"reviewneeded", "review_needed", "pending"}:
        mapped = "PENDING_REVIEW"
    elif final_status in {"rejected", "failed"}:
        mapped = "REJECTED"
    else:
        mapped = "PENDING_REVIEW"
    if identity.status in {"PENDING_REVIEW", "REJECTED", "EXPIRED", "VERIFIED"} and mapped != identity.status:
        raise ValueError("invalid identity verification transition")
    if attempt.status in {"PENDING_REVIEW", "REJECTED", "EXPIRED"} and mapped != attempt.status:
        raise ValueError("invalid identity verification transition")
    db.add(IdentityVerificationEvent(
        provider="metamap",
        provider_event_id=event_id[:128],
        identity_verification_attempt_id=attempt.id,
        event_name=event_name[:64],
        payload_hash=payload_hash,
        status="PROCESSED",
        reason_code="SANDBOX_NON_AUTHORITATIVE" if mapped == "PENDING_REVIEW" else None,
        processed_at=datetime.utcnow(),
    ))
    attempt.external_reference = event_id[:128]
    attempt.status = mapped
    identity.external_reference = event_id[:128]
    identity.provider = "metamap"
    identity.status = mapped
    identity.next_action_code = "REVIEW" if mapped == "PENDING_REVIEW" else None
    return "PROCESSED"
