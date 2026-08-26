"""Central commercial catalog, entitlements and usage reporting."""

from datetime import datetime

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.models.commercial_subscription import CommercialSubscription, PlanChangeEvent
from app.models.lead import Lead
from app.models.organization import Organization
from app.models.service_opportunity import ServiceOpportunity
from app.models.service_order import ServiceOrder
from app.models.user import User
from app.models.user_commercial_profile import UserCommercialProfile


PLANS = {
    "FREE": {"name": "FREE", "price": "MX$ 0 / mês", "monthly_reference": 0, "recommended": False,
             "features": {"CRM_BASIC", "PABLO_BASIC", "RADAR", "AGENDA"},
             "limits": {"users": 1, "clients": 100, "service_orders": 200, "pablo_actions": 50, "radar_opportunities": 5}},
    "PRO": {"name": "PRO", "price": "MX$ 499 / mês", "monthly_reference": 499, "recommended": True,
            "features": {"CRM_BASIC", "PABLO_BASIC", "PABLO_FULL", "RADAR", "RADAR_FULL", "AGENDA", "LOCATION", "DOCUMENTS", "VISION"},
            "limits": {"users": 5, "clients": 1000, "service_orders": 3000, "pablo_actions": 1000, "radar_opportunities": 200}},
    "BUSINESS": {"name": "BUSINESS", "price": "MX$ 1.999 / mês", "monthly_reference": 1999, "recommended": False,
                 "features": {"CRM_BASIC", "PABLO_BASIC", "PABLO_FULL", "RADAR", "RADAR_FULL", "AGENDA", "LOCATION", "DOCUMENTS", "VISION", "TEAM_MANAGEMENT", "DISTRIBUTION", "REPORTS", "PABLO_SUPERVISOR"},
                 "limits": {"users": 100, "clients": 10000, "service_orders": 30000, "pablo_actions": 10000, "radar_opportunities": 1000}},
}

LEGACY_PLAN_MAP = {"STARTER": "FREE", "INTERNAL": "FREE"}


def normalize_plan(plan: str | None) -> str:
    value = (plan or "FREE").strip().upper()
    return LEGACY_PLAN_MAP.get(value, value if value in PLANS else "FREE")


def plan_catalog() -> list[dict]:
    return [{**plan, "features": sorted(plan["features"]), "limits": dict(plan["limits"])} for plan in PLANS.values()]


def ensure_user_commercial_profile(db: Session, user: User, *, plan: str = "FREE", source: str = "ONBOARDING", granted_by_user_id: int | None = None) -> UserCommercialProfile:
    UserCommercialProfile.__table__.create(bind=db.get_bind(), checkfirst=True)
    profile = db.query(UserCommercialProfile).filter(UserCommercialProfile.user_id == user.id).first()
    if profile:
        return profile
    profile = UserCommercialProfile(
        user_id=user.id,
        plan=normalize_plan(plan),
        source=(source or "ONBOARDING")[:40],
        granted_by_user_id=granted_by_user_id,
    )
    db.add(profile)
    db.flush()
    return profile


def set_user_entitlement_plan(db: Session, user: User, plan: str, *, actor: User, source: str = "ADMIN_GRANT") -> UserCommercialProfile:
    profile = ensure_user_commercial_profile(db, user, source=source, granted_by_user_id=actor.id)
    profile.plan = normalize_plan(plan)
    profile.source = (source or "ADMIN_GRANT")[:40]
    profile.granted_by_user_id = actor.id
    profile.updated_at = datetime.utcnow()
    return profile


def subscription_for(db: Session, actor: User, *, create: bool = False) -> CommercialSubscription | None:
    return subscription_for_organization(db, actor.organization_id, create=create)


def subscription_for_organization(db: Session, organization_id: int | None, *, create: bool = False) -> CommercialSubscription | None:
    subscription = db.query(CommercialSubscription).filter(CommercialSubscription.organization_id == organization_id).first()
    if subscription or not create:
        return subscription
    organization = db.query(Organization).filter(Organization.id == organization_id).first()
    plan = normalize_plan(getattr(organization, "plan", None))
    subscription = CommercialSubscription(
        organization_id=organization_id, plan=plan,
        status="LAUNCH_ACCESS", provider="MOCK", reference_price=PLANS[plan]["price"],
    )
    db.add(subscription)
    db.flush()
    return subscription


