from datetime import datetime
import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, inspect, or_
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_db, require_admin_user, require_root_user, user_access_block_reason
from app.auth.routes import create_email_verification_link, issue_email_verification
from app.core.auth_security import audit_auth_event
from app.core.security import hash_password
from app.core.user_status import PENDING_ADMIN_STATUSES, PENDING_USER_STATUSES
from app.models.lead import Lead
from app.models.user import User
from app.models.organization import Organization
from app.core.organization import create_independent_organization, get_platform_primary_organization
from app.models.user_lifecycle import UserLifecycleEvent, UserReactivationRequest
from app.schemas.auth_schema import UserApprovalRequest
from app.schemas.user_schema import (
    UserAnonymizeRequest,
    UserArchiveRequest,
    UserLifecycleEventResponse,
    UserLifecycleRequest,
    UserReactivateRequest,
    UserReactivationRequestResponse,
    UserResponse,
)
from app.services.notification_service import dispatch_web_push_for_notification_ids, notify_user_activation
from app.services.commercial_upgrade_service import (
    activate_upgrade_intent,
    cancel_upgrade_intent,
    global_commercial_metrics,
    list_global_upgrade_intents,
    mark_payment_confirmed,
)
from app.services.user_lifecycle_service import record_user_lifecycle_event, revoke_user_access, transition_user_status
from app.services.entitlement_service import current_plan, ensure_user_commercial_profile, resolve_plan, set_user_entitlement_plan
from app.services.legacy_workspace_migration_service import legacy_workspace_candidates, legacy_workspace_diagnostics, migrate_legacy_user_to_primary
from app.services.organization_onboarding_service import send_invitation_email
from app.services.organization_provisioning_service import provision_organization, provision_response
from app.services.platform_admin_service import (
    get_platform_organization,
    get_platform_user,
    list_platform_organizations,
    list_platform_users,
    platform_directory_metrics,
)


router = APIRouter(prefix="/admin", tags=["admin"])
PENDING_EMAIL_SUPPORT_STATUSES = PENDING_USER_STATUSES


class ManualEmailVerificationRequest(BaseModel):
    reason: str


class CommercialIntentActionRequest(BaseModel):
    confirmation: str | None = None


class PlatformSupervisorRequest(BaseModel):
    supervisor_id: int | None = None


class PlatformEntitlementRequest(BaseModel):
    plan: str


class PlatformRoleRequest(BaseModel):
    role: str
    subordinate_action: str | None = None
    transfer_to_supervisor_id: int | None = None


class LegacyMigrationRequest(BaseModel):
    confirm: bool = False
    reason: str | None = None


class OrganizationProvisionPayload(BaseModel):
    name: str
    slug: str | None = None
    country: str = "MX"
    language: str = "es"
    currency: str = "MXN"
    timezone: str = "America/Cancun"
    plan: str = "FREE"
    manager_full_name: str
    manager_email: str
    manager_language: str = "es"


def require_reason(reason: str) -> str:
    clean_reason = (reason or "").strip()
    if len(clean_reason) < 3:
        raise HTTPException(status_code=400, detail="Motivo obrigatorio")
    return clean_reason[:2000]


def visible_user_query(db: Session, actor: User):
    if actor.role not in {"ROOT", "GERENTE"}:
        raise HTTPException(status_code=403, detail="Tecnico nao pode executar acoes administrativas")
    query = db.query(User)
    if actor.organization_id:
        query = query.filter(User.organization_id == actor.organization_id)
    if actor.role == "GERENTE":
        query = query.filter(or_(User.manager_id == actor.id, User.id == actor.id))
    return query


def load_target_user(db: Session, user_id: int, actor: User) -> User:
    user = visible_user_query(db, actor).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario nao encontrado")
    if actor.role == "GERENTE" and user.id == actor.id:
        raise HTTPException(status_code=403, detail="Supervisor nao pode alterar a propria conta")
    return user


def active_root_count(db: Session) -> int:
    return (
        db.query(func.count(User.id))
        .filter(User.role == "ROOT", User.status == "ACTIVE", User.is_active.is_(True))
        .scalar()
        or 0
    )


def validate_role_and_manager(db: Session, actor: User, role: str, manager_id: int | None) -> tuple[str, int | None]:
    role = role.upper()
    if role not in {"GERENTE", "BROKER"}:
        raise HTTPException(status_code=400, detail="Role deve ser GERENTE ou BROKER")
    if actor.role == "GERENTE" and role != "BROKER":
        raise HTTPException(status_code=403, detail="Supervisor pode reativar apenas tecnicos")
    if role == "BROKER":
        if actor.role == "GERENTE":
            return role, actor.id
        if manager_id is not None:
            manager = (
                db.query(User)
                .filter(
                    User.id == manager_id,
                    User.role == "GERENTE",
                    User.status == "ACTIVE",
                    User.is_active.is_(True),
                    User.organization_id == actor.organization_id,
                )
                .first()
            )
            if not manager:
                raise HTTPException(status_code=400, detail="Supervisor responsavel nao encontrado")
        return role, manager_id
    return role, None


