from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth.jwt_handler import (
    get_current_user as get_actor,
    get_db,
    require_admin_user as require_admin_actor,
    require_root_user as require_root_actor,
)
from app.core.security import hash_password, verify_password
from app.core.storage import PROFILE_PHOTOS_DIR, delete_profile_photo
from app.models.lead import Lead
from app.models.lead_event import LeadEvent
from app.models.user import User
from app.schemas.user_schema import AssignLeadsRequest, LoginRequest, ReturnLeadsRequest, UserCreate, UserResponse, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])

MAX_PROFILE_PHOTO_BYTES = 5 * 1024 * 1024
PROFILE_PHOTO_TYPES = {
    "image/jpeg": ("jpg", lambda content: content.startswith(b"\xff\xd8\xff")),
    "image/png": ("png", lambda content: content.startswith(b"\x89PNG\r\n\x1a\n")),
    "image/webp": (
        "webp",
        lambda content: len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP",
    ),
}


def manager_team_broker_ids(db: Session, manager_id: int):
    return [
        broker_id
        for (broker_id,) in (
            db.query(User.id)
            .filter(
                User.role == "BROKER",
                User.manager_id == manager_id,
                User.is_active.is_(True),
            )
            .all()
        )
    ]


def actor_label(actor: User | None):
    if not actor:
        return "Sistema"

    return actor.full_name or actor.username


def add_lead_event(db: Session, lead: Lead, actor: User | None, event_type: str, message: str):
    db.add(
        LeadEvent(
            lead_id=lead.id,
            actor_id=actor.id if actor else None,
            actor_name=actor_label(actor),
            event_type=event_type,
            message=message,
        )
    )


@router.post("/login", response_model=UserResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == payload.username).first()

    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Usuario ou senha invalidos")

    if not user.is_active or (user.status and user.status != "ACTIVE"):
        raise HTTPException(status_code=403, detail="Usuario inativo")

    user.last_seen_at = datetime.utcnow()
    db.commit()
    db.refresh(user)

    return user


@router.post("/me/heartbeat", response_model=UserResponse)
def heartbeat(
    user: User = Depends(get_actor),
    db: Session = Depends(get_db),
):
    user.last_seen_at = datetime.utcnow()
    db.commit()
    db.refresh(user)
    return user


@router.post("/me/logout")
def mark_logout(
    user: User = Depends(get_actor),
    db: Session = Depends(get_db),
):
    user.last_seen_at = None
    db.commit()

    return {"offline": True}


@router.get("/", response_model=list[UserResponse])
def list_users(
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_actor),
):
    query = db.query(User)
    if actor and actor.role == "GERENTE":
        query = query.filter(User.role == "BROKER", User.manager_id == actor.id)

    return query.order_by(User.id).all()


@router.get("/brokers/summary")
def broker_summary(
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_actor),
):
    query = db.query(User)

    if actor and actor.role == "GERENTE":
        query = query.filter(User.role == "BROKER", User.manager_id == actor.id)
    else:
        query = query.filter(User.role.in_(["GERENTE", "BROKER"]))

    brokers = query.order_by(User.role.desc(), User.id).all()

    summary = []
    for broker in brokers:
        pipeline_counts = (
            db.query(Lead.pipeline, func.count(Lead.id))
            .filter(Lead.assigned_to_user_id == broker.id)
            .group_by(Lead.pipeline)
            .all()
        )
        counts = {pipeline: count for pipeline, count in pipeline_counts}
        total = sum(counts.values())

        summary.append(
            {
                "id": broker.id,
                "manager_id": broker.manager_id,
                "username": broker.username,
                "email": broker.email,
                "company": broker.company,
                "full_name": broker.full_name,
                "role": broker.role,
                "creci": broker.creci,
                "data_nascimento": broker.data_nascimento,
                "telefone": broker.telefone,
                "email_pessoal": broker.email_pessoal,
                "documento": broker.documento,
                "observacoes": broker.observacoes,
                "pais_operacao": broker.pais_operacao,
                "estado_operacao": broker.estado_operacao,
                "cidade_operacao": broker.cidade_operacao,
                "idioma": broker.idioma,
                "profile_photo_url": broker.profile_photo_url,
                "last_seen_at": broker.last_seen_at,
                "is_online": broker.is_online,
                "is_active": broker.is_active,
                "status": broker.status,
                "plan": broker.plan,
                "plan_max_brokers": broker.plan_max_brokers,
                "plan_max_leads": broker.plan_max_leads,
                "total_leads": total,
                "pipeline_counts": counts,
            }
        )

    return summary


