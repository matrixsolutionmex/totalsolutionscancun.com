from datetime import datetime

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
from app.services.entitlement_service import resolve_plan
from app.services.commercial_upgrade_service import ACTIVE_INTENT_STATUSES


def legacy_workspace_diagnostics(db: Session) -> list[dict]:
    primary = get_platform_primary_organization(db)
    diagnostics = []
    for organization in db.query(Organization).filter(Organization.id != primary.id).all():
        users = db.query(User).filter(User.organization_id == organization.id).all()
        client_count = int(db.query(func.count(Lead.id)).filter(Lead.organization_id == organization.id).scalar() or 0)
        service_order_count = int(db.query(func.count(ServiceOrder.id)).filter(ServiceOrder.organization_id == organization.id).scalar() or 0)
        intents = db.query(CommercialUpgradeIntent).filter(CommercialUpgradeIntent.organization_id == organization.id).all()
        active_intents = [item for item in intents if item.status in ACTIVE_INTENT_STATUSES]
        subscription = db.query(CommercialSubscription).filter(CommercialSubscription.organization_id == organization.id).first()
        for user in users:
            resolved_plan = resolve_plan(db, user)
            individual_plan = resolved_plan["plan"]
            subordinate_count = db.query(User).filter(User.manager_id == user.id, User.organization_id == organization.id, User.role == "BROKER").count()
            checks = {
                "legacy_workspace": organization.id != primary.id,
                "single_user_workspace": len(users) == 1,
                "role_allowed": user.role in {"BROKER", "GERENTE"},
                "active_user": bool(user.is_active and user.status == "ACTIVE"),
                "plan_allowed": individual_plan in {"PRO", "BUSINESS"},
                "zero_clients": client_count == 0,
                "zero_service_orders": service_order_count == 0,
                "no_active_commercial_intent": not active_intents,
                "no_subordinates": subordinate_count == 0,
            }
            reasons = []
            reason_checks = (
                ("NOT_LEGACY_WORKSPACE", "legacy_workspace"),
                ("MULTIPLE_USERS", "single_user_workspace"),
                ("ROLE_NOT_ALLOWED", "role_allowed"),
                ("USER_NOT_ACTIVE", "active_user"),
                ("PLAN_NOT_ELIGIBLE", "plan_allowed"),
                ("HAS_CLIENTS", "zero_clients"),
                ("HAS_SERVICE_ORDERS", "zero_service_orders"),
                ("ACTIVE_COMMERCIAL_INTENT", "no_active_commercial_intent"),
                ("HAS_SUBORDINATES", "no_subordinates"),
            )
            for code, check_name in reason_checks:
                if not checks[check_name]:
                    reasons.append(code)
            diagnostics.append({
                "eligible": not reasons,
                "blocking_reasons": reasons,
                "checks": checks,
                "organization_id": organization.id,
                "organization_name": organization.name,
                "organization_status": organization.status,
                "organization_user_count": len(users),
                "organization_subscription": {"plan": subscription.plan, "status": subscription.status} if subscription else None,
                "historical_intent_statuses": sorted({item.status for item in intents}),
                "active_intent_statuses": sorted({item.status for item in active_intents}),
                "user_id": user.id,
                "user": user.full_name or user.username,
                "email": user.email,
                "role": user.role,
                "status": user.status,
                "onboarding_source": user.onboarding_source,
                "individual_plan": individual_plan,
                "resolved_plan": individual_plan,
                "plan_source": resolved_plan["source"],
                "displayed_plan": individual_plan,
                "clients_count": client_count,
                "service_orders_count": service_order_count,
                "subordinates_count": subordinate_count,
                "target_organization_id": primary.id,
                "target_organization_name": primary.name,
            })
    return diagnostics


def legacy_workspace_candidates(db: Session) -> list[dict]:
    return [item for item in legacy_workspace_diagnostics(db) if item["eligible"]]


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