def handle_user_clients(db: Session, user: User, action: str) -> int:
    active_clients = (
        db.query(Lead)
        .filter(Lead.assigned_to_user_id == user.id, Lead.organization_id == user.organization_id)
        .all()
    )
    if active_clients and action.upper() != "UNASSIGN":
        raise HTTPException(status_code=409, detail="Usuario possui clientes ativos. Reatribua ou libere antes de concluir.")
    for lead in active_clients:
        lead.assigned_to_user_id = None
        lead.updated_at = datetime.utcnow()
    return len(active_clients)


def approve_pending_user(db: Session, user: User, actor: User, payload: UserApprovalRequest) -> User:
    if not user.email_verified:
        raise HTTPException(status_code=400, detail="Correo pendiente de confirmación")
    if (user.status or "").upper() not in PENDING_ADMIN_STATUSES:
        raise HTTPException(status_code=400, detail="Usuario no está pendiente de aprobación administrativa")
    previous_organization_id = user.organization_id
    role = payload.role.upper()
    if role not in {"GERENTE", "BROKER"}:
        raise HTTPException(status_code=400, detail="Role deve ser GERENTE ou BROKER")
    organization_mode = (payload.organization_mode or "INDEPENDENT").upper()
    if organization_mode not in {"INDEPENDENT", "EXISTING"}:
        raise HTTPException(status_code=400, detail="organization_mode deve ser INDEPENDENT ou EXISTING")
    standard_signup = user.onboarding_source == "STANDARD"
    if standard_signup:
        target_organization = db.query(Organization).filter(Organization.id == user.organization_id).first()
        primary = get_platform_primary_organization(db)
        if not target_organization or target_organization.id != primary.id:
            raise HTTPException(status_code=409, detail="Cadastro padrão está fora da organização principal")
        organization_mode = "STANDARD"
    elif organization_mode == "EXISTING":
        if payload.organization_id is None:
            raise HTTPException(status_code=400, detail="organization_id é obrigatório para organização existente")
        if actor.role != "ROOT" and payload.organization_id != actor.organization_id:
            raise HTTPException(status_code=403, detail="Organização fora do escopo administrativo")
        target_organization = db.query(Organization).filter(Organization.id == payload.organization_id).first()
        if not target_organization:
            raise HTTPException(status_code=404, detail="Organização não encontrada")
    else:
        target_organization = db.query(Organization).filter(Organization.id == user.organization_id).first()
        if user.onboarding_source != "INDEPENDENT" or not target_organization or target_organization.slug == "total-solutions-cancun":
            target_organization = create_independent_organization(
                db,
                name=user.company or user.full_name or user.username,
                pending_onboarding=True,
            )
        user.organization_id = target_organization.id
        user.onboarding_source = "INDEPENDENT"
    if organization_mode == "EXISTING":
        user.onboarding_source = "TEAM"

    manager_id = payload.manager_id if role == "BROKER" else None
    if actor.role == "GERENTE" and role != "BROKER":
        raise HTTPException(status_code=403, detail="Supervisor pode aprovar apenas técnicos")
    if manager_id is not None:
        manager = db.query(User).filter(
            User.id == manager_id,
            User.organization_id == target_organization.id,
            User.role == "GERENTE",
            User.status == "ACTIVE",
            User.is_active.is_(True),
        ).first()
        if not manager:
            raise HTTPException(status_code=400, detail="Supervisor responsável não encontrado na organização")
    previous_status = user.status
    # Approval activates the account, but must not downgrade an existing
    # individual entitlement that the platform already resolved for this user.
    if inspect(db.get_bind()).has_table("commercial_subscriptions"):
        approval_plan = resolve_plan(db, user)["plan"]
    else:
        approval_plan = (user.plan or "FREE").strip().upper()
    if approval_plan not in {"PRO", "BUSINESS"}:
        approval_plan = "FREE"
    user.role = role
    user.manager_id = manager_id
    user.plan = approval_plan
    user.organization_id = target_organization.id
    if target_organization.status == "PENDING_ONBOARDING":
        target_organization.status = "ACTIVE"
    if payload.plan_max_brokers is not None:
        user.plan_max_brokers = max(payload.plan_max_brokers, 0)
    if payload.plan_max_leads is not None:
        user.plan_max_leads = max(payload.plan_max_leads, 0)
    user.status = "ACTIVE"
    user.is_active = True
    user.status_reason = "Cadastro aprovado"
    user.status_changed_at = datetime.utcnow()
    user.status_changed_by = actor.id
    ensure_user_commercial_profile(db, user, plan=approval_plan, source="PLATFORM_SIGNUP" if standard_signup else "ONBOARDING", granted_by_user_id=actor.id)
    if organization_mode == "EXISTING" and previous_organization_id != target_organization.id:
        previous_organization = db.query(Organization).filter(Organization.id == previous_organization_id).first()
        remaining_users = db.query(User).filter(
            User.organization_id == previous_organization_id,
            User.id != user.id,
        ).count()
        if previous_organization and previous_organization.status == "PENDING_ONBOARDING" and remaining_users == 0:
            previous_organization.status = "ORPHANED_ONBOARDING"
    revoke_user_access(db, user, deactivate_push=True)
    onboarding_event = {
        ("STANDARD", "GERENTE"): "ONBOARDING_APPROVED_STANDARD",
        ("STANDARD", "BROKER"): "ONBOARDING_APPROVED_STANDARD",
        ("INDEPENDENT", "GERENTE"): "ONBOARDING_APPROVED_SUPERVISOR",
        ("INDEPENDENT", "BROKER"): "ONBOARDING_APPROVED_INDEPENDENT",
        ("EXISTING", "BROKER"): "ONBOARDING_ASSIGNED_TO_ORGANIZATION",
        ("EXISTING", "GERENTE"): "ONBOARDING_ASSIGNED_TO_ORGANIZATION",
    }[(organization_mode, role)]
    record_user_lifecycle_event(
        db,
        user=user,
        actor=actor,
        event_type=onboarding_event,
        from_status=previous_status,
        to_status="ACTIVE",
        reason="Cadastro aprovado",
        metadata={"role": role, "manager_id": manager_id, "organization_id": target_organization.id, "organization_mode": organization_mode},
    )
    return user