@router.post("/", response_model=UserResponse)
def create_user(
    payload: UserCreate,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_actor),
):
    role = payload.role.upper()

    if role not in {"ROOT", "GERENTE", "BROKER"}:
        raise HTTPException(status_code=400, detail="Role deve ser ROOT, GERENTE ou BROKER")

    if actor and actor.role == "GERENTE" and role != "BROKER":
        raise HTTPException(status_code=403, detail="Gerente pode cadastrar apenas broker")

    manager_id = payload.manager_id
    if role == "BROKER":
        if actor and actor.role == "GERENTE":
            manager_id = actor.id
        elif manager_id is not None:
            manager = (
                db.query(User)
                .filter(User.id == manager_id, User.role == "GERENTE", User.is_active.is_(True))
                .first()
            )
            if not manager:
                raise HTTPException(status_code=400, detail="Gerente responsavel nao encontrado")
    else:
        manager_id = None

    existing_user = db.query(User).filter(User.username == payload.username).first()
    if existing_user:
        raise HTTPException(status_code=409, detail="Usuario ja existe")

    user = User(
        manager_id=manager_id,
        username=payload.username,
        email=(payload.email or payload.email_pessoal or "").strip().lower() or None,
        company=(payload.company or "").strip() or None,
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
        role=role,
        creci=payload.creci,
        data_nascimento=payload.data_nascimento,
        telefone=payload.telefone,
        email_pessoal=payload.email_pessoal,
        documento=payload.documento,
        observacoes=payload.observacoes,
        pais_operacao=(payload.pais_operacao or "BR").upper(),
        estado_operacao=(payload.estado_operacao or "").strip(),
        cidade_operacao=(payload.cidade_operacao or "").strip(),
        idioma=(payload.idioma or "pt").lower(),
        email_verified=True,
        status="ACTIVE",
        plan=(payload.plan or "STARTER").upper(),
        plan_max_brokers=max(payload.plan_max_brokers or 0, 0),
        plan_max_leads=max(payload.plan_max_leads or 0, 0),
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def get_profile_photo_user(user_id: int, actor: User, db: Session):
    if actor.id != user_id and actor.role != "ROOT":
        raise HTTPException(status_code=403, detail="Usuario pode alterar apenas a propria foto")

    user = db.query(User).filter(User.id == user_id, User.is_active.is_(True)).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario nao encontrado")
    return user


@router.post("/{user_id}/profile-photo", response_model=UserResponse)
async def upload_profile_photo(
    user_id: int,
    photo: UploadFile = File(...),
    actor: User = Depends(get_actor),
    db: Session = Depends(get_db),
):
    user = get_profile_photo_user(user_id, actor, db)
    photo_type = PROFILE_PHOTO_TYPES.get((photo.content_type or "").lower())
    if not photo_type:
        raise HTTPException(status_code=400, detail="Use uma imagem JPG, PNG ou WebP")

    content = await photo.read(MAX_PROFILE_PHOTO_BYTES + 1)
    if len(content) > MAX_PROFILE_PHOTO_BYTES:
        raise HTTPException(status_code=413, detail="A foto deve ter no maximo 5 MB")
    if not content or not photo_type[1](content):
        raise HTTPException(status_code=400, detail="Arquivo de imagem invalido")

    previous_photo = user.profile_photo_url
    filename = f"{user.id}-{uuid4().hex}.{photo_type[0]}"
    photo_path = PROFILE_PHOTOS_DIR / filename
    photo_path.write_bytes(content)

    user.profile_photo_url = f"/uploads/profile_photos/{filename}"
    db.commit()
    db.refresh(user)
    delete_profile_photo(previous_photo)
    return user


@router.delete("/{user_id}/profile-photo", response_model=UserResponse)
def remove_profile_photo(
    user_id: int,
    actor: User = Depends(get_actor),
    db: Session = Depends(get_db),
):
    user = get_profile_photo_user(user_id, actor, db)
    previous_photo = user.profile_photo_url
    user.profile_photo_url = None
    db.commit()
    db.refresh(user)
    delete_profile_photo(previous_photo)
    return user


@router.patch("/{user_id}", response_model=UserResponse)
def update_user(
    user_id: int,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_actor),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario nao encontrado")

    updates = payload.model_dump(exclude_unset=True)

    if actor and actor.role == "GERENTE":
        blocked_fields = {"role", "is_active", "manager_id"}
        if blocked_fields.intersection(updates):
            raise HTTPException(status_code=403, detail="Gerente nao pode alterar role ou status")
        if user.role != "BROKER" or user.manager_id != actor.id:
            raise HTTPException(status_code=403, detail="Gerente pode editar apenas brokers")

    if "role" in updates and updates["role"]:
        role = updates["role"].upper()
        if role not in {"ROOT", "GERENTE", "BROKER"}:
            raise HTTPException(status_code=400, detail="Role deve ser ROOT, GERENTE ou BROKER")
        updates["role"] = role
        if role != "BROKER":
            updates["manager_id"] = None

    target_role = updates.get("role", user.role)
    if actor and actor.role == "ROOT" and "manager_id" in updates:
        if target_role != "BROKER":
            updates["manager_id"] = None
        elif updates["manager_id"] is not None:
            manager = (
                db.query(User)
                .filter(User.id == updates["manager_id"], User.role == "GERENTE", User.is_active.is_(True))
                .first()
            )
            if not manager:
                raise HTTPException(status_code=400, detail="Gerente responsavel nao encontrado")

    if "pais_operacao" in updates and updates["pais_operacao"]:
        updates["pais_operacao"] = updates["pais_operacao"].upper()

    if "estado_operacao" in updates and updates["estado_operacao"] is not None:
        updates["estado_operacao"] = updates["estado_operacao"].strip()

    if "cidade_operacao" in updates and updates["cidade_operacao"] is not None:
        updates["cidade_operacao"] = updates["cidade_operacao"].strip()

    if "idioma" in updates and updates["idioma"]:
        updates["idioma"] = updates["idioma"].lower()

    if "password" in updates:
        password = updates.pop("password")
        if password:
            user.password_hash = hash_password(password)

    if "username" in updates and updates["username"]:
        existing_user = (
            db.query(User)
            .filter(User.username == updates["username"], User.id != user.id)
            .first()
        )
        if existing_user:
            raise HTTPException(status_code=409, detail="Usuario ja existe")

    for field, value in updates.items():
        setattr(user, field, value)

    db.commit()
    db.refresh(user)
    return user


@router.patch("/{user_id}/profile", response_model=UserResponse)
def update_own_profile(
    user_id: int,
    payload: UserUpdate,
    actor: User = Depends(get_actor),
    db: Session = Depends(get_db),
):
    if actor.id != user_id:
        raise HTTPException(status_code=403, detail="Usuario pode editar apenas o proprio perfil")

    user = db.query(User).filter(User.id == user_id, User.is_active.is_(True)).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario nao encontrado")

    updates = payload.model_dump(exclude_unset=True)

    for blocked_field in ("role", "is_active", "username", "manager_id"):
        updates.pop(blocked_field, None)

    if "pais_operacao" in updates and updates["pais_operacao"]:
        updates["pais_operacao"] = updates["pais_operacao"].upper()

    if "estado_operacao" in updates and updates["estado_operacao"] is not None:
        updates["estado_operacao"] = updates["estado_operacao"].strip()

    if "cidade_operacao" in updates and updates["cidade_operacao"] is not None:
        updates["cidade_operacao"] = updates["cidade_operacao"].strip()

    if "idioma" in updates and updates["idioma"]:
        updates["idioma"] = updates["idioma"].lower()

    if "password" in updates:
        password = updates.pop("password")
        if password:
            user.password_hash = hash_password(password)

    for field, value in updates.items():
        setattr(user, field, value)

    db.commit()
    db.refresh(user)
    return user


@router.delete("/{user_id}")
def deactivate_user(
    user_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(require_root_actor),
):
    if actor.id == user_id:
        raise HTTPException(status_code=400, detail="Root nao pode excluir a propria conta")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario nao encontrado")

    user.is_active = False
    user.status = "SUSPENDED"
    user.last_seen_at = None
    db.commit()

    return {
        "deleted": True,
        "mode": "deactivated",
        "user_id": user_id,
    }


@router.post("/assign-leads")
def assign_leads(
    payload: AssignLeadsRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_actor),
):
    broker = (
        db.query(User)
        .filter(User.id == payload.broker_id, User.role == "BROKER", User.is_active.is_(True))
        .first()
    )

    if not broker:
        raise HTTPException(status_code=404, detail="Broker ativo nao encontrado")

    if actor and actor.role == "GERENTE" and broker.manager_id != actor.id:
        raise HTTPException(status_code=403, detail="Gerente pode enviar leads apenas para sua equipe")

    query = db.query(Lead).filter(Lead.assigned_to_user_id.is_(None))

    if payload.nicho:
        query = query.filter(Lead.nicho == payload.nicho.upper())

    if payload.pais:
        query = query.filter(Lead.pais == payload.pais.upper())

    if payload.estado:
        query = query.filter(Lead.estado == payload.estado)

    if payload.cidade:
        query = query.filter(Lead.cidade == payload.cidade)

    leads = query.order_by(Lead.score.desc().nullslast(), Lead.id).limit(payload.limit).all()

    for lead in leads:
        lead.assigned_to_user_id = broker.id
        lead.pipeline = "NOVO LEAD"
        lead.pipeline_updated_at = datetime.utcnow()
        lead.updated_at = datetime.utcnow()
        add_lead_event(
            db,
            lead,
            actor,
            "DISTRIBUICAO",
            f"Lead enviado para {broker.full_name or broker.username} em Aguardando Atendimento",
        )

    db.commit()

    return {
        "broker_id": broker.id,
        "broker": broker.username,
        "leads_enviados": len(leads),
    }