def resolve_plan(db: Session, actor: User) -> dict:
    profile_table_exists = inspect(db.get_bind()).has_table(UserCommercialProfile.__tablename__)
    # Managers administer the tenant, so a paid organization cannot be
    # downgraded by their individual profile. Technicians may still retain an
    # explicit individual entitlement inside that organization.
    subscription = subscription_for(db, actor)
    if subscription and actor.role in {"ROOT", "GERENTE"} and normalize_plan(subscription.plan) in {"PRO", "BUSINESS"}:
        return {"plan": normalize_plan(subscription.plan), "source": "ORGANIZATION_SUBSCRIPTION"}
    profile = None
    if profile_table_exists:
        profile = db.query(UserCommercialProfile).filter(UserCommercialProfile.user_id == actor.id).first()
    if profile and profile.status == "ACTIVE":
        return {"plan": normalize_plan(profile.plan), "source": "USER_COMMERCIAL_PROFILE"}
    if subscription:
        return {"plan": normalize_plan(subscription.plan), "source": "ORGANIZATION_SUBSCRIPTION"}
    organization = db.query(Organization).filter(Organization.id == actor.organization_id).first()
    return {"plan": normalize_plan(getattr(organization, "plan", None)), "source": "ORGANIZATION_PLAN_FALLBACK"}


def current_plan(db: Session, actor: User) -> str:
    return resolve_plan(db, actor)["plan"]


def get_plan_limits(db: Session, actor: User) -> dict:
    return dict(PLANS[current_plan(db, actor)]["limits"])


def has_entitlement(db: Session, actor: User, feature: str) -> bool:
    return feature.upper() in PLANS[current_plan(db, actor)]["features"]


def can_use_feature(db: Session, actor: User, feature: str) -> bool:
    return has_entitlement(db, actor, feature)


def get_usage(db: Session, actor: User) -> dict:
    org = actor.organization_id
    return {
        "users": db.query(User).filter(User.organization_id == org, User.is_active.is_(True)).count(),
        "clients": db.query(Lead).filter(Lead.organization_id == org).count(),
        "service_orders": db.query(ServiceOrder).filter(ServiceOrder.organization_id == org).count(),
        "radar_opportunities": db.query(ServiceOpportunity).filter(ServiceOpportunity.organization_id == org, ServiceOpportunity.status == "AVAILABLE").count(),
        "pablo_actions": None,
    }


def account_snapshot(db: Session, actor: User) -> dict:
    subscription = subscription_for(db, actor, create=True)
    db.commit()
    db.refresh(subscription)
    effective_plan = current_plan(db, actor)
    plan = PLANS[effective_plan]
    history = db.query(PlanChangeEvent).filter(PlanChangeEvent.organization_id == actor.organization_id).order_by(PlanChangeEvent.created_at.desc()).limit(20).all()
    return {
        "plan": effective_plan, "organization_plan": normalize_plan(subscription.plan), "status": subscription.status, "provider": subscription.provider,
        "reference_price": subscription.reference_price, "started_at": subscription.started_at.isoformat(),
        "expires_at": subscription.expires_at.isoformat() if subscription.expires_at else None,
        "features": sorted(plan["features"]), "limits": dict(plan["limits"]), "usage": get_usage(db, actor),
        "history": [{"previous_plan": item.previous_plan, "new_plan": item.new_plan, "provider": item.provider, "reason": item.reason, "created_at": item.created_at.isoformat()} for item in history],
        "launch_message": "Acesso promocional liberado. Pagamento online em breve.",
    }


def record_plan_change(
    db: Session,
    actor: User,
    new_plan: str,
    reason: str | None = None,
    *,
    organization_id: int | None = None,
    provider: str = "MOCK",
) -> CommercialSubscription:
    new_plan = normalize_plan(new_plan)
    target_organization_id = organization_id or actor.organization_id
    subscription = subscription_for_organization(db, target_organization_id, create=True)
    previous = normalize_plan(subscription.plan)
    subscription.plan = new_plan
    subscription.status = "ACTIVE" if provider.upper() == "STRIPE" else "LAUNCH_ACCESS"
    subscription.provider = provider.upper()
    subscription.reference_price = PLANS[new_plan]["price"]
    subscription.updated_at = datetime.utcnow()
    db.add(PlanChangeEvent(organization_id=target_organization_id, actor_user_id=actor.id, previous_plan=previous,
                           new_plan=new_plan, provider=provider.upper(), reason=reason or "mock_billing"))
    organization = db.query(Organization).filter(Organization.id == target_organization_id).first()
    if organization:
        organization.plan = new_plan
    db.commit()
    db.refresh(subscription)
    return subscription