@router.get("/users", response_model=list[UserResponse])
def list_users_by_status(
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_user),
):
    query = visible_user_query(db, actor)
    if status:
        normalized = status.strip().upper()
        if normalized == "PENDING":
            query = query.filter(User.status.in_(PENDING_USER_STATUSES))
        else:
            query = query.filter(User.status == normalized)
    return query.order_by(User.registered_at.desc(), User.id.desc()).all()


@router.get("/users/pending", response_model=list[UserResponse])
def pending_users(
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_user),
):
    return (
        visible_user_query(db, actor)
        .filter(User.status.in_(PENDING_USER_STATUSES))
        .order_by(User.registered_at.desc(), User.id.desc())
        .all()
    )


def global_onboarding_pending_query(db: Session):
    return (
        db.query(User)
        .filter(
            User.status.in_(PENDING_ADMIN_STATUSES),
        )
        .order_by(User.registered_at.desc(), User.id.desc())
    )


def serialize_global_onboarding_user(db: Session, user: User) -> dict:
    organization = db.query(Organization).filter(Organization.id == user.organization_id).first()
    supervisor = db.query(User).filter(User.id == user.manager_id).first() if user.manager_id else None
    return {
        "id": user.id,
        "full_name": user.full_name,
        "email": user.email,
        "pais_operacao": user.pais_operacao,
        "estado_operacao": user.estado_operacao,
        "cidade_operacao": user.cidade_operacao,
        "requested_role": user.role,
        "organization_id": user.organization_id,
        "organization_name": organization.name if organization else None,
        "organization_status": organization.status if organization else None,
        "onboarding_source": user.onboarding_source,
        "created_at": user.registered_at.isoformat() if user.registered_at else None,
        "supervisor": supervisor.full_name or supervisor.username if supervisor else None,
        "status": user.status,
        "email_verified": user.email_verified,
        "access_block_reason": user_access_block_reason(db, user),
    }


@router.get("/onboarding/pending")
def pending_onboarding_users(
    db: Session = Depends(get_db),
    actor: User = Depends(require_root_user),
):
    return [serialize_global_onboarding_user(db, user) for user in global_onboarding_pending_query(db).all()]


def load_global_onboarding_user(db: Session, user_id: int) -> User:
    user = global_onboarding_pending_query(db).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Cadastro de onboarding não encontrado")
    return user


@router.post("/onboarding/{user_id}/approve", response_model=UserResponse)
def approve_onboarding_user(
    user_id: int,
    payload: UserApprovalRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_root_user),
):
    user = load_global_onboarding_user(db, user_id)
    approved = approve_pending_user(db, user, actor, payload)
    db.commit()
    db.refresh(approved)
    notification_ids = notify_user_activation(db, activated_user=approved, actor=actor)
    db.commit()
    dispatch_web_push_for_notification_ids(db, notification_ids)
    db.refresh(approved)
    return approved


