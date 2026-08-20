from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.auth_security import audit_auth_event
from app.core.organization import get_platform_primary_organization
from app.models.commercial_subscription import CommercialSubscription
from app.models.commercial_upgrade_intent import CommercialUpgradeIntent
from app.models.lead import Lead
from app.models.organization import Organization
from app.models.service_order import ServiceOrder
from app.models.user import User


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
        if len(users) == 1 and leads == 0 and orders == 0 and (not subscription or subscription.plan == "FREE") and not active_intent:
            rows.append({"organization_id": organization.id, "organization_name": organization.name, "user_id": users[0].id, "user": users[0].full_name or users[0].username})
    return rows


def migrate_legacy_user_to_primary(db: Session, *, user_id: int, actor: User) -> User:
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
    user.organization_id = primary.id
    user.manager_id = None
    user.onboarding_source = "MIGRATED_LEGACY"
    audit_auth_event(db, request=None, event_type="ORGANIZATION_MIGRATED", outcome="SUCCESS", user=user, actor=actor, detail={"from_organization_id": previous_organization_id, "to_organization_id": primary.id, "reason": "legacy_single_user_workspace"})
    db.commit()
    db.refresh(user)
    return user
