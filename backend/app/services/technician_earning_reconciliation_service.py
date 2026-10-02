"""Provider-agnostic recognition of technician earnings from confirmed payments.

This module is deliberately event-driven and has no worker or startup side effect.
The automation flag is separate from the read-only earnings UI flag and defaults off.
"""

import hashlib
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import NamedTuple

from sqlalchemy import inspect, func
from sqlalchemy.orm import Session

from app.models.payment import Payment
from app.models.service_order import ServiceOrder
from app.models.service_order_completion import ServiceOrderCustomerAcceptance, ServiceOrderWarranty
from app.models.service_order_warranty_claim import ServiceOrderWarrantyClaim
from app.models.stripe_reconciliation import StripePaymentAdjustment
from app.models.technician_compensation import ServiceOrderCompensationSnapshot, TechnicianCompensationPolicy
from app.models.technician_earning import TechnicianEarning, TechnicianEarningEvent
from app.models.technician_earning_reconciliation import TechnicianEarningReconciliationEvent
from app.models.organization import Organization
from app.services.technician_earning_service import _membership_gate_result


MONEY = Decimal("0.01")
AUTOMATION_SOURCE = "AUTOMATED_PAYMENT"
REVERSAL_SOURCE = "AUTOMATED_ADJUSTMENT"
_schema_cache: dict[int, bool] = {}
ROLLOUT_MODES = frozenset({"off", "canary", "all"})
logger = logging.getLogger(__name__)


class AutomationRolloutConfig(NamedTuple):
    valid: bool
    mode: str
    organization_ids: frozenset[int]
    reason_code: str | None = None


