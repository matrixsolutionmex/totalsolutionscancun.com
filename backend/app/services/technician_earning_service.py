import hashlib
import importlib
import os
import re
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Protocol

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.identity_human_review import IdentityHumanReviewDecision
from app.models.identity_provider import IdentityVerificationAttempt, IdentityVerificationEvent
from app.models.identity_verification import IdentityVerification
from app.models.organization import Organization
from app.models.payment import Payment
from app.models.organization_payment_policy import OrganizationPaymentPolicy
from app.models.service_order import ServiceOrder
from app.models.service_order_completion import ServiceOrderCustomerAcceptance, ServiceOrderTechnicalCompletion, ServiceOrderWarranty
from app.models.service_order_warranty_claim import ServiceOrderWarrantyClaim
from app.models.technician_earning import (
    EARNING_EVENT_TYPES,
    EARNING_REASON_CODES,
    EARNING_STATUSES,
    TechnicianEarning,
    TechnicianEarningEvent,
)
from app.models.user import User


TECHNICIAN_ROLES = frozenset({"BROKER", "TECNICO", "TÉCNICO"})
RESERVED_PAYOUT_STATUSES = frozenset({"PAYMENT_REQUESTED", "PAID"})
ALLOWED_TRANSITIONS = {
    "PROCESSING": {"IN_GUARANTEE", "HELD", "REVERSED"},
    "IN_GUARANTEE": {"AVAILABLE_FOR_PAYMENT", "HELD", "REVERSED"},
    "AVAILABLE_FOR_PAYMENT": {"HELD", "REVERSED"},
    "HELD": {"AVAILABLE_FOR_PAYMENT", "REVERSED"},
}


class MembershipGate(Protocol):
    def __call__(self, db: Session, user_id: int, organization_id: int): ...


_membership_gate_initialized = False
_membership_gate: MembershipGate | None = None


def _default_membership_gate() -> MembershipGate | None:
    """Resolve NETWORK-01 lazily; absent capability must never grant eligibility."""
    global _membership_gate_initialized, _membership_gate
    if _membership_gate_initialized:
        return _membership_gate
    _membership_gate_initialized = True
    try:
        module = importlib.import_module("app.models.organization_membership")
        model = getattr(module, "OrganizationMembership")
    except (ImportError, AttributeError):
        return None

    def query_membership(db: Session, user_id: int, organization_id: int):
        return db.query(model).filter_by(user_id=user_id, organization_id=organization_id).first()

    _membership_gate = query_membership
    return _membership_gate


def _membership_gate_result(
    db: Session,
    *,
    user_id: int,
    organization_id: int,
    membership_gate: MembershipGate | None = None,
) -> str | None:
    resolver = membership_gate if membership_gate is not None else _default_membership_gate()
    if resolver is None:
        return "MEMBERSHIP_GATE_UNAVAILABLE"
    try:
        membership = resolver(db, user_id, organization_id)
    except SQLAlchemyError:
        db.rollback()
        return "MEMBERSHIP_GATE_UNAVAILABLE"
    if (
        not membership
        or getattr(membership, "user_id", user_id) != user_id
        or getattr(membership, "organization_id", organization_id) != organization_id
        or membership.status != "ACTIVE"
        or not membership.is_operational
    ):
        return "MEMBERSHIP_NOT_OPERATIONAL"
    return None