@router.post("/return-leads-to-bank")
def return_leads_to_bank(
    payload: ReturnLeadsRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_root_actor),
):
    if not payload.all and (payload.limit is None or payload.limit < 1):
        raise HTTPException(status_code=400, detail="Informe uma quantidade ou marque todos")

    query = db.query(Lead).filter(Lead.assigned_to_user_id.isnot(None))

    if payload.broker_id:
        query = query.filter(Lead.assigned_to_user_id == payload.broker_id)

    if payload.nicho:
        query = query.filter(Lead.nicho == payload.nicho.upper())

    if payload.pais:
        query = query.filter(Lead.pais == payload.pais.upper())

    if payload.estado:
        query = query.filter(Lead.estado == payload.estado)

    if payload.cidade:
        query = query.filter(Lead.cidade == payload.cidade)

    query = query.order_by(Lead.id)

    if not payload.all:
        query = query.limit(payload.limit)

    leads = query.all()

    for lead in leads:
        previous_broker_id = lead.assigned_to_user_id
        lead.assigned_to_user_id = None
        lead.pipeline = "NOVO LEAD"
        lead.pipeline_updated_at = datetime.utcnow()
        lead.updated_at = datetime.utcnow()
        add_lead_event(
            db,
            lead,
            actor,
            "BANCO",
            f"Lead retornou ao banco em lote vindo do broker {previous_broker_id}",
        )

    db.commit()

    return {
        "leads_devolvidos": len(leads),
        "broker_id": payload.broker_id,
        "todos": payload.all,
    }
