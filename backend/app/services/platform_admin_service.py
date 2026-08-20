from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.commercial_subscription import CommercialSubscription
from app.models.commercial_upgrade_intent import CommercialUpgradeIntent
from app.models.lead import Lead
from app.models.organization import Organization
from app.models.service_order import ServiceOrder
from app.models.user import User
from app.services.entitlement_service import normalize_plan


def _subscription(db: Session, organization_id: int):
    return db.query(CommercialSubscription).filter(CommercialSubscription.organization_id == organization_id).first()


def _organization_plan(db: Session, organization: Organization) -> tuple[str, str | None]:
    subscription = _subscription(db, organization.id)
    return normalize_plan(subscription.plan if subscription else organization.plan), subscription.status if subscription else None


def _mask_phone(phone: str | None) -> str | None:
    value = (phone or "").strip()
    if len(value) <= 4:
        return "***" if value else None
    return f"{'*' * max(len(value) - 4, 4)}{value[-4:]}"


def _supervisor(db: Session, manager_id: int | None):
    return db.query(User).filter(User.id == manager_id).first() if manager_id else None


def _user_row(db: Session, user: User, organization: Organization | None = None) -> dict:
    organization = organization or db.query(Organization).filter(Organization.id == user.organization_id).first()
    supervisor = _supervisor(db, user.manager_id)
    plan, _ = _organization_plan(db, organization) if organization else ("FREE", None)
    return {
        "id": user.id,
        "full_name": user.full_name or user.username,
        "username": user.username,
        "email": user.email or user.email_pessoal,
        "telefone": _mask_phone(user.telefone),
        "role": user.role,
        "status": user.status,
        "is_active": bool(user.is_active),
        "email_verified": bool(user.email_verified),
        "organization_id": user.organization_id,
        "organization_name": organization.name if organization else None,
        "supervisor": supervisor.full_name or supervisor.username if supervisor else None,
        "supervisor_id": supervisor.id if supervisor else None,
        "onboarding_source": user.onboarding_source,
        "created_at": user.registered_at.isoformat() if user.registered_at else None,
        "last_seen_at": user.last_seen_at.isoformat() if user.last_seen_at else None,
        "plan": plan,
    }


def _organization_row(db: Session, organization: Organization) -> dict:
    plan, subscription_status = _organization_plan(db, organization)
    users = db.query(User).filter(User.organization_id == organization.id).all()
    active_intent = (
        db.query(CommercialUpgradeIntent)
        .filter(
            CommercialUpgradeIntent.organization_id == organization.id,
            CommercialUpgradeIntent.status.in_(["CHECKOUT_OPENED", "PAYMENT_PENDING", "PAYMENT_CONFIRMED", "PAID"]),
        )
        .order_by(CommercialUpgradeIntent.created_at.desc(), CommercialUpgradeIntent.id.desc())
        .first()
    )
    owner = next((item for item in users if item.role == "ROOT"), None) or next((item for item in users if item.role == "GERENTE"), None)
    return {
        "id": organization.id,
        "organization_id": organization.id,
        "name": organization.name,
        "slug": organization.slug,
        "status": organization.status,
        "plan": plan,
        "subscription_status": subscription_status,
        "owner": owner.full_name or owner.username if owner else None,
        "owner_user_id": owner.id if owner else None,
        "users_count": len(users),
        "technicians_count": sum(1 for item in users if item.role == "BROKER"),
        "clients_count": db.query(func.count(Lead.id)).filter(Lead.organization_id == organization.id).scalar() or 0,
        "service_orders_count": db.query(func.count(ServiceOrder.id)).filter(ServiceOrder.organization_id == organization.id).scalar() or 0,
        "created_at": organization.created_at.isoformat() if organization.created_at else None,
        "updated_at": organization.updated_at.isoformat() if organization.updated_at else None,
        "active_intent": {
            "id": active_intent.id,
            "plan": active_intent.requested_plan,
            "status": "PAYMENT_CONFIRMED" if active_intent.status == "PAID" else active_intent.status,
            "reference_price_mxn": float(active_intent.reference_price_mxn),
            "confirmation_source": active_intent.confirmation_source,
        } if active_intent else None,
    }


def list_platform_organizations(db: Session, *, search: str | None = None, plan: str | None = None, status: str | None = None) -> list[dict]:
    query = db.query(Organization).order_by(Organization.created_at.desc(), Organization.id.desc())
    term = (search or "").strip()
    if term:
        query = query.filter(or_(Organization.name.ilike(f"%{term}%"), Organization.slug.ilike(f"%{term}%"), Organization.id == int(term) if term.isdigit() else False))
    if status and status.upper() != "ALL":
        query = query.filter(Organization.status == status.upper())
    rows = [_organization_row(db, item) for item in query.limit(500).all()]
    if plan and plan.upper() != "ALL":
        rows = [item for item in rows if item["plan"] == normalize_plan(plan)]
    return rows


def list_platform_users(db: Session, *, search: str | None = None, role: str | None = None, status: str | None = None, organization_id: int | None = None) -> list[dict]:
    query = db.query(User, Organization).outerjoin(Organization, Organization.id == User.organization_id).order_by(User.registered_at.desc(), User.id.desc())
    term = (search or "").strip()
    if term:
        pattern = f"%{term}%"
        filters = [User.full_name.ilike(pattern), User.username.ilike(pattern), User.email.ilike(pattern), User.email_pessoal.ilike(pattern), User.telefone.ilike(pattern), Organization.name.ilike(pattern), Organization.slug.ilike(pattern)]
        if term.isdigit():
            filters.extend([User.id == int(term), User.organization_id == int(term)])
        query = query.filter(or_(*filters))
    if role and role.upper() != "ALL":
        query = query.filter(User.role == role.upper())
    if status and status.upper() != "ALL":
        query = query.filter(User.status == status.upper())
    if organization_id is not None:
        query = query.filter(User.organization_id == organization_id)
    return [_user_row(db, user, organization) for user, organization in query.limit(500).all()]


def get_platform_organization(db: Session, organization_id: int) -> dict | None:
    organization = db.query(Organization).filter(Organization.id == organization_id).first()
    if not organization:
        return None
    row = _organization_row(db, organization)
    row["users"] = list_platform_users(db, organization_id=organization.id)
    return row


def get_platform_user(db: Session, user_id: int) -> dict | None:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return None
    row = _user_row(db, user)
    row["active_intent"] = next((item for item in list_platform_organizations(db) if item["id"] == user.organization_id), {}).get("active_intent")
    return row


def platform_directory_metrics(db: Session) -> dict:
    organizations = list_platform_organizations(db)
    users = db.query(User).all()
    return {
        "organizations_total": len(organizations),
        "organizations_free": sum(1 for item in organizations if item["plan"] == "FREE"),
        "organizations_pro": sum(1 for item in organizations if item["plan"] == "PRO"),
        "organizations_business": sum(1 for item in organizations if item["plan"] == "BUSINESS"),
        "users_total": len(users),
        "technicians_total": sum(1 for item in users if item.role == "BROKER"),
        "supervisors_total": sum(1 for item in users if item.role == "GERENTE"),
        "users_active": sum(1 for item in users if item.is_active and item.status == "ACTIVE"),
        "users_pending": sum(1 for item in users if item.status in {"PENDING", "PENDING_EMAIL", "PENDING_APPROVAL", "PENDING_ADMIN"}),
    }