@router.post("/onboarding/{user_id}/reject", response_model=UserResponse)
def reject_onboarding_user(
    user_id: int,
    payload: UserLifecycleRequest | None = None,
    db: Session = Depends(get_db),
    actor: User = Depends(require_root_user),
):
    user = load_global_onboarding_user(db, user_id)
    transition_user_status(
        db,
        user=user,
        actor=actor,
        to_status="SUSPENDED",
        reason=require_reason(payload.reason if payload else "Cadastro rejeitado no onboarding global"),
        event_type="ONBOARDING_REJECTED",
        is_active=False,
    )
    db.commit()
    db.refresh(user)
    return user


@router.get("/commercial/metrics")
def admin_commercial_metrics(
    db: Session = Depends(get_db),
    actor: User = Depends(require_root_user),
):
    return global_commercial_metrics(db)


@router.get("/platform/metrics")
def admin_platform_metrics(
    db: Session = Depends(get_db),
    actor: User = Depends(require_root_user),
):
    return platform_directory_metrics(db)


@router.get("/platform/organizations")
def admin_platform_organizations(
    search: str | None = Query(default=None),
    plan: str | None = Query(default=None),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    actor: User = Depends(require_root_user),
):
    return {"organizations": list_platform_organizations(db, search=search, plan=plan, status=status)}


@router.post("/platform/organizations/provision", status_code=201)
def admin_provision_organization(
    payload: OrganizationProvisionPayload,
    db: Session = Depends(get_db),
    actor: User = Depends(require_root_user),
):
    if not payload.manager_full_name.strip():
        raise HTTPException(status_code=422, detail="Nome completo do gerente é obrigatório")
    try:
        organization, invitation, raw_token = provision_organization(
            db,
            name=payload.name,
            slug=payload.slug,
            country=payload.country,
            language=payload.language,
            currency=payload.currency,
            timezone=payload.timezone,
            plan=payload.plan,
            manager_full_name=payload.manager_full_name,
            manager_email=payload.manager_email,
            invited_by=actor,
        )
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Não foi possível provisionar a organização") from exc

    base_url = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    invite_url = f"{base_url}/invite/{raw_token}" if base_url else f"/invite/{raw_token}"
    email_sent = send_invitation_email(
        to_email=invitation.invited_email,
        organization_name=organization.name,
        role=invitation.role,
        invite_url=invite_url,
    )
    return provision_response(
        organization,
        invitation,
        email_delivery_status="sent" if email_sent else "unavailable",
        warnings=[
            "region_and_city_not_persisted: Organization ainda não possui campos para localização regional",
            "manager_name_and_language_are_collected_for_future_invite_metadata",
        ],
    )


@router.get("/platform/organizations/{organization_id}")
def admin_platform_organization(
    organization_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(require_root_user),
):
    organization = get_platform_organization(db, organization_id)
    if not organization:
        raise HTTPException(status_code=404, detail="Organização não encontrada")
    return organization


@router.get("/platform/users")
def admin_platform_users(
    search: str | None = Query(default=None),
    role: str | None = Query(default=None),
    status: str | None = Query(default=None),
    organization_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
    actor: User = Depends(require_root_user),
):
    return {"users": list_platform_users(db, search=search, role=role, status=status, organization_id=organization_id)}


