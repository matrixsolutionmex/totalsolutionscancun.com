from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import func, inspect
from sqlalchemy.orm import Session

from app.core.auth_security import audit_auth_event
from app.core.organization import get_platform_primary_organization
from app.models.commercial_subscription import CommercialSubscription
from app.models.commercial_upgrade_intent import CommercialUpgradeIntent
from app.models.lead import Lead
from app.models.organization import Organization
from app.models.service_order import ServiceOrder
from app.models.user import User
from app.models.user_commercial_profile import UserCommercialProfile
from app.services.entitlement_service import normalize_plan


def legacy_workspace_candidates(db: Session) -> list[dict]:
    primary = get_platform_primary_organization(db)
    rows = []
    for organization in db.query(Organization).filter(Organization.id != primary.id).all():
        users = db.query(User).filter(User.organization_id == organization.id).all()
        subscription = db.query(CommercialSubscription).filter(CommercialSubscription.organization_id == organization.id).first()
        leads = db.query(func.count(Lead.id)).filter(Lead.organization_id == organization.id).scalar() or 0
        orders = db.query(func.count(ServiceOrder.id)).filter(ServiceOrder.organization_id == organization.id).scalar() or 0
        active_intent = db.query(CommercialUpgradeIntent).filter(
            CommercialUpgradeIntent.organization_id == organization.id,
            CommercialUpgradeIntent.status.in_(["CHECKOUT_OPENED", "PAYMENT_PENDING", "PAYMENT_CONFIRMED", "PAID"]),
        ).first()
        user = users[0] if len(users) == 1 else None
        profile = None
        if user and inspect(db.get_bind()).has_table(UserCommercialProfile.__tablename__):
            profile = db.query(UserCommercialProfile).filter(UserCommercialProfile.user_id == user.id, UserCommercialProfile.status == "ACTIVE").first()
        individual_plan = normalize_plan(profile.plan if profile else getattr(user, "plan", "FREE")) if user else "FREE"
        # Migration moves the workspace only. Promotion to GERENTE remains a separate ROOT action.
        eligible_role = bool(user and user.role in {"BROKER", "GERENTE"})
        eligible_user = bool(user and user.is_active and user.status == "ACTIVE" and eligible_role and individual_plan in {"PRO", "BUSINESS"})
        if user and leads == 0 and orders == 0 and eligible_user and not active_intent:
            rows.append({
                "organization_id": organization.id,
                "organization_name": organization.name,
                "organization_status": organization.status,
                "user_id": user.id,
                "user": user.full_name or user.username,
                "email": user.email,
                "role": user.role,
                "status": user.status,
                "individual_plan": individual_plan,
                "clients_count": int(leads),
                "service_orders_count": int(orders),
                "target_organization_id": primary.id,
                "target_organization_name": primary.name,
            })
    return rows


def migrate_legacy_user_to_primary(db: Session, *, user_id: int, actor: User, reason: str | None = None) -> User:
    if actor.role != "ROOT":
        raise HTTPException(status_code=403, detail="Somente ROOT pode migrar usuários antigos")
    primary = get_platform_primary_organization(db)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    candidate = next((row for row in legacy_workspace_candidates(db) if row["user_id"] == user_id), None)
    if not candidate:
        raise HTTPException(status_code=409, detail="Usuário não atende aos critérios de migração segura")
    previous_organization_id = user.organization_id
    old_organization = db.query(Organization).filter(Organization.id == previous_organization_id).first()
    user.organization_id = primary.id
    user.manager_id = None
    user.onboarding_source = "MIGRATED_LEGACY"
    if old_organization and not db.query(User).filter(User.organization_id == old_organization.id, User.id != user.id).first():
        old_organization.status = "ORPHANED_ONBOARDING"
    audit_auth_event(db, request=None, event_type="LEGACY_USER_MIGRATED_TO_PRIMARY_ORGANIZATION", outcome="SUCCESS", user=user, actor=actor, detail={"user_id": user.id, "old_organization_id": previous_organization_id, "new_organization_id": primary.id, "timestamp": datetime.utcnow().isoformat(), "reason": (reason or "legacy_single_user_workspace")[:300]})
    db.commit()
    db.refresh(user)
    return user
