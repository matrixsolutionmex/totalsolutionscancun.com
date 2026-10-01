import hashlib
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models.organization_membership import OrganizationMembership
from app.models.organization import Organization
from app.models.service_order import ServiceOrder
from app.models.service_order_quote import ServiceOrderQuote
from app.models.technician_compensation import (
    COMPENSATION_INSTALLMENT_RULE,
    COMPENSATION_ITEM_CATEGORIES,
    COMPENSATION_POLICY_STATUSES,
    COMPENSATION_SNAPSHOT_STATUSES,
    ServiceOrderCompensationSnapshot,
    TechnicianCompensationEvent,
    TechnicianCompensationPolicy,
)
from app.models.user import User


MONEY = Decimal("0.01")
POLICY_EVENT_TYPES = {"POLICY_CREATED", "POLICY_ACTIVATED"}
SNAPSHOT_EVENT_TYPES = {"SNAPSHOT_PROPOSED", "SNAPSHOT_FROZEN", "SNAPSHOT_VOIDED"}
ROLLOUT_MODES = {"off", "canary", "all"}
PUBLISHED_POLICY_STATUSES = {"ACTIVE", "RETIRED"}


class CompensationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def compensation_policy_enabled() -> bool:
    value = os.getenv("TECHNICIAN_COMPENSATION_POLICY_ENABLED", "false").strip().lower()
    if value not in {"", "0", "1", "false", "true", "no", "yes", "off", "on"}:
        raise CompensationError("COMPENSATION_POLICY_CONFIGURATION_INVALID")
    return value in {"1", "true", "yes", "on"}


def _rollout_organization_ids() -> set[int] | None:
    raw = os.getenv("TECHNICIAN_COMPENSATION_POLICY_CANARY_ORGANIZATION_IDS", "")
    tokens = [token.strip() for token in raw.split(",") if token.strip()]
    if not tokens:
        return None
    try:
        organization_ids = {int(token) for token in tokens}
    except (TypeError, ValueError):
        return None
    if any(organization_id <= 0 for organization_id in organization_ids):
        return None
    return organization_ids


def compensation_policy_enabled_for_organization(db: Session, organization_id: int | None) -> bool:
    """Resolve rollout server-side; malformed configuration always disables it."""
    try:
        if not compensation_policy_enabled() or not organization_id:
            return False
        mode = os.getenv("TECHNICIAN_COMPENSATION_POLICY_ROLLOUT_MODE", "off").strip().lower()
        if mode not in ROLLOUT_MODES or mode == "off":
            return False
        organization = db.query(Organization).filter(
            Organization.id == organization_id,
            Organization.status == "ACTIVE",
        ).first()
        if not organization:
            return False
        if mode == "all":
            return True
        organization_ids = _rollout_organization_ids()
        return organization_ids is not None and organization.id in organization_ids
    except Exception:
        return False