@router.get("/platform/users/{user_id}")
def admin_platform_user(
    user_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(require_root_user),
):
    user = get_platform_user(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    return user


@router.get("/platform/users/{user_id}/access-diagnostic")
def admin_platform_user_access_diagnostic(
    user_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(require_root_user),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    supervisor = db.query(User).filter(User.id == user.manager_id).first() if user.manager_id else None
    return {
        "user_id": user.id,
        "email": user.email,
        "email_verified": bool(user.email_verified),
        "status": user.status,
        "role": user.role,
        "is_active": bool(user.is_active),
        "onboarding_source": user.onboarding_source,
        "organization_id": user.organization_id,
        "supervisor": {
            "user_id": supervisor.id,
            "name": supervisor.full_name or supervisor.username,
        } if supervisor else None,
        "access_block_reason": user_access_block_reason(db, user),
    }


@router.patch("/platform/users/{user_id}/role")
def admin_platform_change_role(
    user_id: int,
    payload: PlatformRoleRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_root_user),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    if not user.is_active or user.status != "ACTIVE":
        raise HTTPException(status_code=409, detail="Usuário não está ativo")
    requested_role = payload.role.strip().upper()
    if requested_role == "SUPERVISOR":
        requested_role = "GERENTE"
    if requested_role not in {"BROKER", "GERENTE"}:
        raise HTTPException(status_code=400, detail="Role permitida: TÉCNICO ou SUPERVISOR")
    if requested_role == "GERENTE" and current_plan(db, user) not in {"PRO", "BUSINESS"}:
        raise HTTPException(status_code=403, detail="Supervisor exige entitlement PRO ou BUSINESS")
    if requested_role == user.role:
        return {"user_id": user.id, "role": user.role, "organization_id": user.organization_id, "plan": current_plan(db, user)}

    old_role = user.role
    subordinates = db.query(User).filter(User.manager_id == user.id, User.organization_id == user.organization_id, User.role == "BROKER").all()
    if old_role == "GERENTE" and requested_role == "BROKER" and subordinates:
        action = (payload.subordinate_action or "").strip().upper()
        if action not in {"CLEAR", "TRANSFER"}:
            raise HTTPException(status_code=409, detail="Trate os subordinados com CLEAR ou TRANSFER antes do rebaixamento")
        new_supervisor = None
        if action == "TRANSFER":
            if payload.transfer_to_supervisor_id is None or payload.transfer_to_supervisor_id == user.id:
                raise HTTPException(status_code=400, detail="Supervisor de destino obrigatório")
            new_supervisor = db.query(User).filter(
                User.id == payload.transfer_to_supervisor_id,
                User.role == "GERENTE",
                User.is_active.is_(True),
                User.status == "ACTIVE",
                User.organization_id == user.organization_id,
            ).first()
            if not new_supervisor:
                raise HTTPException(status_code=400, detail="Supervisor de destino inválido")
        for subordinate in subordinates:
            subordinate.manager_id = new_supervisor.id if new_supervisor else None
    if requested_role == "GERENTE":
        user.manager_id = None
    user.role = requested_role
    event_type = "USER_PROMOTED_TO_SUPERVISOR" if requested_role == "GERENTE" else "USER_ROLE_CHANGED"
    audit_auth_event(
        db,
        request=None,
        event_type=event_type,
        outcome="SUCCESS",
        user=user,
        actor=actor,
        detail={"old_role": old_role, "new_role": requested_role, "organization_id": user.organization_id, "subordinate_action": payload.subordinate_action},
    )
    if old_role == "GERENTE" and requested_role == "BROKER" and subordinates:
        audit_auth_event(db, request=None, event_type="SUPERVISOR_TEAM_REASSIGNED", outcome="SUCCESS", user=user, actor=actor, detail={"subordinate_count": len(subordinates), "subordinate_action": payload.subordinate_action, "transfer_to_supervisor_id": payload.transfer_to_supervisor_id})
    db.commit()
    return {"user_id": user.id, "role": user.role, "organization_id": user.organization_id, "plan": current_plan(db, user)}


@router.patch("/platform/users/{user_id}/supervisor")
def admin_platform_assign_supervisor(
    user_id: int,
    payload: PlatformSupervisorRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_root_user),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    if user.role != "BROKER":
        raise HTTPException(status_code=400, detail="Somente técnicos podem receber supervisor")
    supervisor = None
    if payload.supervisor_id is not None:
        supervisor = db.query(User).filter(
            User.id == payload.supervisor_id,
            User.role == "GERENTE",
            User.is_active.is_(True),
            User.status == "ACTIVE",
            User.organization_id == user.organization_id,
        ).first()
        if not supervisor:
            raise HTTPException(status_code=400, detail="Supervisor ativo da mesma organização não encontrado")
    previous_id = user.manager_id
    user.manager_id = supervisor.id if supervisor else None
    audit_auth_event(
        db,
        request=None,
        event_type="SUPERVISOR_ASSIGNED",
        outcome="SUCCESS",
        user=user,
        actor=actor,
        detail={"previous_supervisor_id": previous_id, "supervisor_id": user.manager_id, "organization_id": user.organization_id},
    )
    db.commit()
    return {"user_id": user.id, "organization_id": user.organization_id, "supervisor_id": user.manager_id}


@router.patch("/platform/users/{user_id}/entitlement")
def admin_platform_set_entitlement(
    user_id: int,
    payload: PlatformEntitlementRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_root_user),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    plan = payload.plan.strip().upper()
    if plan not in {"FREE", "PRO", "BUSINESS"}:
        raise HTTPException(status_code=400, detail="Plano inválido")
    profile = set_user_entitlement_plan(db, user, plan, actor=actor, source="ROOT_ADMIN")
    audit_auth_event(db, request=None, event_type="USER_ENTITLEMENT_CHANGED", outcome="SUCCESS", user=user, actor=actor, detail={"plan": profile.plan, "organization_id": user.organization_id})
    db.commit()
    return {"user_id": user.id, "plan": profile.plan, "organization_id": user.organization_id}


@router.get("/platform/migrations/legacy-users")
def admin_legacy_workspace_candidates(
    db: Session = Depends(get_db),
    actor: User = Depends(require_root_user),
):
    return {"candidates": legacy_workspace_candidates(db), "diagnostics": legacy_workspace_diagnostics(db)}


@router.post("/platform/migrations/legacy-users/{user_id}")
def admin_migrate_legacy_user(
    user_id: int,
    payload: LegacyMigrationRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_root_user),
):
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Confirmação explícita obrigatória para migrar o usuário")
    user = migrate_legacy_user_to_primary(db, user_id=user_id, actor=actor, reason=payload.reason)
    return {"user_id": user.id, "organization_id": user.organization_id, "onboarding_source": user.onboarding_source}


@router.get("/commercial/intents")
def admin_commercial_intents(
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    actor: User = Depends(require_root_user),
):
    return {"intents": list_global_upgrade_intents(db, status=status)}


@router.post("/commercial/intents/{intent_id}/confirm-payment")
def admin_confirm_commercial_payment(
    intent_id: int,
    request: Request,
    payload: CommercialIntentActionRequest | None = None,
    db: Session = Depends(get_db),
    actor: User = Depends(require_root_user),
):
    if not payload or payload.confirmation != "CONFIRM":
        raise HTTPException(status_code=400, detail="Confirmação explícita obrigatória")
    intent = mark_payment_confirmed(db, actor, intent_id, request=request, global_scope=True)
    return {"intent_id": intent.id, "status": intent.status, "confirmation_source": intent.confirmation_source}


@router.post("/commercial/intents/{intent_id}/activate")
def admin_activate_commercial_intent(
    intent_id: int,
    request: Request,
    db: Session = Depends(get_db),
    actor: User = Depends(require_root_user),
):
    intent, subscription = activate_upgrade_intent(db, actor, intent_id, request=request, global_scope=True)
    return {"intent_id": intent.id, "status": intent.status, "plan": subscription.plan, "organization_id": intent.organization_id}


@router.post("/commercial/intents/{intent_id}/cancel")
def admin_cancel_commercial_intent(
    intent_id: int,
    request: Request,
    db: Session = Depends(get_db),
    actor: User = Depends(require_root_user),
):
    intent = cancel_upgrade_intent(db, actor, intent_id, request=request, global_scope=True)
    return {"intent_id": intent.id, "status": intent.status}


@router.post("/users/{user_id}/approve", response_model=UserResponse)
def approve_user(
    user_id: int,
    payload: UserApprovalRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_user),
):
    user = load_target_user(db, user_id, actor)
    if payload.organization_mode == "INDEPENDENT" and user.onboarding_source == "TEAM":
        payload = payload.model_copy(update={"organization_mode": "EXISTING", "organization_id": user.organization_id})
    approved = approve_pending_user(db, user, actor, payload)
    db.commit()
    db.refresh(approved)
    notification_ids = notify_user_activation(db, activated_user=approved, actor=actor)
    db.commit()
    dispatch_web_push_for_notification_ids(db, notification_ids)
    db.refresh(approved)
    return approved