def technician_earning_automation_enabled() -> bool:
    return os.getenv("TECHNICIAN_EARNING_AUTOMATION_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def technician_earning_automation_rollout() -> AutomationRolloutConfig:
    """Parse rollout configuration without ever granting access on malformed input."""
    mode = os.getenv("TECHNICIAN_EARNING_AUTOMATION_ROLLOUT_MODE", "off").strip().lower()
    raw_allowlist = os.getenv("TECHNICIAN_EARNING_AUTOMATION_CANARY_ORGANIZATION_IDS", "").strip()
    if mode not in ROLLOUT_MODES:
        return AutomationRolloutConfig(False, mode, frozenset(), "AUTOMATION_ROLLOUT_INVALID")
    if mode != "canary" and raw_allowlist:
        return AutomationRolloutConfig(False, mode, frozenset(), "AUTOMATION_ROLLOUT_INVALID")
    if mode != "canary":
        return AutomationRolloutConfig(True, mode, frozenset())
    tokens = [token.strip() for token in raw_allowlist.split(",")]
    if not raw_allowlist or any(not token.isascii() or not token.isdigit() or int(token) <= 0 for token in tokens):
        return AutomationRolloutConfig(False, mode, frozenset(), "AUTOMATION_ROLLOUT_INVALID")
    ids = [int(token) for token in tokens]
    if len(ids) != len(set(ids)):
        return AutomationRolloutConfig(False, mode, frozenset(), "AUTOMATION_ROLLOUT_INVALID")
    return AutomationRolloutConfig(True, mode, frozenset(ids))


def _new_earning_rollout_reason(organization_id: int, config: AutomationRolloutConfig | None = None) -> str | None:
    config = config or technician_earning_automation_rollout()
    if not config.valid:
        return config.reason_code
    if config.mode == "off":
        return "AUTOMATION_ROLLOUT_OFF"
    if config.mode == "canary" and organization_id not in config.organization_ids:
        return "AUTOMATION_ORGANIZATION_NOT_AUTHORIZED"
    return None


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(MONEY, rounding=ROUND_HALF_UP)


def _utc_now(value: datetime | None = None) -> datetime:
    """Require explicit timezone-aware clock input for release decisions."""
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware UTC datetime required")
    return value.astimezone(timezone.utc)


def _stored_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _hash_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _schema_available(db: Session) -> bool:
    bind = db.get_bind()
    cache_key = id(bind)
    if cache_key not in _schema_cache:
        _schema_cache[cache_key] = inspect(bind).has_table(TechnicianEarningReconciliationEvent.__tablename__)
    return _schema_cache[cache_key]


def _eligible_total(snapshot: ServiceOrderCompensationSnapshot) -> Decimal:
    # Materials, taxes and reimbursements are intentionally excluded from accrual.
    return max(Decimal("0.00"), _money(snapshot.labor_base_amount) - _money(snapshot.discount_amount))


def _recognized_total(db: Session, order_id: int, technician_id: int, currency: str) -> Decimal:
    positives = db.query(func.coalesce(func.sum(TechnicianEarning.net_amount), 0)).filter(
        TechnicianEarning.service_order_id == order_id,
        TechnicianEarning.technician_user_id == technician_id,
        TechnicianEarning.currency == currency,
        TechnicianEarning.source_type == AUTOMATION_SOURCE,
        TechnicianEarning.status != "REVERSED",
    ).scalar() or 0
    reversals = db.query(func.coalesce(func.sum(TechnicianEarning.net_amount), 0)).filter(
        TechnicianEarning.service_order_id == order_id,
        TechnicianEarning.technician_user_id == technician_id,
        TechnicianEarning.currency == currency,
        TechnicianEarning.source_type == REVERSAL_SOURCE,
    ).scalar() or 0
    return max(Decimal("0.00"), _money(positives) - _money(reversals))


def _initial_status(db: Session, order: ServiceOrder) -> str:
    acceptance = db.query(ServiceOrderCustomerAcceptance).filter_by(
        service_order_id=order.id, organization_id=order.organization_id, status="ACCEPTED",
    ).first()
    if not acceptance:
        return "PROCESSING"
    warranty = db.query(ServiceOrderWarranty).filter_by(
        service_order_id=order.id, organization_id=order.organization_id,
    ).first()
    if warranty and warranty.status == "ACTIVE" and warranty.ends_at and _stored_utc(warranty.ends_at) > _utc_now():
        return "IN_GUARANTEE"
    return "AVAILABLE_FOR_PAYMENT"


def _append_event(db: Session, earning: TechnicianEarning, *, event_type: str, reason_code: str, key: str,
                  previous_status: str | None, new_status: str):
    event = TechnicianEarningEvent(
        earning_id=earning.id, organization_id=earning.organization_id, event_type=event_type,
        previous_status=previous_status, new_status=new_status, reason_code=reason_code,
        idempotency_key_hash=_hash_key(key),
    )
    db.add(event)
    db.flush()
    return event


def _eligible_snapshot(db: Session, payment: Payment):
    if not payment.service_order_id or not payment.technician_id:
        return None, "PAYMENT_NOT_LINKED_TO_SERVICE_ORDER"
    order = db.query(ServiceOrder).filter_by(id=payment.service_order_id, organization_id=payment.organization_id).with_for_update().first()
    if not order:
        return None, "SERVICE_ORDER_NOT_FOUND"
    organization = db.query(Organization).filter_by(id=order.organization_id).first()
    if not organization or (organization.status or "").upper() != "ACTIVE":
        return None, "ORGANIZATION_NOT_ACTIVE"
    snapshot = db.query(ServiceOrderCompensationSnapshot).filter_by(
        service_order_id=order.id, organization_id=order.organization_id,
        technician_user_id=payment.technician_id, status="FROZEN",
    ).order_by(ServiceOrderCompensationSnapshot.id.desc()).first()
    if not snapshot:
        return None, "FROZEN_SNAPSHOT_REQUIRED"
    if str(snapshot.currency).upper() != str(payment.currency).upper():
        return None, "CURRENCY_MISMATCH"
    if order.responsible_user_id != payment.technician_id:
        return None, "TECHNICIAN_MISMATCH"
    policy = db.query(TechnicianCompensationPolicy).filter_by(
        id=snapshot.policy_id, organization_id=order.organization_id,
        currency=snapshot.currency, version=snapshot.policy_version,
    ).first()
    if not policy or policy.status not in {"ACTIVE", "RETIRED"}:
        return None, "POLICY_SNAPSHOT_MISMATCH"
    if not order.created_at or _stored_utc(policy.effective_from) > _stored_utc(order.created_at):
        return None, "ORDER_BEFORE_POLICY_CUTOFF"
    membership_reason = _membership_gate_result(
        db, user_id=payment.technician_id, organization_id=payment.organization_id,
    )
    if membership_reason:
        return None, membership_reason
    return (order, snapshot), None


def _record_reconciliation(db: Session, *, payment: Payment, order: ServiceOrder, event_type: str,
                           provider_event_key: str, amount: Decimal, earning_id: int | None = None,
                           reversal_of_earning_id: int | None = None):
    row = TechnicianEarningReconciliationEvent(
        organization_id=payment.organization_id, service_order_id=order.id,
        technician_user_id=payment.technician_id, payment_id=payment.id,
        installment_id=payment.installment_id, earning_id=earning_id,
        reversal_of_earning_id=reversal_of_earning_id,
        provider_event_key_hash=_hash_key(provider_event_key), event_type=event_type,
        amount=_money(amount), currency=str(payment.currency).upper(),
    )
    db.add(row)
    db.flush()
    return row


def reconcile_confirmed_payment(db: Session, payment: Payment, *, provider_event_key: str,
                                confirmed_amount=None) -> dict:
    if not technician_earning_automation_enabled():
        return {"recognized": False, "reason_code": "AUTOMATION_DISABLED"}
    rollout = technician_earning_automation_rollout()
    if not rollout.valid:
        logger.warning("technician earning automation rollout rejected: %s", rollout.reason_code)
        return {"recognized": False, "reason_code": rollout.reason_code}
    if rollout.mode == "off":
        return {"recognized": False, "reason_code": "AUTOMATION_ROLLOUT_OFF"}
    if not _schema_available(db):
        return {"recognized": False, "reason_code": "AUTOMATION_SCHEMA_UNAVAILABLE"}
    if payment.status not in {"PAID", "SUCCEEDED"}:
        return {"recognized": False, "reason_code": "PAYMENT_NOT_CONFIRMED"}
    if not provider_event_key or not payment.technician_id:
        return {"recognized": False, "reason_code": "RECONCILIATION_KEY_REQUIRED"}
    existing = db.query(TechnicianEarningReconciliationEvent).filter_by(
        provider_event_key_hash=_hash_key(provider_event_key),
    ).first()
    if existing:
        return {"recognized": False, "reason_code": "DUPLICATE_PROVIDER_EVENT", "earning_id": existing.earning_id}
    context, reason = _eligible_snapshot(db, payment)
    if reason:
        return {"recognized": False, "reason_code": reason}
    order, snapshot = context
    rollout_reason = _new_earning_rollout_reason(order.organization_id, rollout)
    if rollout_reason:
        return {"recognized": False, "reason_code": rollout_reason}
    amount = _money(confirmed_amount if confirmed_amount is not None else payment.gross_amount)
    eligible_total = _eligible_total(snapshot)
    if amount <= 0 or eligible_total <= 0:
        return {"recognized": False, "reason_code": "INELIGIBLE_AMOUNT"}
    confirmed = db.query(func.coalesce(func.sum(TechnicianEarningReconciliationEvent.amount), 0)).filter(
        TechnicianEarningReconciliationEvent.service_order_id == order.id,
        TechnicianEarningReconciliationEvent.technician_user_id == payment.technician_id,
        TechnicianEarningReconciliationEvent.currency == str(payment.currency).upper(),
        TechnicianEarningReconciliationEvent.event_type == "PAYMENT_CONFIRMED",
    ).scalar() or 0
    adjusted = db.query(func.coalesce(func.sum(TechnicianEarningReconciliationEvent.amount), 0)).filter(
        TechnicianEarningReconciliationEvent.service_order_id == order.id,
        TechnicianEarningReconciliationEvent.technician_user_id == payment.technician_id,
        TechnicianEarningReconciliationEvent.currency == str(payment.currency).upper(),
        TechnicianEarningReconciliationEvent.event_type.in_({"REFUND", "DISPUTE_OPEN", "DISPUTE_LOST"}),
    ).scalar() or 0
    recovered = db.query(func.coalesce(func.sum(TechnicianEarningReconciliationEvent.amount), 0)).filter(
        TechnicianEarningReconciliationEvent.service_order_id == order.id,
        TechnicianEarningReconciliationEvent.technician_user_id == payment.technician_id,
        TechnicianEarningReconciliationEvent.currency == str(payment.currency).upper(),
        TechnicianEarningReconciliationEvent.event_type == "DISPUTE_WON",
    ).scalar() or 0
    cumulative = min(eligible_total, max(Decimal("0.00"), _money(confirmed) - _money(adjusted) + _money(recovered) + amount))
    target = min(_money(snapshot.technician_amount), _money(_money(snapshot.technician_amount) * cumulative / eligible_total))
    delta = _money(target - _recognized_total(db, order.id, payment.technician_id, str(payment.currency).upper()))
    reconciliation = _record_reconciliation(
        db, payment=payment, order=order, event_type="PAYMENT_CONFIRMED", provider_event_key=provider_event_key,
        amount=amount,
    )
    if delta <= 0:
        return {"recognized": False, "reason_code": "NO_NEW_DELTA", "reconciliation_id": reconciliation.id}
    earning = TechnicianEarning(
        organization_id=order.organization_id, service_order_id=order.id, technician_user_id=payment.technician_id,
        payment_id=payment.id, currency=str(payment.currency).upper(), gross_amount=delta,
        platform_fee_amount=Decimal("0.00"), processing_fee_amount=Decimal("0.00"), net_amount=delta,
        policy_version=str(snapshot.policy_version), source_type=AUTOMATION_SOURCE,
        source_reference=f"payment-{payment.id}-event-{reconciliation.id}", status=_initial_status(db, order),
        idempotency_key_hash=_hash_key(f"earning-payment:{provider_event_key}"),
    )
    db.add(earning)
    db.flush()
    reconciliation.earning_id = earning.id
    _append_event(db, earning, event_type="EARNING_RECOGNIZED", reason_code="PAYMENT_CONFIRMED",
                  key=f"earning-event:{provider_event_key}", previous_status=None, new_status=earning.status)
    return {"recognized": True, "earning_id": earning.id, "delta": str(delta), "status": earning.status}


def reconcile_payment_adjustment(db: Session, payment: Payment, *, provider_event_key: str,
                                 amount, event_type: str) -> dict:
    if not technician_earning_automation_enabled():
        return {"adjusted": False, "reason_code": "AUTOMATION_DISABLED"}
    if event_type not in {"REFUND", "DISPUTE_OPEN", "DISPUTE_LOST", "DISPUTE_WON"}:
        raise ValueError("unsupported payment adjustment")
    if not _schema_available(db):
        return {"adjusted": False, "reason_code": "AUTOMATION_SCHEMA_UNAVAILABLE"}
    context, reason = _eligible_snapshot(db, payment)
    if reason:
        return {"adjusted": False, "reason_code": reason}
    order, snapshot = context
    if db.query(TechnicianEarningReconciliationEvent).filter_by(provider_event_key_hash=_hash_key(provider_event_key)).first():
        return {"adjusted": False, "reason_code": "DUPLICATE_PROVIDER_EVENT"}
    amount = _money(amount)
    if amount <= 0:
        raise ValueError("adjustment amount must be positive")
    currency = str(payment.currency).upper()
    existing_positive = db.query(TechnicianEarning).filter(
        TechnicianEarning.service_order_id == order.id,
        TechnicianEarning.organization_id == order.organization_id,
        TechnicianEarning.technician_user_id == payment.technician_id,
        TechnicianEarning.source_type == AUTOMATION_SOURCE,
        TechnicianEarning.status != "REVERSED",
    ).first()
    if event_type == "DISPUTE_WON" and not existing_positive:
        reconciliation = _record_reconciliation(
            db, payment=payment, order=order, event_type=event_type,
            provider_event_key=provider_event_key, amount=amount,
        )
        return {"adjusted": False, "reason_code": "NO_EXISTING_EARNING", "reconciliation_id": reconciliation.id}
    recognized = _recognized_total(db, order.id, payment.technician_id, currency)
    confirmed = db.query(func.coalesce(func.sum(TechnicianEarningReconciliationEvent.amount), 0)).filter(
        TechnicianEarningReconciliationEvent.service_order_id == order.id,
        TechnicianEarningReconciliationEvent.technician_user_id == payment.technician_id,
        TechnicianEarningReconciliationEvent.currency == currency,
        TechnicianEarningReconciliationEvent.event_type == "PAYMENT_CONFIRMED",
    ).scalar() or 0
    reductions = db.query(func.coalesce(func.sum(TechnicianEarningReconciliationEvent.amount), 0)).filter(
        TechnicianEarningReconciliationEvent.service_order_id == order.id,
        TechnicianEarningReconciliationEvent.technician_user_id == payment.technician_id,
        TechnicianEarningReconciliationEvent.currency == currency,
        TechnicianEarningReconciliationEvent.event_type.in_({"REFUND", "DISPUTE_OPEN", "DISPUTE_LOST"}),
    ).scalar() or 0
    recoveries = db.query(func.coalesce(func.sum(TechnicianEarningReconciliationEvent.amount), 0)).filter(
        TechnicianEarningReconciliationEvent.service_order_id == order.id,
        TechnicianEarningReconciliationEvent.technician_user_id == payment.technician_id,
        TechnicianEarningReconciliationEvent.currency == currency,
        TechnicianEarningReconciliationEvent.event_type == "DISPUTE_WON",
    ).scalar() or 0
    net_paid = max(Decimal("0.00"), _money(confirmed) - _money(reductions) + _money(recoveries))
    if event_type == "DISPUTE_WON":
        net_paid += amount
    else:
        net_paid = max(Decimal("0.00"), net_paid - amount)
    desired = min(_money(snapshot.technician_amount), _money(_money(snapshot.technician_amount) * min(net_paid, _eligible_total(snapshot)) / _eligible_total(snapshot)))
    delta = _money(desired - recognized)
    reconciliation = _record_reconciliation(db, payment=payment, order=order, event_type=event_type,
                                             provider_event_key=provider_event_key, amount=amount)
    if delta > 0:
        earning = TechnicianEarning(
            organization_id=order.organization_id, service_order_id=order.id, technician_user_id=payment.technician_id,
            payment_id=payment.id, currency=currency, gross_amount=delta,
            platform_fee_amount=Decimal("0.00"), processing_fee_amount=Decimal("0.00"), net_amount=delta,
            policy_version=str(snapshot.policy_version), source_type=AUTOMATION_SOURCE,
            source_reference=f"{event_type.lower()}-recovery-{payment.id}-{reconciliation.id}", status=_initial_status(db, order),
            idempotency_key_hash=_hash_key(f"earning-adjustment:{provider_event_key}"),
        )
        db.add(earning)
        db.flush()
        reconciliation.earning_id = earning.id
        _append_event(db, earning, event_type="EARNING_ADJUSTED", reason_code="REFUND_OR_DISPUTE_ADJUSTMENT",
                      key=f"earning-adjustment-event:{provider_event_key}", previous_status=None, new_status=earning.status)
        return {"adjusted": True, "earning_id": earning.id, "delta": str(delta)}
    if delta == 0:
        return {"adjusted": False, "reason_code": "NO_RECOGNIZED_AMOUNT", "reconciliation_id": reconciliation.id}
    remaining = abs(delta)
    originals = db.query(TechnicianEarning).filter(
        TechnicianEarning.organization_id == order.organization_id,
        TechnicianEarning.service_order_id == order.id,
        TechnicianEarning.technician_user_id == payment.technician_id,
        TechnicianEarning.payment_id == payment.id,
        TechnicianEarning.currency == currency,
        TechnicianEarning.source_type == AUTOMATION_SOURCE,
        TechnicianEarning.status != "REVERSED",
    ).order_by(TechnicianEarning.created_at.desc(), TechnicianEarning.id.desc()).with_for_update().all()
    reversals = []
    for original in originals:
        already_reversed = db.query(func.coalesce(func.sum(TechnicianEarning.net_amount), 0)).filter(
            TechnicianEarning.reversal_of_id == original.id,
            TechnicianEarning.source_type == REVERSAL_SOURCE,
        ).scalar() or 0
        available = max(Decimal("0.00"), _money(original.net_amount) - _money(already_reversed))
        allocation = min(remaining, available)
        if allocation <= 0:
            continue
        reversal_key = f"earning-adjustment:{provider_event_key}:original:{original.id}"
        reversal = TechnicianEarning(
            organization_id=order.organization_id, service_order_id=order.id,
            technician_user_id=payment.technician_id, payment_id=payment.id,
            currency=currency, gross_amount=allocation,
            platform_fee_amount=Decimal("0.00"), processing_fee_amount=Decimal("0.00"), net_amount=allocation,
            policy_version=str(snapshot.policy_version), source_type=REVERSAL_SOURCE,
            source_reference=f"{event_type.lower()}-{payment.id}-{original.id}", status="REVERSED",
            reversal_of_id=original.id, idempotency_key_hash=_hash_key(reversal_key),
        )
        db.add(reversal)
        db.flush()
        if not reversals:
            reconciliation.reversal_of_earning_id = original.id
            reconciliation.earning_id = reversal.id
        else:
            _record_reconciliation(
                db, payment=payment, order=order, event_type=event_type,
                provider_event_key=f"{provider_event_key}:original:{original.id}",
                amount=allocation, earning_id=reversal.id, reversal_of_earning_id=original.id,
            )
        _append_event(db, reversal, event_type="EARNING_REVERSED", reason_code="REFUND_OR_DISPUTE_ADJUSTMENT",
                      key=f"earning-adjustment-event:{provider_event_key}:original:{original.id}",
                      previous_status=None, new_status="REVERSED")
        reversals.append(reversal)
        remaining = _money(remaining - allocation)
        if remaining <= 0:
            break
    if remaining > 0:
        raise ValueError("reversal allocation exceeds linked positive earnings")
    return {"adjusted": True, "earning_id": reversals[0].id, "delta": str(abs(delta)),
            "reversal_ids": [row.id for row in reversals]}


def _transition_order_earnings(db: Session, order: ServiceOrder, *, new_status: str, event_type: str, reason: str, key: str):
    rows = db.query(TechnicianEarning).filter_by(
        service_order_id=order.id, organization_id=order.organization_id,
    ).filter(TechnicianEarning.status.notin_({"PAID", "PAYMENT_REQUESTED", "REVERSED"})).with_for_update().all()
    for earning in rows:
        if earning.status == new_status:
            continue
        previous = earning.status
        earning.status = new_status
        earning.updated_at = datetime.utcnow()
        _append_event(db, earning, event_type=event_type, reason_code=reason, key=f"{key}:earning:{earning.id}",
                      previous_status=previous, new_status=new_status)
    return len(rows)


def on_order_customer_accepted(db: Session, order: ServiceOrder, *, event_key: str):
    if not technician_earning_automation_enabled() or not _schema_available(db):
        return {"updated": 0, "reason_code": "AUTOMATION_DISABLED" if not technician_earning_automation_enabled() else "AUTOMATION_SCHEMA_UNAVAILABLE"}
    return {"updated": _transition_order_earnings(db, order, new_status="IN_GUARANTEE", event_type="GUARANTEE_STARTED", reason="GUARANTEE_STARTED", key=event_key)}


def on_guarantee_closed(db: Session, order: ServiceOrder, *, event_key: str):
    if not technician_earning_automation_enabled() or not _schema_available(db):
        return {"updated": 0, "reason_code": "AUTOMATION_DISABLED" if not technician_earning_automation_enabled() else "AUTOMATION_SCHEMA_UNAVAILABLE"}
    result = release_due_technician_earnings(
        db, _utc_now(), batch_size=100, service_order_id=order.id,
    )
    result["event_key"] = event_key
    return result


def release_due_technician_earnings(db: Session, now: datetime, batch_size: int,
                                    service_order_id: int | None = None, *, dry_run: bool = False) -> dict:
    """Release expired guarantees in bounded, lock-safe batches; never performs payout."""
    if not technician_earning_automation_enabled():
        return {"updated": 0, "reason_code": "AUTOMATION_DISABLED", "planned": []}
    if not _schema_available(db):
        return {"updated": 0, "reason_code": "AUTOMATION_SCHEMA_UNAVAILABLE", "planned": []}
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    aware_now = _utc_now(now)
    now_naive = aware_now.replace(tzinfo=None)
    query = db.query(TechnicianEarning).join(
        ServiceOrderWarranty,
        (ServiceOrderWarranty.service_order_id == TechnicianEarning.service_order_id)
        & (ServiceOrderWarranty.organization_id == TechnicianEarning.organization_id),
    ).filter(
        TechnicianEarning.status == "IN_GUARANTEE",
        ServiceOrderWarranty.status == "ACTIVE",
        ServiceOrderWarranty.ends_at <= now_naive,
    )
    if service_order_id is not None:
        query = query.filter(TechnicianEarning.service_order_id == service_order_id)
    query = query.order_by(TechnicianEarning.id)
    rows = query.limit(batch_size).all() if dry_run else query.with_for_update(skip_locked=True).limit(batch_size).all()
    planned = []
    updated = 0
    for earning in rows:
        order = db.query(ServiceOrder).filter_by(
            id=earning.service_order_id, organization_id=earning.organization_id,
        ).first()
        snapshot = db.query(ServiceOrderCompensationSnapshot).filter_by(
            service_order_id=earning.service_order_id, organization_id=earning.organization_id,
            technician_user_id=earning.technician_user_id, status="FROZEN",
            currency=earning.currency,
        ).order_by(ServiceOrderCompensationSnapshot.id.desc()).first()
        acceptance = db.query(ServiceOrderCustomerAcceptance).filter_by(
            service_order_id=earning.service_order_id, organization_id=earning.organization_id,
            status="ACCEPTED",
        ).first()
        open_claim = db.query(ServiceOrderWarrantyClaim).filter(
            ServiceOrderWarrantyClaim.service_order_id == earning.service_order_id,
            ServiceOrderWarrantyClaim.organization_id == earning.organization_id,
            ServiceOrderWarrantyClaim.status.in_({"OPEN", "UNDER_REVIEW", "APPROVED", "TECHNICIAN_ASSIGNED", "IN_PROGRESS", "RESOLVED"}),
        ).first()
        payment = db.query(Payment).filter_by(
            id=earning.payment_id, organization_id=earning.organization_id,
        ).first()
        dispute = db.query(StripePaymentAdjustment).filter(
            StripePaymentAdjustment.payment_id == earning.payment_id,
            StripePaymentAdjustment.kind == "DISPUTE",
            StripePaymentAdjustment.status != "WON",
        ).first()
        if (
            not order
            or order.status not in {"COMPLETED", "CONCLUIDA", "FINALIZADO", "FINALIZADA"}
            or not snapshot
            or not acceptance
            or open_claim
            or dispute
            or not payment
            or payment.status not in {"PAID", "SUCCEEDED"}
        ):
            continue
        planned.append(earning.id)
        if dry_run:
            continue
        earning.status = "AVAILABLE_FOR_PAYMENT"
        earning.updated_at = now_naive
        _append_event(db, earning, event_type="AVAILABLE_FOR_PAYMENT", reason_code="AVAILABLE_FOR_PAYMENT",
                      key=f"guarantee-expired:earning:{earning.id}", previous_status="IN_GUARANTEE",
                      new_status="AVAILABLE_FOR_PAYMENT")
        updated += 1
    return {"updated": updated, "planned": planned}