def _money(value) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(MONEY, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise CompensationError("MONEY_INVALID") from exc


def _currency(value: str) -> str:
    normalized = str(value or "").strip().upper()
    if len(normalized) != 3 or not normalized.isalpha():
        raise CompensationError("CURRENCY_INVALID")
    return normalized


def _utc_datetime(value: datetime, *, field: str) -> datetime:
    """Require an explicit UTC-capable timestamp and return UTC without tz storage."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CompensationError(f"{field}_TIMEZONE_REQUIRED")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _local_datetime_to_utc(value: str, timezone_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip() or not isinstance(timezone_name, str) or not timezone_name.strip():
        raise CompensationError("EFFECTIVE_FROM_REQUIRED")
    try:
        local_value = datetime.fromisoformat(value)
        zone = ZoneInfo(timezone_name.strip())
    except (TypeError, ValueError):
        raise CompensationError("EFFECTIVE_FROM_LOCAL_INVALID") from None
    except ZoneInfoNotFoundError:
        raise CompensationError("EFFECTIVE_FROM_TIMEZONE_INVALID") from None
    if local_value.tzinfo is not None or local_value.utcoffset() is not None:
        raise CompensationError("EFFECTIVE_FROM_LOCAL_INVALID")

    candidates = []
    for fold in (0, 1):
        aware = local_value.replace(tzinfo=zone, fold=fold)
        round_trip = aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
        if round_trip == local_value:
            candidates.append(aware)
    if not candidates:
        raise CompensationError("EFFECTIVE_FROM_LOCAL_NONEXISTENT")
    if len(candidates) == 2 and candidates[0].utcoffset() != candidates[1].utcoffset():
        raise CompensationError("EFFECTIVE_FROM_LOCAL_AMBIGUOUS")
    return candidates[0].astimezone(timezone.utc).replace(tzinfo=None)


def normalize_effective_from(*, effective_from: datetime | None = None,
                             effective_from_local: str | None = None,
                             effective_timezone: str | None = None) -> datetime:
    if effective_from_local is not None or effective_timezone is not None:
        if effective_from is not None:
            raise CompensationError("EFFECTIVE_FROM_INPUT_CONFLICT")
        return _local_datetime_to_utc(effective_from_local, effective_timezone)
    if effective_from is None:
        raise CompensationError("EFFECTIVE_FROM_REQUIRED")
    return _utc_datetime(effective_from, field="EFFECTIVE_FROM")


def _stored_utc(value: datetime | None) -> datetime | None:
    """Migration 089 stores UTC timestamps in TIMESTAMP WITHOUT TIME ZONE."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _hash_key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_admin(actor: User, organization_id: int) -> None:
    if actor.role not in {"ROOT", "GERENTE"}:
        raise CompensationError("ADMIN_REQUIRED")
    if actor.organization_id != organization_id:
        raise CompensationError("TENANT_MISMATCH")
    if not actor.is_active or str(actor.status or "").upper() != "ACTIVE":
        raise CompensationError("ACTOR_NOT_ACTIVE")


def _require_feature(db: Session, organization_id: int | None) -> None:
    if not compensation_policy_enabled_for_organization(db, organization_id):
        raise CompensationError("COMPENSATION_POLICY_UNAVAILABLE")


def _event(db: Session, *, organization_id: int, event_type: str, reason_code: str,
           idempotency_key: str, policy_id: int | None = None,
           snapshot_id: int | None = None, actor_user_id: int | None = None):
    if event_type not in POLICY_EVENT_TYPES | SNAPSHOT_EVENT_TYPES:
        raise CompensationError("EVENT_TYPE_INVALID")
    existing = db.query(TechnicianCompensationEvent).filter_by(
        idempotency_key_hash=_hash_key(idempotency_key),
    ).first()
    if existing:
        return existing
    row = TechnicianCompensationEvent(
        organization_id=organization_id, policy_id=policy_id, snapshot_id=snapshot_id,
        event_type=event_type, actor_user_id=actor_user_id, reason_code=reason_code,
        idempotency_key_hash=_hash_key(idempotency_key),
    )
    db.add(row)
    db.flush()
    return row


def policy_payload(row: TechnicianCompensationPolicy) -> dict:
    return {
        "id": row.id, "organization_id": row.organization_id, "version": row.version,
        "status": row.status, "currency": row.currency,
        "technician_share_bps": row.technician_share_bps,
        "organization_share_bps": row.organization_share_bps,
        "default_guarantee_days": row.default_guarantee_days,
        "installment_rule": row.installment_rule,
        "processor_fee_responsibility": row.processor_fee_responsibility,
        "discount_rule": row.discount_rule,
        "effective_from": f"{row.effective_from.isoformat()}Z" if row.effective_from else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def snapshot_payload(row: ServiceOrderCompensationSnapshot) -> dict:
    return {
        "id": row.id, "service_order_id": row.service_order_id,
        "technician_user_id": row.technician_user_id, "policy_version": row.policy_version,
        "currency": row.currency, "labor_base_amount": str(row.labor_base_amount),
        "material_amount": str(row.material_amount), "tax_amount": str(row.tax_amount),
        "reimbursement_amount": str(row.reimbursement_amount),
        "discount_amount": str(row.discount_amount),
        "technician_share_bps": row.technician_share_bps,
        "organization_share_bps": row.organization_share_bps,
        "technician_amount": str(row.technician_amount),
        "organization_amount": str(row.organization_amount),
        "guarantee_days": row.guarantee_days, "installment_rule": row.installment_rule,
        "status": row.status,
        "approved_by": row.approved_by,
        "frozen_at": row.frozen_at.isoformat() if row.frozen_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _active_policy(db: Session, organization_id: int, currency: str) -> TechnicianCompensationPolicy:
    row = db.query(TechnicianCompensationPolicy).filter(
        TechnicianCompensationPolicy.organization_id == organization_id,
        TechnicianCompensationPolicy.currency == currency,
        TechnicianCompensationPolicy.status == "ACTIVE",
        TechnicianCompensationPolicy.effective_from <= datetime.utcnow(),
    ).order_by(TechnicianCompensationPolicy.version.desc()).first()
    if not row:
        raise CompensationError("ACTIVE_POLICY_MISSING")
    return row


def resolve_policy_for_order(db: Session, order: ServiceOrder) -> TechnicianCompensationPolicy | None:
    """Resolve the published policy covering the OS creation cutoff."""
    if not order.organization_id or not order.created_at:
        raise CompensationError("ORDER_DATE_INVALID")
    quote = _approved_quote(db, order)
    order_currency = getattr(order, "pricing_currency", None)
    if order_currency and _currency(order_currency) != _currency(quote.currency):
        raise CompensationError("CURRENCY_MISMATCH")
    created_at = _stored_utc(order.created_at)
    rows = db.query(TechnicianCompensationPolicy).filter(
        TechnicianCompensationPolicy.organization_id == order.organization_id,
        TechnicianCompensationPolicy.currency == _currency(quote.currency),
        TechnicianCompensationPolicy.status.in_(PUBLISHED_POLICY_STATUSES),
    ).order_by(TechnicianCompensationPolicy.effective_from.desc(), TechnicianCompensationPolicy.version.desc()).all()
    for row in rows:
        effective_from = _stored_utc(row.effective_from)
        if effective_from is not None and effective_from <= created_at:
            return row
    return None


def _assigned_active_technician(db: Session, order: ServiceOrder) -> User:
    if not order.responsible_user_id:
        raise CompensationError("TECHNICIAN_NOT_ASSIGNED")
    technician = db.query(User).filter(
        User.id == order.responsible_user_id,
        User.organization_id == order.organization_id,
    ).first()
    if not technician or not technician.is_active or str(technician.status or "").upper() != "ACTIVE":
        raise CompensationError("TECHNICIAN_NOT_ACTIVE")
    try:
        membership = db.query(OrganizationMembership).filter_by(
            user_id=technician.id, organization_id=order.organization_id,
        ).first()
    except OperationalError as exc:
        raise CompensationError("MEMBERSHIP_GATE_UNAVAILABLE") from exc
    if not membership or membership.status != "ACTIVE" or not membership.is_operational:
        raise CompensationError("MEMBERSHIP_NOT_OPERATIONAL")
    return technician


def _approved_quote(db: Session, order: ServiceOrder) -> ServiceOrderQuote:
    quote = db.query(ServiceOrderQuote).filter_by(
        service_order_id=order.id, organization_id=order.organization_id, status="APPROVED",
    ).order_by(ServiceOrderQuote.version.desc()).first()
    if not quote:
        raise CompensationError("APPROVED_QUOTE_MISSING")
    if order.created_at and quote.approved_at and quote.approved_at < order.created_at:
        raise CompensationError("ORDER_DATE_INVALID")
    return quote


def _calculate(db: Session, order: ServiceOrder, policy: TechnicianCompensationPolicy,
               category_by_item_id: dict[int, str] | None = None) -> dict:
    quote = _approved_quote(db, order)
    categories = {int(key): str(value or "").strip().upper() for key, value in (category_by_item_id or {}).items()}
    totals = {category: Decimal("0.00") for category in COMPENSATION_ITEM_CATEGORIES}
    for item in quote.items:
        category = categories.get(item.id, str(getattr(item, "compensation_category", "") or "").upper())
        if category not in COMPENSATION_ITEM_CATEGORIES or category == "OTHER":
            raise CompensationError("ITEM_CLASSIFICATION_REQUIRED")
        totals[category] += _money(item.subtotal)
    labor = totals["LABOR"]
    if labor <= 0:
        raise CompensationError("LABOR_BASE_EMPTY")
    discount = _money(quote.discount_amount)
    organization_amount = (labor * Decimal(policy.organization_share_bps) / Decimal("10000")).quantize(MONEY, rounding=ROUND_HALF_UP)
    if discount > organization_amount:
        raise CompensationError("DISCOUNT_EXCEEDS_ORGANIZATION_SHARE")
    technician_amount = (labor * Decimal(policy.technician_share_bps) / Decimal("10000")).quantize(MONEY, rounding=ROUND_HALF_UP)
    if technician_amount + organization_amount != labor:
        raise CompensationError("SHARE_RECONCILIATION_FAILED")
    guarantee_days = int(order.warranty_days or policy.default_guarantee_days)
    return {
        "quote": quote, "labor": labor, "material": totals["MATERIAL"],
        "tax": _money(quote.tax_amount) + totals["TAX"], "reimbursement": totals["REIMBURSEMENT"],
        "discount": discount, "technician_amount": technician_amount,
        "organization_amount": organization_amount, "guarantee_days": guarantee_days,
    }


def create_policy(db: Session, *, actor: User, organization_id: int, currency: str,
                  effective_from: datetime | None = None, idempotency_key: str,
                  effective_from_local: str | None = None,
                  effective_timezone: str | None = None,
                  technician_share_bps: int = 7500, organization_share_bps: int = 2500,
                  default_guarantee_days: int = 7) -> TechnicianCompensationPolicy:
    _require_feature(db, organization_id)
    _require_admin(actor, organization_id)
    currency = _currency(currency)
    effective_from = normalize_effective_from(
        effective_from=effective_from,
        effective_from_local=effective_from_local,
        effective_timezone=effective_timezone,
    )
    if effective_from < datetime.now(timezone.utc).replace(tzinfo=None):
        raise CompensationError("POLICY_EFFECTIVE_FROM_PAST")
    if technician_share_bps + organization_share_bps != 10000 or min(technician_share_bps, organization_share_bps) < 0:
        raise CompensationError("SHARE_TOTAL_INVALID")
    if default_guarantee_days < 0 or not idempotency_key.strip():
        raise CompensationError("POLICY_INPUT_INVALID")
    hashed = _hash_key(idempotency_key)
    existing = db.query(TechnicianCompensationPolicy).filter_by(idempotency_key_hash=hashed).first()
    if existing:
        return existing
    version = (db.query(TechnicianCompensationPolicy).filter_by(organization_id=organization_id, currency=currency).count() or 0) + 1
    row = TechnicianCompensationPolicy(
        organization_id=organization_id, version=version, status="DRAFT", currency=currency,
        technician_share_bps=technician_share_bps, organization_share_bps=organization_share_bps,
        default_guarantee_days=default_guarantee_days, installment_rule=COMPENSATION_INSTALLMENT_RULE,
        processor_fee_responsibility="ORGANIZATION", discount_rule="ORGANIZATION_SHARE_ONLY",
        effective_from=effective_from, created_by=actor.id, idempotency_key_hash=hashed,
    )
    db.add(row)
    db.flush()
    _event(db, organization_id=organization_id, policy_id=row.id, event_type="POLICY_CREATED",
           reason_code="POLICY_CREATED", idempotency_key=f"policy-created:{idempotency_key}", actor_user_id=actor.id)
    return row


def activate_policy(db: Session, *, actor: User, policy_id: int) -> TechnicianCompensationPolicy:
    row = db.query(TechnicianCompensationPolicy).filter_by(id=policy_id).with_for_update().first()
    if not row:
        raise CompensationError("POLICY_NOT_FOUND")
    _require_feature(db, row.organization_id)
    _require_admin(actor, row.organization_id)
    if row.status == "ACTIVE":
        return row
    if row.status != "DRAFT":
        raise CompensationError("POLICY_NOT_DRAFT")
    now = datetime.now(timezone.utc)
    effective_from = _stored_utc(row.effective_from)
    published = db.query(TechnicianCompensationPolicy).filter(
        TechnicianCompensationPolicy.organization_id == row.organization_id,
        TechnicianCompensationPolicy.currency == row.currency,
        TechnicianCompensationPolicy.status.in_(PUBLISHED_POLICY_STATUSES),
        TechnicianCompensationPolicy.id != row.id,
    ).order_by(TechnicianCompensationPolicy.effective_from.desc()).all()
    if effective_from is None:
        raise CompensationError("EFFECTIVE_FROM_TIMEZONE_REQUIRED")
    if not published and effective_from < now.replace(tzinfo=None):
        raise CompensationError("POLICY_EFFECTIVE_FROM_PAST")
    if published and effective_from <= _stored_utc(published[0].effective_from):
        raise CompensationError("POLICY_EFFECTIVE_FROM_NOT_AFTER_PREVIOUS")
    active = db.query(TechnicianCompensationPolicy).filter(
        TechnicianCompensationPolicy.organization_id == row.organization_id,
        TechnicianCompensationPolicy.currency == row.currency,
        TechnicianCompensationPolicy.status == "ACTIVE",
        TechnicianCompensationPolicy.id != row.id,
    ).with_for_update().all()
    for previous in active:
        previous.status = "RETIRED"
    row.status = "ACTIVE"
    row.activated_by = actor.id
    row.updated_at = datetime.utcnow()
    db.flush()
    _event(db, organization_id=row.organization_id, policy_id=row.id, event_type="POLICY_ACTIVATED",
           reason_code="POLICY_ACTIVATED", idempotency_key=f"policy-activated:{row.id}:{row.version}", actor_user_id=actor.id)
    return row


def preview_order(db: Session, *, actor: User, order_id: int, category_by_item_id: dict[int, str] | None = None) -> dict:
    order = db.query(ServiceOrder).filter_by(id=order_id, organization_id=actor.organization_id).first()
    if not order:
        raise CompensationError("ORDER_NOT_FOUND")
    _require_feature(db, order.organization_id)
    _require_admin(actor, order.organization_id)
    _assigned_active_technician(db, order)
    quote = _approved_quote(db, order)
    policy = resolve_policy_for_order(db, order)
    if not policy:
        raise CompensationError("PRE_POLICY_LEGACY")
    return _calculation_payload(_calculate(db, order, policy, category_by_item_id), policy, order)


def quote_items_for_order(db: Session, *, actor: User, order_id: int) -> list[dict]:
    order = db.query(ServiceOrder).filter_by(id=order_id, organization_id=actor.organization_id).first()
    if not order:
        raise CompensationError("ORDER_NOT_FOUND")
    _require_feature(db, order.organization_id)
    _require_admin(actor, order.organization_id)
    quote = _approved_quote(db, order)
    return [{
        "id": item.id,
        "description": item.description,
        "quantity": str(item.quantity),
        "unit": item.unit,
        "subtotal": str(item.subtotal),
        "compensation_category": getattr(item, "compensation_category", None),
    } for item in quote.items]


def _calculation_payload(calculation: dict, policy: TechnicianCompensationPolicy, order: ServiceOrder) -> dict:
    return {
        "service_order_id": order.id, "technician_user_id": order.responsible_user_id,
        "policy_version": policy.version, "currency": policy.currency,
        "labor_base_amount": str(calculation["labor"]), "material_amount": str(calculation["material"]),
        "tax_amount": str(calculation["tax"]), "reimbursement_amount": str(calculation["reimbursement"]),
        "discount_amount": str(calculation["discount"]),
        "technician_share_bps": policy.technician_share_bps,
        "organization_share_bps": policy.organization_share_bps,
        "technician_amount": str(calculation["technician_amount"]),
        "organization_amount": str(calculation["organization_amount"]),
        "guarantee_days": calculation["guarantee_days"],
        "installment_rule": policy.installment_rule,
    }


def propose_snapshot(db: Session, *, actor: User, order_id: int, idempotency_key: str,
                     category_by_item_id: dict[int, str] | None = None) -> ServiceOrderCompensationSnapshot:
    order = db.query(ServiceOrder).filter_by(id=order_id, organization_id=actor.organization_id).with_for_update().first()
    if not order:
        raise CompensationError("ORDER_NOT_FOUND")
    _require_feature(db, order.organization_id)
    _require_admin(actor, order.organization_id)
    technician = _assigned_active_technician(db, order)
    if not idempotency_key.strip():
        raise CompensationError("IDEMPOTENCY_KEY_REQUIRED")
    hashed = _hash_key(idempotency_key)
    existing = db.query(ServiceOrderCompensationSnapshot).filter_by(idempotency_key_hash=hashed).first()
    if existing:
        return existing
    quote = _approved_quote(db, order)
    policy = resolve_policy_for_order(db, order)
    if not policy:
        raise CompensationError("PRE_POLICY_LEGACY")
    if order.created_at and order.created_at < policy.effective_from:
        raise CompensationError("ORDER_BEFORE_POLICY_EFFECTIVE_DATE")
    calculation = _calculate(db, order, policy, category_by_item_id)
    prior = db.query(ServiceOrderCompensationSnapshot).filter(
        ServiceOrderCompensationSnapshot.organization_id == order.organization_id,
        ServiceOrderCompensationSnapshot.service_order_id == order.id,
        ServiceOrderCompensationSnapshot.status != "VOID",
    ).with_for_update().all()
    for row in prior:
        row.status = "VOID"
        row.updated_at = datetime.utcnow()
        _event(db, organization_id=row.organization_id, snapshot_id=row.id, event_type="SNAPSHOT_VOIDED",
               reason_code="NEW_SNAPSHOT_VERSION", idempotency_key=f"snapshot-voided:{row.id}:{idempotency_key}", actor_user_id=actor.id)
    row = ServiceOrderCompensationSnapshot(
        organization_id=order.organization_id, service_order_id=order.id,
        technician_user_id=technician.id, policy_id=policy.id, policy_version=policy.version,
        currency=policy.currency, labor_base_amount=calculation["labor"],
        material_amount=calculation["material"], tax_amount=calculation["tax"],
        reimbursement_amount=calculation["reimbursement"], discount_amount=calculation["discount"],
        technician_share_bps=policy.technician_share_bps, organization_share_bps=policy.organization_share_bps,
        technician_amount=calculation["technician_amount"], organization_amount=calculation["organization_amount"],
        guarantee_days=calculation["guarantee_days"], installment_rule=policy.installment_rule,
        status="PROPOSED", idempotency_key_hash=hashed,
    )
    db.add(row)
    db.flush()
    _event(db, organization_id=row.organization_id, snapshot_id=row.id, policy_id=policy.id,
           event_type="SNAPSHOT_PROPOSED", reason_code="SNAPSHOT_PROPOSED",
           idempotency_key=f"snapshot-proposed:{idempotency_key}", actor_user_id=actor.id)
    return row


def freeze_snapshot(db: Session, *, actor: User, snapshot_id: int) -> ServiceOrderCompensationSnapshot:
    row = db.query(ServiceOrderCompensationSnapshot).filter_by(id=snapshot_id).with_for_update().first()
    if not row:
        raise CompensationError("SNAPSHOT_NOT_FOUND")
    _require_feature(db, row.organization_id)
    _require_admin(actor, row.organization_id)
    if actor.id == row.technician_user_id:
        raise CompensationError("APPROVER_CANNOT_BE_TECHNICIAN")
    if row.status == "FROZEN":
        return row
    if row.status != "PROPOSED":
        raise CompensationError("SNAPSHOT_NOT_PROPOSED")
    row.status = "FROZEN"
    row.approved_by = actor.id
    row.frozen_at = datetime.utcnow()
    row.updated_at = datetime.utcnow()
    db.flush()
    _event(db, organization_id=row.organization_id, snapshot_id=row.id, policy_id=row.policy_id,
           event_type="SNAPSHOT_FROZEN", reason_code="TECHNICIAN_COMPENSATION_FROZEN",
           idempotency_key=f"snapshot-frozen:{row.id}", actor_user_id=actor.id)
    return row


def get_snapshot(db: Session, *, actor: User, snapshot_id: int) -> ServiceOrderCompensationSnapshot:
    row = db.query(ServiceOrderCompensationSnapshot).filter_by(id=snapshot_id).first()
    if not row or row.organization_id != actor.organization_id:
        raise CompensationError("SNAPSHOT_NOT_FOUND")
    _require_feature(db, row.organization_id)
    if actor.id != row.technician_user_id and actor.role not in {"ROOT", "GERENTE"}:
        raise CompensationError("SNAPSHOT_NOT_FOUND")
    return row


def technician_snapshot_payload(row: ServiceOrderCompensationSnapshot) -> dict:
    """Expose only the frozen technician-facing compensation terms."""
    return {
        "id": row.id,
        "service_order_id": row.service_order_id,
        "policy_version": row.policy_version,
        "currency": row.currency,
        "labor_base_amount": str(row.labor_base_amount),
        "technician_amount": str(row.technician_amount),
        "guarantee_days": row.guarantee_days,
        "installment_rule": row.installment_rule,
        "status": row.status,
        "frozen_at": row.frozen_at.isoformat() if row.frozen_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def list_technician_snapshots(db: Session, *, actor: User) -> list[ServiceOrderCompensationSnapshot]:
    _require_feature(db, actor.organization_id)
    if actor.role not in {"BROKER", "TECNICO", "TÉCNICO"}:
        raise CompensationError("TECHNICIAN_REQUIRED")
    if not actor.organization_id or not actor.is_active or str(actor.status or "").upper() != "ACTIVE":
        raise CompensationError("TECHNICIAN_NOT_ACTIVE")
    return db.query(ServiceOrderCompensationSnapshot).filter(
        ServiceOrderCompensationSnapshot.organization_id == actor.organization_id,
        ServiceOrderCompensationSnapshot.technician_user_id == actor.id,
        ServiceOrderCompensationSnapshot.status == "FROZEN",
    ).order_by(ServiceOrderCompensationSnapshot.frozen_at.desc(), ServiceOrderCompensationSnapshot.id.desc()).all()


def ensure_technician_acceptance_allowed(db: Session, *, order: ServiceOrder, technician_user_id: int) -> None:
    """When enabled, acceptance requires a current immutable frozen snapshot."""
    if not compensation_policy_enabled_for_organization(db, order.organization_id):
        return
    remunerated_quote = db.query(ServiceOrderQuote.id).filter_by(
        service_order_id=order.id, organization_id=order.organization_id, status="APPROVED",
    ).first()
    if not remunerated_quote:
        return
    policy = resolve_policy_for_order(db, order)
    if not policy:
        return
    snapshot = db.query(ServiceOrderCompensationSnapshot).filter(
        ServiceOrderCompensationSnapshot.organization_id == order.organization_id,
        ServiceOrderCompensationSnapshot.service_order_id == order.id,
        ServiceOrderCompensationSnapshot.technician_user_id == technician_user_id,
        ServiceOrderCompensationSnapshot.status == "FROZEN",
        ServiceOrderCompensationSnapshot.policy_id == policy.id,
        ServiceOrderCompensationSnapshot.policy_version == policy.version,
        ServiceOrderCompensationSnapshot.currency == policy.currency,
    ).first()
    if not snapshot:
        raise CompensationError("COMPENSATION_SNAPSHOT_REQUIRED")