@router.post("/users/{user_id}/suspend", response_model=UserResponse)
def suspend_user(
    user_id: int,
    payload: UserLifecycleRequest | None = None,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_user),
):
    if user_id == actor.id:
        raise HTTPException(status_code=400, detail="Administrador nao pode suspender a propria conta")
    user = load_target_user(db, user_id, actor)
    if user.role == "ROOT" and active_root_count(db) <= 1:
        raise HTTPException(status_code=400, detail="Nao e possivel suspender o ultimo administrador principal ativo")
    transition_user_status(
        db,
        user=user,
        actor=actor,
        to_status="SUSPENDED",
        reason=require_reason(payload.reason if payload else "Suspensao administrativa"),
        event_type="SUSPENDED",
        is_active=False,
    )
    db.commit()
    db.refresh(user)
    notification_ids = notify_user_activation(db, activated_user=user, actor=actor)
    db.commit()
    dispatch_web_push_for_notification_ids(db, notification_ids)
    db.refresh(user)
    return user


@router.post("/users/{user_id}/reactivate", response_model=UserResponse)
def reactivate_user(
    user_id: int,
    payload: UserReactivateRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_user),
):
    user = load_target_user(db, user_id, actor)
    if user.status not in {"SUSPENDED", "ARCHIVED", "PENDING", "PENDING_APPROVAL", "PENDING_ADMIN"}:
        raise HTTPException(status_code=400, detail="Usuario nao esta em estado de reativacao")
    role, manager_id = validate_role_and_manager(db, actor, payload.role, payload.manager_id)
    user.role = role
    user.manager_id = manager_id
    if payload.plan:
        user.plan = payload.plan.upper()
    if payload.plan_max_brokers is not None:
        user.plan_max_brokers = max(payload.plan_max_brokers, 0)
    if payload.plan_max_leads is not None:
        user.plan_max_leads = max(payload.plan_max_leads, 0)
    if payload.reset_password:
        user.password_hash = hash_password(f"reset-required-{user.id}-{datetime.utcnow().timestamp()}")
    transition_user_status(
        db,
        user=user,
        actor=actor,
        to_status="ACTIVE",
        reason=require_reason(payload.reason),
        event_type="REACTIVATED",
        is_active=True,
        metadata={"role": role, "manager_id": manager_id, "reset_password": payload.reset_password},
    )
    db.commit()
    db.refresh(user)
    return user


@router.post("/users/{user_id}/archive", response_model=UserResponse)
def archive_user(
    user_id: int,
    payload: UserArchiveRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_user),
):
    if user_id == actor.id:
        raise HTTPException(status_code=400, detail="Administrador nao pode arquivar a propria conta")
    user = load_target_user(db, user_id, actor)
    if user.role == "ROOT" and active_root_count(db) <= 1:
        raise HTTPException(status_code=400, detail="Nao e possivel arquivar o ultimo administrador principal ativo")
    released_clients = handle_user_clients(db, user, payload.client_action)
    transition_user_status(
        db,
        user=user,
        actor=actor,
        to_status="ARCHIVED",
        reason=require_reason(payload.reason),
        event_type="ARCHIVED",
        is_active=False,
        metadata={"released_clients": released_clients},
    )
    db.commit()
    db.refresh(user)
    return user


@router.post("/users/{user_id}/anonymize", response_model=UserResponse)
def anonymize_user(
    user_id: int,
    payload: UserAnonymizeRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_root_user),
):
    if user_id == actor.id:
        raise HTTPException(status_code=400, detail="Administrador nao pode anonimizar a propria conta")
    if payload.confirmation != "ANONIMIZAR":
        raise HTTPException(status_code=400, detail="Confirmacao invalida")
    user = db.query(User).filter(User.id == user_id, User.organization_id == actor.organization_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario nao encontrado")
    if user.role == "ROOT" and active_root_count(db) <= 1:
        raise HTTPException(status_code=400, detail="Nao e possivel anonimizar o ultimo administrador principal ativo")
    released_clients = handle_user_clients(db, user, payload.client_action)
    user.username = f"removed-user-{user.id}"
    user.email = None
    user.email_pessoal = None
    user.telefone = None
    user.full_name = "Usuario removido"
    user.company = None
    user.creci = None
    user.data_nascimento = None
    user.documento = None
    user.observacoes = None
    user.profile_photo_url = None
    user.email_verification_token = None
    user.email_verification_token_hash = None
    user.email_verification_expires_at = None
    user.email_verification_sent_at = None
    user.email_verification_used_at = None
    user.email_verified = False
    user.password_hash = hash_password(f"removed-{user.id}-{datetime.utcnow().timestamp()}")
    transition_user_status(
        db,
        user=user,
        actor=actor,
        to_status="ANONYMIZED",
        reason=require_reason(payload.reason),
        event_type="ANONYMIZED",
        is_active=False,
        metadata={"released_clients": released_clients},
    )
    db.commit()
    db.refresh(user)
    return user



@router.post("/users/{user_id}/email-verification/resend")
def admin_resend_email_verification(
    user_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_user),
):
    user = load_target_user(db, user_id, actor)

    if user.email_verified:
        raise HTTPException(status_code=400, detail="Correo ya verificado")

    if (user.status or "").upper() not in PENDING_EMAIL_SUPPORT_STATUSES:
        raise HTTPException(
            status_code=400,
            detail="Estado del usuario no permite soporte de verificacion",
        )

    delivered = issue_email_verification(db, user)

    audit_auth_event(
        db,
        request=None,
        event_type="ADMIN_EMAIL_VERIFICATION_RESEND",
        outcome="DELIVERED" if delivered else "DELIVERY_FAILED",
        user=user,
        actor=actor,
        detail={"channel": "email"},
    )

    db.commit()

    return {
        "ok": delivered,
        "email_verified": False,
        "message": (
            "Correo de verificacion reenviado"
            if delivered
            else "No fue posible entregar el correo. Puede generar un enlace para compartir."
        ),
    }


@router.post("/users/{user_id}/email-verification/link")
def admin_generate_email_verification_link(
    user_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_user),
):
    user = load_target_user(db, user_id, actor)

    if user.email_verified:
        raise HTTPException(status_code=400, detail="Correo ya verificado")

    if (user.status or "").upper() not in PENDING_EMAIL_SUPPORT_STATUSES:
        raise HTTPException(
            status_code=400,
            detail="Estado del usuario no permite soporte de verificacion",
        )

    verification_url = create_email_verification_link(user)

    if not verification_url:
        raise HTTPException(
            status_code=500,
            detail="No fue posible generar el enlace de verificacion",
        )

    audit_auth_event(
        db,
        request=None,
        event_type="ADMIN_EMAIL_VERIFICATION_LINK",
        outcome="GENERATED",
        user=user,
        actor=actor,
        detail={
            "channel": "manual_share",
            "expires_minutes": 60,
        },
    )

    db.commit()

    return {
        "ok": True,
        "verification_url": verification_url,
        "expires_minutes": 60,
        "message": "Enlace de verificacion generado",
    }


@router.post("/users/{user_id}/email-verification/manual", response_model=UserResponse)
def admin_manual_email_verification(
    user_id: int,
    payload: ManualEmailVerificationRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_user),
):
    user = load_target_user(db, user_id, actor)
    reason = require_reason(payload.reason)

    if user.email_verified:
        raise HTTPException(status_code=400, detail="Correo ya verificado")

    current_status = (user.status or "").upper()

    if current_status not in PENDING_EMAIL_SUPPORT_STATUSES:
        raise HTTPException(
            status_code=400,
            detail="Estado del usuario no permite verificacion manual",
        )

    previous_status = user.status
    next_status = "PENDING_ADMIN" if current_status == "PENDING_EMAIL" else user.status

    user.email_verified = True
    user.email_verification_token = None
    user.email_verification_token_hash = None
    user.email_verification_expires_at = None
    user.email_verification_used_at = datetime.utcnow()

    if next_status != user.status:
        user.status = next_status
        user.status_reason = reason
        user.status_changed_at = datetime.utcnow()
        user.status_changed_by = actor.id

    record_user_lifecycle_event(
        db,
        user=user,
        actor=actor,
        event_type="EMAIL_VERIFIED_MANUALLY",
        from_status=previous_status,
        to_status=user.status,
        reason=reason,
        metadata={"method": "admin_manual_override"},
    )

    audit_auth_event(
        db,
        request=None,
        event_type="ADMIN_EMAIL_VERIFICATION_MANUAL",
        outcome="SUCCESS",
        user=user,
        actor=actor,
        detail={
            "reason": reason,
            "previous_status": previous_status,
            "new_status": user.status,
        },
    )

    db.commit()
    db.refresh(user)
    return user


@router.get("/users/{user_id}/events", response_model=list[UserLifecycleEventResponse])
def user_lifecycle_events(
    user_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_user),
):
    load_target_user(db, user_id, actor)
    return (
        db.query(UserLifecycleEvent)
        .filter(UserLifecycleEvent.user_id == user_id)
        .order_by(UserLifecycleEvent.created_at.desc(), UserLifecycleEvent.id.desc())
        .all()
    )


@router.get("/reactivation-requests", response_model=list[UserReactivationRequestResponse])
def list_reactivation_requests(
    status: str | None = Query(default="PENDING"),
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_user),
):
    query = db.query(UserReactivationRequest).join(User, User.id == UserReactivationRequest.user_id)
    if actor.organization_id:
        query = query.filter(UserReactivationRequest.organization_id == actor.organization_id)
    if actor.role == "GERENTE":
        query = query.filter(User.manager_id == actor.id)
    if status:
        query = query.filter(UserReactivationRequest.status == status.upper())
    return query.order_by(UserReactivationRequest.created_at.desc(), UserReactivationRequest.id.desc()).all()


@router.post("/reactivation-requests/{request_id}/reject", response_model=UserReactivationRequestResponse)
def reject_reactivation_request(
    request_id: int,
    payload: UserLifecycleRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_user),
):
    request_row = (
        db.query(UserReactivationRequest)
        .filter(UserReactivationRequest.id == request_id, UserReactivationRequest.organization_id == actor.organization_id)
        .first()
    )
    if not request_row:
        raise HTTPException(status_code=404, detail="Solicitacao nao encontrada")
    load_target_user(db, request_row.user_id, actor)
    request_row.status = "REJECTED"
    request_row.reviewed_by = actor.id
    request_row.reviewed_at = datetime.utcnow()
    request_row.review_reason = require_reason(payload.reason)
    user = db.query(User).filter(User.id == request_row.user_id).first()
    if user:
        record_user_lifecycle_event(
            db,
            user=user,
            actor=actor,
            event_type="REACTIVATION_REJECTED",
            from_status=user.status,
            to_status=user.status,
            reason=request_row.review_reason,
            metadata={"request_id": request_row.id},
        )
    db.commit()
    db.refresh(request_row)
    return request_row