def technician_earnings_enabled() -> bool:
    return os.getenv("TECHNICIAN_EARNINGS_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _hash_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _currency(value: str) -> str:
    normalized = str(value or "").strip().upper()
    if len(normalized) != 3 or not normalized.isalpha():
        raise ValueError("invalid currency")
    return normalized


def _safe_error_reasons(*reasons: str) -> list[str]:
    return list(dict.fromkeys(reasons))


def evaluate_technician_earning_eligibility(
    db: Session,
    order: ServiceOrder,
    *,
    technician_id: int | None = None,
    membership_gate: MembershipGate | None = None,
) -> dict:
    """Read-only gate evaluation. It never creates or updates an earning."""
    reasons: list[str] = []
    technician_id = technician_id or order.responsible_user_id
    technician = db.query(User).filter(User.id == technician_id).first() if technician_id else None
    organization = db.query(Organization).filter(Organization.id == order.organization_id).first() if order.organization_id else None
    if not technician or technician.role not in TECHNICIAN_ROLES:
        reasons.append("TECHNICIAN_INVALID")
    if not technician or not technician.is_active or (technician.status or "").upper() != "ACTIVE":
        reasons.append("USER_NOT_ACTIVE")
    if not organization or (organization.status or "").upper() != "ACTIVE":
        reasons.append("ORGANIZATION_NOT_ACTIVE")
    if not order.organization_id or technician and technician.organization_id != order.organization_id:
        reasons.append("TENANT_MISMATCH")
    if order.responsible_user_id != technician_id:
        reasons.append("TECHNICIAN_NOT_ASSIGNED")

    membership_reason = _membership_gate_result(
        db,
        user_id=technician_id,
        organization_id=order.organization_id,
        membership_gate=membership_gate,
    ) if technician_id and order.organization_id else "MEMBERSHIP_GATE_UNAVAILABLE"
    if membership_reason:
        reasons.append(membership_reason)

    identity = db.query(IdentityVerification).filter_by(
        user_id=technician_id, organization_id=order.organization_id,
    ).first()
    if not identity or identity.status != "VERIFIED":
        reasons.append("IDENTITY_NOT_VERIFIED")
    elif identity.expires_at and identity.expires_at <= datetime.utcnow():
        reasons.append("IDENTITY_EXPIRED")
    elif not identity.provider:
        reasons.append("IDENTITY_PROVIDER_MISSING")
    else:
        live_attempt = db.query(IdentityVerificationAttempt).filter_by(
            identity_verification_id=identity.id, user_id=technician_id,
            organization_id=order.organization_id, provider_mode="live",
        ).order_by(IdentityVerificationAttempt.created_at.desc()).first()
        approved_event = db.query(IdentityVerificationEvent).filter_by(
            identity_verification_attempt_id=live_attempt.id if live_attempt else None,
            reason_code="PROVIDER_APPROVED",
        ).first() if live_attempt else None
        human_approval = db.query(IdentityHumanReviewDecision).filter_by(
            attempt_id=live_attempt.id if live_attempt else None,
            user_id=technician_id, organization_id=order.organization_id,
            decision="APPROVE",
        ).first() if live_attempt else None
        if not live_attempt or not approved_event or not human_approval:
            reasons.append("IDENTITY_LIVE_APPROVAL_INCOMPLETE")

    completion = db.query(ServiceOrderTechnicalCompletion).filter_by(
        service_order_id=order.id, organization_id=order.organization_id,
    ).first()
    if (order.status or "").upper() not in {"COMPLETED", "CONCLUIDA", "FINALIZADO", "FINALIZADA"}:
        reasons.append("SERVICE_NOT_COMPLETED")
    if not completion or completion.status != "REVIEWED":
        reasons.append("COMPLETION_NOT_REVIEWED")
    acceptance = db.query(ServiceOrderCustomerAcceptance).filter_by(
        service_order_id=order.id, organization_id=order.organization_id, status="ACCEPTED",
    ).first()
    if not acceptance:
        reasons.append("CUSTOMER_ACCEPTANCE_MISSING")

    warranty = db.query(ServiceOrderWarranty).filter_by(
        service_order_id=order.id, organization_id=order.organization_id,
    ).first()
    if not warranty or warranty.status == "ACTIVE" and warranty.ends_at > datetime.utcnow():
        reasons.append("WARRANTY_NOT_CLOSED")
    open_claim = db.query(ServiceOrderWarrantyClaim).filter(
        ServiceOrderWarrantyClaim.service_order_id == order.id,
        ServiceOrderWarrantyClaim.organization_id == order.organization_id,
        ServiceOrderWarrantyClaim.status.in_({"OPEN", "UNDER_REVIEW", "APPROVED", "TECHNICIAN_ASSIGNED", "IN_PROGRESS", "RESOLVED"}),
    ).first()
    if open_claim:
        reasons.append("WARRANTY_CLAIM_OPEN")

    payment = db.query(Payment).filter_by(
        service_order_id=order.id, organization_id=order.organization_id,
    ).order_by(Payment.created_at.desc(), Payment.id.desc()).first()
    if not payment or payment.status not in {"PAID", "PAID_CASH"}:
        reasons.append("PAYMENT_NOT_CONFIRMED")
    elif payment.status == "PAID_CASH" or payment.payment_method == "CASH":
        reasons.append("CASH_PAYMENT_NOT_ELIGIBLE")
    if payment and payment.status in {"REFUNDED", "PARTIALLY_REFUNDED", "DISPUTED"}:
        reasons.append("REFUND_OR_DISPUTE")

    policy = db.query(OrganizationPaymentPolicy).filter_by(organization_id=order.organization_id).first()
    if not policy:
        reasons.append("FINANCIAL_POLICY_MISSING")

    reasons = _safe_error_reasons(*reasons)
    return {"eligible": not reasons, "reason_codes": reasons}


def _append_event(db: Session, earning: TechnicianEarning, *, event_type: str, previous_status: str | None,
                  new_status: str, reason_code: str, idempotency_key: str, actor_user_id: int | None = None):
    if event_type not in EARNING_EVENT_TYPES or reason_code not in EARNING_REASON_CODES:
        raise ValueError("invalid earning event")
    event = TechnicianEarningEvent(
        earning_id=earning.id, organization_id=earning.organization_id, event_type=event_type,
        previous_status=previous_status, new_status=new_status, actor_user_id=actor_user_id,
        reason_code=reason_code, idempotency_key_hash=_hash_key(idempotency_key),
    )
    db.add(event)
    db.flush()
    return event


def create_technician_earning(db: Session, *, organization_id: int, service_order_id: int,
                              technician_user_id: int, gross_amount, platform_fee_amount,
                              processing_fee_amount, currency: str, policy_version: str,
                              source_type: str, source_reference: str, idempotency_key: str,
                              payment_id: int | None = None) -> TechnicianEarning:
    if not technician_earnings_enabled():
        raise RuntimeError("technician earnings unavailable")
    if not policy_version.strip() or not source_type.strip() or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", source_reference.strip()):
        raise ValueError("earning source is not opaque")
    gross = _money(gross_amount)
    platform_fee = _money(platform_fee_amount)
    processing_fee = _money(processing_fee_amount)
    net = _money(gross - platform_fee - processing_fee)
    if min(gross, platform_fee, processing_fee, net) < 0 or net != gross - platform_fee - processing_fee:
        raise ValueError("earning amount is inconsistent")
    normalized_currency = _currency(currency)
    existing = db.query(TechnicianEarning).filter_by(idempotency_key_hash=_hash_key(idempotency_key)).first()
    if existing:
        return existing
    earning = TechnicianEarning(
        organization_id=organization_id, service_order_id=service_order_id,
        technician_user_id=technician_user_id, payment_id=payment_id, currency=normalized_currency,
        gross_amount=gross, platform_fee_amount=platform_fee, processing_fee_amount=processing_fee,
        net_amount=net, policy_version=policy_version.strip()[:64], source_type=source_type.strip()[:40],
        source_reference=source_reference.strip()[:160], status="PROCESSING",
        idempotency_key_hash=_hash_key(idempotency_key),
    )
    db.add(earning)
    db.flush()
    _append_event(db, earning, event_type="CREATED", previous_status=None, new_status="PROCESSING",
                  reason_code="EARNING_CREATED", idempotency_key=f"earning-created:{idempotency_key}")
    return earning


def transition_technician_earning(db: Session, *, earning_id: int, organization_id: int,
                                  new_status: str, reason_code: str, idempotency_key: str,
                                  actor_user_id: int | None = None) -> TechnicianEarning:
    if not technician_earnings_enabled():
        raise RuntimeError("technician earnings unavailable")
    if new_status not in EARNING_STATUSES or new_status in RESERVED_PAYOUT_STATUSES:
        raise ValueError("earning status is not available in this phase")
    if reason_code not in EARNING_REASON_CODES or not idempotency_key.strip():
        raise ValueError("invalid earning transition")
    earning = db.query(TechnicianEarning).filter_by(id=earning_id, organization_id=organization_id).with_for_update().first()
    if not earning:
        raise LookupError("earning not found")
    existing = db.query(TechnicianEarningEvent).filter_by(idempotency_key_hash=_hash_key(idempotency_key)).first()
    if existing:
        return earning
    if new_status not in ALLOWED_TRANSITIONS.get(earning.status, set()):
        raise ValueError("invalid earning transition")
    previous = earning.status
    earning.status = new_status
    earning.updated_at = datetime.utcnow()
    _append_event(db, earning, event_type="STATUS_CHANGED", previous_status=previous, new_status=new_status,
                  reason_code=reason_code, idempotency_key=idempotency_key, actor_user_id=actor_user_id)
    return earning


def reverse_technician_earning(db: Session, *, earning_id: int, organization_id: int,
                               idempotency_key: str, reason_code: str = "CORRECTION",
                               actor_user_id: int | None = None) -> TechnicianEarning:
    if not technician_earnings_enabled():
        raise RuntimeError("technician earnings unavailable")
    if reason_code not in EARNING_REASON_CODES:
        raise ValueError("invalid reversal reason")
    original = db.query(TechnicianEarning).filter_by(id=earning_id, organization_id=organization_id).with_for_update().first()
    if not original:
        raise LookupError("earning not found")
    existing = db.query(TechnicianEarning).filter_by(idempotency_key_hash=_hash_key(idempotency_key)).first()
    if existing:
        return existing
    if original.status == "REVERSED":
        raise ValueError("earning already reversed")
    if db.query(TechnicianEarning).filter_by(reversal_of_id=original.id).first():
        raise ValueError("earning already reversed")
    original_status = original.status
    original.status = "REVERSED"
    reversal = TechnicianEarning(
        organization_id=original.organization_id, service_order_id=original.service_order_id,
        technician_user_id=original.technician_user_id, payment_id=original.payment_id,
        currency=original.currency, gross_amount=original.gross_amount,
        platform_fee_amount=original.platform_fee_amount, processing_fee_amount=original.processing_fee_amount,
        net_amount=original.net_amount, policy_version=original.policy_version,
        source_type="REVERSAL", source_reference=f"earning-{original.id}", status="REVERSED",
        reversal_of_id=original.id, idempotency_key_hash=_hash_key(idempotency_key),
    )
    db.add(reversal)
    db.flush()
    _append_event(db, original, event_type="STATUS_CHANGED", previous_status=original_status,
                  new_status="REVERSED", reason_code=reason_code, idempotency_key=f"earning-reversed:{idempotency_key}", actor_user_id=actor_user_id)
    _append_event(db, reversal, event_type="REVERSED", previous_status=None, new_status="REVERSED",
                  reason_code=reason_code, idempotency_key=f"reversal-created:{idempotency_key}", actor_user_id=actor_user_id)
    return reversal


def earning_payload(row: TechnicianEarning) -> dict:
    return {
        "id": row.id, "service_order_id": row.service_order_id, "currency": row.currency,
        "gross_amount": str(row.gross_amount), "platform_fee_amount": str(row.platform_fee_amount),
        "processing_fee_amount": str(row.processing_fee_amount), "net_amount": str(row.net_amount),
        "policy_version": row.policy_version, "source_type": row.source_type, "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }
