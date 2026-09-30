from datetime import datetime

from sqlalchemy.orm import Session

from app.models.organization import Organization
from app.models.organization_membership import OrganizationMembership
from app.models.payment import Payment, PlatformLedgerEntry
from app.models.service_order import ServiceOrder
from app.models.service_order_completion import ServiceOrderTechnicalCompletion
from app.models.service_order_diagnosis import ServiceOrderDiagnosis
from app.models.service_order_ledger_entry import ServiceOrderLedgerEntry
from app.models.service_order_quote import ServiceOrderQuote
from app.models.service_order_warranty_claim import ServiceOrderWarrantyClaim
from app.models.technician_transfer import TechnicianTransferRequest
from app.models.user import User


TERMINAL_ORDER_STATUSES = {"COMPLETED", "CANCELLED", "CANCELED", "REJECTED", "FINALIZADA", "CONCLUIDA"}
ACTIVE_MEMBERSHIP_STATUSES = {"ACTIVE"}
ROLE_TO_MEMBERSHIP = {
    "GERENTE": ("ADMIN", "ADMIN"),
    "BROKER": ("TECHNICIAN", "TECHNICIAN"),
    "TECNICO": ("TECHNICIAN", "TECHNICIAN"),
}


def membership_role_for_user(user: User) -> tuple[str, str]:
    role = (user.role or "").upper()
    if role == "ROOT":
        return "OWNER", "OWNER"
    return ROLE_TO_MEMBERSHIP.get(role, ("TECHNICIAN", "TECHNICIAN"))


def explicit_membership_role(user: User, owner_user_ids: set[int], admin_user_ids: set[int]) -> tuple[str, str]:
    """Resolve a role without ever promoting an unclassified ROOT."""
    user_role = (user.role or "").upper()
    if user_role == "ROOT":
        if user.id in owner_user_ids:
            return "OWNER", "OWNER"
        if user.id in admin_user_ids:
            return "ADMIN", "ADMIN"
        raise ValueError(f"ROOT user {user.id} requires explicit owner/admin classification")
    if user.id in owner_user_ids:
        raise ValueError(f"owner classification {user.id} requires current ROOT role")
    if user.id in admin_user_ids and user_role not in {"GERENTE", "ROOT"}:
        raise ValueError(f"admin classification {user.id} requires ROOT or GERENTE role")
    return ROLE_TO_MEMBERSHIP.get(user_role, ("TECHNICIAN", "TECHNICIAN"))


def _membership_status(user: User, organization: Organization) -> tuple[str, bool]:
    active = (
        bool(user.is_active)
        and (user.status or "ACTIVE").upper() == "ACTIVE"
        and (organization.status or "ACTIVE").upper() == "ACTIVE"
    )
    return ("ACTIVE", True) if active else ("SUSPENDED", False)


def plan_membership_backfill(
    db: Session,
    *,
    owner_user_ids: set[int] | None = None,
    admin_user_ids: set[int] | None = None,
) -> list[dict]:
    """Build an explicit, idempotent plan without changing users or roles."""
    owner_user_ids = set(owner_user_ids or set())
    admin_user_ids = set(admin_user_ids or set())
    if owner_user_ids & admin_user_ids:
        raise ValueError("a user cannot be classified as both OWNER and ADMIN")

    users = db.query(User).filter(User.organization_id.is_not(None)).order_by(User.id.asc()).with_for_update().all()
    known_ids = {user.id for user in users}
    unknown_classified = (owner_user_ids | admin_user_ids) - known_ids
    if unknown_classified:
        raise ValueError(f"classified user does not exist: {sorted(unknown_classified)}")

    plan: list[dict] = []
    for user in users:
        organization = db.query(Organization).filter(Organization.id == user.organization_id).with_for_update().first()
        if organization is None:
            raise ValueError(f"organization {user.organization_id} for user {user.id} does not exist")
        membership_type, role = explicit_membership_role(user, owner_user_ids, admin_user_ids)
        status, is_operational = _membership_status(user, organization)
        existing = db.query(OrganizationMembership).filter_by(
            user_id=user.id, organization_id=user.organization_id,
        ).with_for_update().first()
        desired = {
            "user_id": user.id,
            "organization_id": organization.id,
            "membership_type": membership_type,
            "role": role,
            "status": status,
            "supervisor_user_id": user.manager_id,
            "is_primary": True,
            "is_operational": is_operational,
        }
        if existing is None:
            desired["action"] = "CREATE"
        elif all(getattr(existing, key) == value for key, value in desired.items() if key != "action"):
            desired["action"] = "UNCHANGED"
        else:
            desired["action"] = "UPDATE"
            desired["membership_id"] = existing.id
        plan.append(desired)
    return plan


def apply_membership_backfill(db: Session, plan: list[dict]) -> dict[str, int]:
    """Apply a previously locked plan; caller owns the transaction and commit."""
    counts = {"created": 0, "updated": 0, "unchanged": 0}
    now = datetime.utcnow()
    for item in plan:
        if item["action"] == "UNCHANGED":
            counts["unchanged"] += 1
            continue
        membership = db.query(OrganizationMembership).filter_by(
            user_id=item["user_id"], organization_id=item["organization_id"],
        ).with_for_update().first()
        if membership is None:
            membership = OrganizationMembership(
                user_id=item["user_id"], organization_id=item["organization_id"],
                joined_at=now, approved_at=now if item["status"] == "ACTIVE" else None,
                created_at=now, updated_at=now,
            )
            db.add(membership)
            counts["created"] += 1
        else:
            counts["updated"] += 1
            membership.updated_at = now
        for key in ("membership_type", "role", "status", "supervisor_user_id", "is_primary", "is_operational"):
            setattr(membership, key, item[key])
        if membership.status == "SUSPENDED":
            membership.approved_at = None
    db.flush()
    return counts


def get_active_memberships(db: Session, user_id: int) -> list[OrganizationMembership]:
    return db.query(OrganizationMembership).join(
        Organization, Organization.id == OrganizationMembership.organization_id,
    ).filter(
        OrganizationMembership.user_id == user_id,
        OrganizationMembership.status.in_(ACTIVE_MEMBERSHIP_STATUSES),
        Organization.status == "ACTIVE",
    ).order_by(OrganizationMembership.is_primary.desc(), OrganizationMembership.id.asc()).all()


def get_primary_membership(db: Session, user_id: int) -> OrganizationMembership | None:
    return db.query(OrganizationMembership).join(
        Organization, Organization.id == OrganizationMembership.organization_id,
    ).filter(
        OrganizationMembership.user_id == user_id,
        OrganizationMembership.is_primary.is_(True),
        OrganizationMembership.status == "ACTIVE",
        Organization.status == "ACTIVE",
    ).order_by(OrganizationMembership.id.asc()).first()


def user_has_membership(db: Session, user_id: int, organization_id: int, *, active_only: bool = True) -> bool:
    query = db.query(OrganizationMembership).filter_by(user_id=user_id, organization_id=organization_id)
    if active_only:
        query = query.join(Organization, Organization.id == OrganizationMembership.organization_id).filter(
            OrganizationMembership.status == "ACTIVE",
            Organization.status == "ACTIVE",
        )
    return query.first() is not None


def backfill_memberships(db: Session) -> int:
    """Compatibility helper for tests; production callers must use the CLI plan."""
    plan = plan_membership_backfill(db)
    counts = apply_membership_backfill(db, plan)
    return counts["created"]


def transfer_blockers(db: Session, user_id: int, from_organization_id: int) -> list[dict[str, str | int]]:
    blockers: list[dict[str, str | int]] = []
    orders = db.query(ServiceOrder).filter(
        ServiceOrder.organization_id == from_organization_id,
        ServiceOrder.responsible_user_id == user_id,
        ~ServiceOrder.status.in_(TERMINAL_ORDER_STATUSES),
    ).all()
    for order in orders:
        blockers.append({"code": "OPEN_SERVICE_ORDER", "entity_id": order.id, "detail": f"OS {order.id} ainda está aberta"})

    pending_diagnoses = db.query(ServiceOrderDiagnosis).join(
        ServiceOrder, ServiceOrder.id == ServiceOrderDiagnosis.service_order_id,
    ).filter(
        ServiceOrder.organization_id == from_organization_id,
        ServiceOrder.responsible_user_id == user_id,
        ~ServiceOrder.status.in_(TERMINAL_ORDER_STATUSES),
    ).all()
    blockers.extend({"code": "PENDING_DIAGNOSIS", "entity_id": row.id, "detail": "Diagnóstico pendente"} for row in pending_diagnoses)

    pending_quotes = db.query(ServiceOrderQuote).join(
        ServiceOrder, ServiceOrder.id == ServiceOrderQuote.service_order_id,
    ).filter(
        ServiceOrder.organization_id == from_organization_id,
        ServiceOrder.responsible_user_id == user_id,
        ~ServiceOrderQuote.status.in_({"REJECTED", "CANCELLED", "APPROVED"}),
    ).all()
    blockers.extend({"code": "PENDING_QUOTE", "entity_id": row.id, "detail": "Cotação pendente"} for row in pending_quotes)

    claims = db.query(ServiceOrderWarrantyClaim).join(
        ServiceOrder, ServiceOrder.id == ServiceOrderWarrantyClaim.service_order_id,
    ).filter(
        ServiceOrder.organization_id == from_organization_id,
        ServiceOrder.responsible_user_id == user_id,
        ~ServiceOrderWarrantyClaim.status.in_({"RESOLVED", "REJECTED", "CLOSED"}),
    ).all()
    blockers.extend({"code": "OPEN_WARRANTY_CLAIM", "entity_id": row.id, "detail": "Claim de garantia aberto"} for row in claims)

    payments = db.query(Payment).filter(
        Payment.organization_id == from_organization_id,
        Payment.technician_id == user_id,
        ~Payment.status.in_({"PAID", "SUCCEEDED", "CANCELLED", "REFUNDED", "FAILED"}),
    ).all()
    blockers.extend({"code": "PENDING_PAYMENT", "entity_id": row.id, "detail": "Pagamento pendente vinculado ao técnico"} for row in payments)

    ledger_entries = db.query(ServiceOrderLedgerEntry).filter(
        ServiceOrderLedgerEntry.organization_id == from_organization_id,
        ServiceOrderLedgerEntry.actor_id == user_id,
        ~ServiceOrderLedgerEntry.status.in_({"SETTLED", "CLOSED", "CANCELLED"}),
    ).all()
    blockers.extend({"code": "OPEN_LEDGER_ENTRY", "entity_id": row.id, "detail": "Lançamento financeiro pendente"} for row in ledger_entries)

    platform_entries = db.query(PlatformLedgerEntry).filter(
        PlatformLedgerEntry.organization_id == from_organization_id,
        PlatformLedgerEntry.technician_id == user_id,
        ~PlatformLedgerEntry.status.in_({"SETTLED", "CLOSED", "CANCELLED"}),
    ).all()
    blockers.extend({"code": "OPEN_PLATFORM_LEDGER", "entity_id": row.id, "detail": "Ledger da plataforma pendente"} for row in platform_entries)

    pending_evidence = db.query(ServiceOrderTechnicalCompletion).join(
        ServiceOrder, ServiceOrder.id == ServiceOrderTechnicalCompletion.service_order_id,
    ).filter(
        ServiceOrder.organization_id == from_organization_id,
        ServiceOrder.responsible_user_id == user_id,
        ServiceOrderTechnicalCompletion.status.in_({"REPORTED", "PENDING", "UNDER_REVIEW"}),
    ).all()
    blockers.extend({"code": "PENDING_EVIDENCE_REVIEW", "entity_id": row.id, "detail": "Evidência aguardando revisão"} for row in pending_evidence)
    return blockers


def approve_exclusive_transfer(db: Session, request: TechnicianTransferRequest, reviewer: User) -> tuple[OrganizationMembership, list[dict]]:
    blockers = transfer_blockers(db, request.user_id, request.from_organization_id)
    if blockers:
        request.status = "TRANSFER_PENDING_BLOCKED"
        return None, blockers

    current = db.query(OrganizationMembership).filter_by(
        user_id=request.user_id, organization_id=request.from_organization_id, status="ACTIVE",
    ).first()
    if not current:
        raise ValueError("Vínculo de origem ativo não encontrado")
    target_organization = db.query(Organization).filter(Organization.id == request.to_organization_id, Organization.status == "ACTIVE").first()
    if not target_organization:
        raise ValueError("Vínculo de destino inválido")
    target = db.query(OrganizationMembership).filter_by(
        user_id=request.user_id, organization_id=request.to_organization_id,
    ).first()
    now = datetime.utcnow()
    current.status = "EXITED"
    current.is_operational = False
    current.is_primary = False
    current.exited_at = now
    current.exit_reason = "EXCLUSIVE_TRANSFER"
    if not target:
        user = db.query(User).filter(User.id == request.user_id).first()
        membership_type, role = membership_role_for_user(user)
        target = OrganizationMembership(
            user_id=request.user_id, organization_id=request.to_organization_id,
            membership_type=membership_type, role=role, status="ACTIVE",
            is_primary=True, is_operational=True, joined_at=now,
            approved_at=now, approved_by_user_id=reviewer.id,
            terms_version=request.terms_version,
            terms_accepted_at=request.terms_accepted_at,
        )
        db.add(target)
    else:
        target.status = "ACTIVE"
        target.is_primary = True
        target.is_operational = True
        target.approved_at = now
        target.approved_by_user_id = reviewer.id
        target.terms_version = request.terms_version
        target.terms_accepted_at = request.terms_accepted_at
    request.status = "APPROVED"
    request.reviewed_at = now
    request.reviewed_by_user_id = reviewer.id
    db.flush()
    return target, []
