from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.auth.jwt_handler import (
    get_current_user as get_actor,
    get_db,
    require_admin_user as require_admin_actor,
)
from app.models.lead import Lead
from app.models.lead_document import LeadDocument
from app.models.lead_event import LeadEvent
from app.models.user import User
from app.schemas.lead_schema import (
    LeadAssignUpdate,
    LeadCreate,
    LeadEnrichBatchRequest,
    LeadEnrichBatchResponse,
    LeadEventCreate,
    LeadEventResponse,
    LeadPipelineUpdate,
    LeadResponse,
    LeadUpdate,
)
from app.services.enrichment_service import EnrichmentError, enrich_lead_record, enrich_leads_in_background
from app.services.dossier_pdf_service import build_service_dossier_pdf
from app.services.lead_entry_service import property_extra_json, validate_responsible
from app.services.lead_creation_service import create_lead_record
from app.services.service_order_service import ensure_service_order, sync_service_order_from_lead
from app.services.notification_service import dispatch_web_push_for_notification_ids, notify_assignment_change

router = APIRouter(prefix="/leads", tags=["leads"])

PIPELINE_STAGES = [
    "NOVO LEAD",
    "ATENDIMENTO",
    "TENTATIVA DE CONTATO",
    "VISITA",
    "MONTAGEM DE PASTA",
    "VENDA GANHA",
    "PERDIDO",
]


def broker_ids_for_manager(db: Session, manager_id: int):
    manager = db.query(User).filter(User.id == manager_id).first()
    return [
        broker_id
        for (broker_id,) in (
            db.query(User.id)
            .filter(
                User.role == "BROKER",
                User.manager_id == manager_id,
                User.is_active.is_(True),
                User.organization_id == (manager.organization_id if manager else None),
            )
            .all()
        )
    ]


def apply_actor_scope(query, db: Session, actor: User | None):
    if actor and actor.organization_id:
        query = query.filter(Lead.organization_id == actor.organization_id)

    if not actor or actor.role == "ROOT":
        return query

    if actor.role == "BROKER":
        return query.filter(Lead.assigned_to_user_id == actor.id)

    if actor.role == "GERENTE":
        team_ids = broker_ids_for_manager(db, actor.id)
        return query.filter(
            or_(
                Lead.assigned_to_user_id == actor.id,
                Lead.assigned_to_user_id.in_(team_ids),
                Lead.assigned_to_user_id.is_(None),
            )
        )

    return query.filter(False)


def ensure_lead_visible_to_actor(db: Session, lead: Lead, actor: User | None):
    if actor and lead.organization_id and actor.organization_id != lead.organization_id:
        raise HTTPException(status_code=403, detail="Registro fora da sua organizacao")

    if not actor or actor.role == "ROOT":
        return

    if actor.role == "BROKER" and lead.assigned_to_user_id == actor.id:
        return

    if actor.role == "GERENTE" and (
        lead.assigned_to_user_id is None
        or lead.assigned_to_user_id == actor.id
        or lead.assigned_to_user_id in broker_ids_for_manager(db, actor.id)
    ):
        return

    raise HTTPException(status_code=403, detail="Lead fora da sua estrutura")


def actor_label(actor: User | None):
    if not actor:
        return "Sistema"

    return actor.full_name or actor.username


def add_lead_event(db: Session, lead: Lead, actor: User | None, event_type: str, message: str):
    db.add(
        LeadEvent(
            organization_id=lead.organization_id or (actor.organization_id if actor else None),
            lead_id=lead.id,
            actor_id=actor.id if actor else None,
            actor_name=actor_label(actor),
            event_type=event_type,
            message=message,
        )
    )


@router.get("/", response_model=list[LeadResponse])
def list_leads(
    db: Session = Depends(get_db),
    actor: User | None = Depends(get_actor),
    search: str | None = None,
    pipeline: str | None = None,
    assigned_to_user_id: int | None = None,
    unassigned: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    query = apply_actor_scope(db.query(Lead), db, actor)

    if search:
        term = f"%{search}%"
        query = query.filter(
            or_(
                Lead.nome.ilike(term),
                Lead.email.ilike(term),
                Lead.contato.ilike(term),
                Lead.site.ilike(term),
                Lead.nicho.ilike(term),
                Lead.pais.ilike(term),
                Lead.estado.ilike(term),
                Lead.cidade.ilike(term),
            )
        )

    if pipeline:
        query = query.filter(Lead.pipeline == pipeline)

    if assigned_to_user_id is not None:
        query = query.filter(Lead.assigned_to_user_id == assigned_to_user_id)

    if unassigned:
        query = query.filter(Lead.assigned_to_user_id.is_(None))

    return (
        query.order_by(Lead.score.desc().nullslast(), Lead.id)
        .offset(offset)
        .limit(limit)
        .all()
    )


@router.get("/kanban")
def kanban_leads(
    db: Session = Depends(get_db),
    actor: User | None = Depends(get_actor),
    assigned_to_user_id: int | None = None,
    unassigned: bool = False,
    limit_per_stage: int = Query(default=25, ge=1, le=100),
):
    board = {}

    for stage in PIPELINE_STAGES:
        query = apply_actor_scope(db.query(Lead).filter(Lead.pipeline == stage), db, actor)

        if assigned_to_user_id is not None:
            query = query.filter(Lead.assigned_to_user_id == assigned_to_user_id)

        if unassigned:
            query = query.filter(Lead.assigned_to_user_id.is_(None))

        leads = (
            query.order_by(Lead.score.desc().nullslast(), Lead.id)
            .limit(limit_per_stage)
            .all()
        )
        board[stage] = [LeadResponse.model_validate(lead).model_dump() for lead in leads]

    return {
        "stages": PIPELINE_STAGES,
        "board": board,
    }


@router.post("/", response_model=LeadResponse, status_code=201)
def create_lead(
    payload: LeadCreate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    lead, pending_push_notification_ids = create_lead_record(db, payload, actor)
    db.commit()
    dispatch_web_push_for_notification_ids(db, pending_push_notification_ids)
    db.refresh(lead)
    return lead


@router.get("/inventory")
def lead_inventory(
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_actor),
):
    free_query = apply_actor_scope(db.query(Lead), db, actor).filter(Lead.assigned_to_user_id.is_(None))
    total_free = free_query.count()

    nichos = (
        apply_actor_scope(db.query(Lead.nicho, func.count(Lead.id)), db, actor)
        .filter(Lead.assigned_to_user_id.is_(None), Lead.nicho.isnot(None), Lead.nicho != "")
        .group_by(Lead.nicho)
        .order_by(func.count(Lead.id).desc(), Lead.nicho)
        .all()
    )
    paises = (
        apply_actor_scope(db.query(Lead.pais, func.count(Lead.id)), db, actor)
        .filter(Lead.assigned_to_user_id.is_(None), Lead.pais.isnot(None), Lead.pais != "")
        .group_by(Lead.pais)
        .order_by(func.count(Lead.id).desc(), Lead.pais)
        .all()
    )
    estados = (
        apply_actor_scope(db.query(Lead.estado, func.count(Lead.id)), db, actor)
        .filter(Lead.assigned_to_user_id.is_(None), Lead.estado.isnot(None), Lead.estado != "")
        .group_by(Lead.estado)
        .order_by(func.count(Lead.id).desc(), Lead.estado)
        .all()
    )
    cidades = (
        apply_actor_scope(db.query(Lead.cidade, func.count(Lead.id)), db, actor)
        .filter(Lead.assigned_to_user_id.is_(None), Lead.cidade.isnot(None), Lead.cidade != "")
        .group_by(Lead.cidade)
        .order_by(func.count(Lead.id).desc(), Lead.cidade)
        .all()
    )
    combinacoes = (
        apply_actor_scope(db.query(Lead.nicho, Lead.pais, Lead.estado, Lead.cidade, func.count(Lead.id)), db, actor)
        .filter(Lead.assigned_to_user_id.is_(None))
        .group_by(Lead.nicho, Lead.pais, Lead.estado, Lead.cidade)
        .all()
    )

    return {
        "total_livre": total_free,
        "nichos": [{"nome": nicho, "total": total} for nicho, total in nichos],
        "paises": [{"nome": pais, "total": total} for pais, total in paises],
        "estados": [{"nome": estado, "total": total} for estado, total in estados],
        "cidades": [{"nome": cidade, "total": total} for cidade, total in cidades],
        "combinacoes": [
            {
                "nicho": nicho,
                "pais": pais,
                "estado": estado,
                "cidade": cidade,
                "total": total,
            }
            for nicho, pais, estado, cidade, total in combinacoes
        ],
    }


@router.post("/enrich-batch", response_model=LeadEnrichBatchResponse, status_code=202)
def enrich_lead_batch(
    payload: LeadEnrichBatchRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    query = apply_actor_scope(db.query(Lead.id), db, actor)

    if payload.ids is not None:
        query = query.filter(Lead.id.in_(payload.ids))
        limit = len(payload.ids)
    else:
        if payload.filter and payload.filter.nicho:
            query = query.filter(Lead.nicho.ilike(payload.filter.nicho.strip()))
        if payload.filter and payload.filter.pais:
            query = query.filter(Lead.pais.ilike(payload.filter.pais.strip()))
        limit = payload.limit

    lead_ids = [lead_id for (lead_id,) in query.order_by(Lead.id).limit(limit).all()]
    if not lead_ids:
        raise HTTPException(status_code=404, detail="Nenhum lead acessivel encontrado para enriquecimento")

    background_tasks.add_task(
        enrich_leads_in_background,
        lead_ids,
        actor.id,
        actor_label(actor),
    )
    return LeadEnrichBatchResponse(status="scheduled", scheduled=len(lead_ids))


@router.post("/{lead_id}/enrich", response_model=LeadResponse)
def enrich_lead(
    lead_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead nao encontrado")

    ensure_lead_visible_to_actor(db, lead, actor)
    service_order = ensure_service_order(db, lead, actor=actor)

    try:
        enrich_lead_record(
            db,
            lead,
            actor_id=actor.id,
            actor_name=actor_label(actor),
        )
    except EnrichmentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return lead


@router.get("/{lead_id}/events", response_model=list[LeadEventResponse])
def list_lead_events(
    lead_id: int,
    db: Session = Depends(get_db),
    actor: User | None = Depends(get_actor),
):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead nao encontrado")

    ensure_lead_visible_to_actor(db, lead, actor)

    return (
        db.query(LeadEvent)
        .filter(LeadEvent.lead_id == lead.id, LeadEvent.organization_id == lead.organization_id)
        .order_by(LeadEvent.created_at.desc(), LeadEvent.id.desc())
        .limit(100)
        .all()
    )


@router.get("/{lead_id}/dossier.pdf")
def service_dossier_pdf(
    lead_id: int,
    request: Request,
    db: Session = Depends(get_db),
    actor: User | None = Depends(get_actor),
):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead nao encontrado")

    ensure_lead_visible_to_actor(db, lead, actor)
    service_order = ensure_service_order(db, lead, actor=actor)

    responsible = (
        db.query(User).filter(User.id == lead.assigned_to_user_id).first()
        if lead.assigned_to_user_id
        else None
    )
    supervisor = None
    if responsible and responsible.manager_id:
        supervisor = db.query(User).filter(User.id == responsible.manager_id).first()
    elif responsible and responsible.role == "GERENTE":
        supervisor = responsible
    elif actor and actor.role == "GERENTE":
        supervisor = actor
    administrator = actor if actor and actor.role == "ROOT" else None

    events = (
        db.query(LeadEvent)
        .filter(LeadEvent.lead_id == lead.id, LeadEvent.organization_id == lead.organization_id)
        .order_by(LeadEvent.created_at.asc(), LeadEvent.id.asc())
        .all()
    )
    documents = (
        db.query(LeadDocument)
        .filter(LeadDocument.lead_id == lead.id, LeadDocument.organization_id == lead.organization_id)
        .order_by(LeadDocument.document_type, LeadDocument.created_at.asc(), LeadDocument.id.asc())
        .all()
    )
    uploader_ids = [doc.uploaded_by_user_id for doc in documents if doc.uploaded_by_user_id]
    uploaders = {
        user.id: actor_label(user)
        for user in db.query(User).filter(User.id.in_(uploader_ids)).all()
    } if uploader_ids else {}

    pdf = build_service_dossier_pdf(
        lead=lead,
        events=events,
        documents=documents,
        responsible=responsible,
        supervisor=supervisor,
        administrator=administrator,
        uploaders=uploaders,
        service_order=service_order,
        public_base_url=str(request.base_url).rstrip("/"),
    )
    filename = f"dossier-servicio-{service_order.order_number or lead.property_id or lead.id}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{lead_id}/events", response_model=LeadEventResponse)
def create_lead_note(
    lead_id: int,
    payload: LeadEventCreate,
    db: Session = Depends(get_db),
    actor: User | None = Depends(get_actor),
):
    note = payload.message.strip()
    if not note:
        raise HTTPException(status_code=400, detail="Nota vazia")

    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead nao encontrado")

    ensure_lead_visible_to_actor(db, lead, actor)

    event = LeadEvent(
        organization_id=lead.organization_id or (actor.organization_id if actor else None),
        lead_id=lead.id,
        actor_id=actor.id if actor else None,
        actor_name=actor_label(actor),
        event_type="NOTA",
        message=note,
    )
    db.add(event)
    lead.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(event)
    return event


@router.patch("/{lead_id}/pipeline", response_model=LeadResponse)
def update_lead_pipeline(
    lead_id: int,
    payload: LeadPipelineUpdate,
    db: Session = Depends(get_db),
    actor: User | None = Depends(get_actor),
):
    stage = payload.pipeline.upper()

    if stage not in PIPELINE_STAGES:
        raise HTTPException(status_code=400, detail="Etapa de pipeline invalida")

    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead nao encontrado")

    ensure_lead_visible_to_actor(db, lead, actor)
    if lead.pipeline != stage:
        previous_stage = lead.pipeline or "SEM ETAPA"
        lead.pipeline_updated_at = datetime.utcnow()
        add_lead_event(
            db,
            lead,
            actor,
            "PIPELINE",
            f"Moveu de {previous_stage} para {stage}",
        )
    lead.pipeline = stage
    lead.updated_at = datetime.utcnow()
    service_order = ensure_service_order(db, lead, actor=actor)
    sync_service_order_from_lead(db, service_order, lead, actor=actor)
    db.commit()
    db.refresh(lead)
    return lead


@router.patch("/{lead_id}/assign", response_model=LeadResponse)
def assign_lead(
    lead_id: int,
    payload: LeadAssignUpdate,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_actor),
):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead nao encontrado")

    ensure_lead_visible_to_actor(db, lead, actor)
    validate_responsible(db, actor, payload.assigned_to_user_id)

    previous_broker_id = lead.assigned_to_user_id
    lead.assigned_to_user_id = payload.assigned_to_user_id
    lead.updated_at = datetime.utcnow()
    service_order = ensure_service_order(db, lead, actor=actor)
    sync_service_order_from_lead(db, service_order, lead, actor=actor)
    add_lead_event(
        db,
        lead,
        actor,
        "ATRIBUICAO",
        f"Responsavel alterado de {previous_broker_id or 'sin asignar'} para {payload.assigned_to_user_id or 'sin asignar'}",
    )
    pending_push_notification_ids = notify_assignment_change(
        db,
        lead=lead,
        actor=actor,
        previous_user_id=previous_broker_id,
        new_user_id=payload.assigned_to_user_id,
    )
    db.commit()
    dispatch_web_push_for_notification_ids(db, pending_push_notification_ids)
    db.refresh(lead)
    return lead


@router.patch("/{lead_id}/return-to-bank", response_model=LeadResponse)
def return_lead_to_bank(
    lead_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_actor),
):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead nao encontrado")

    ensure_lead_visible_to_actor(db, lead, actor)
    previous_broker_id = lead.assigned_to_user_id
    lead.assigned_to_user_id = None
    lead.pipeline = "NOVO LEAD"
    lead.pipeline_updated_at = datetime.utcnow()
    lead.updated_at = datetime.utcnow()
    service_order = ensure_service_order(db, lead, actor=actor)
    sync_service_order_from_lead(db, service_order, lead, actor=actor)
    add_lead_event(db, lead, actor, "BANCO", "Lead voltou para o banco")
    pending_push_notification_ids = notify_assignment_change(
        db,
        lead=lead,
        actor=actor,
        previous_user_id=previous_broker_id,
        new_user_id=None,
    )
    db.commit()
    dispatch_web_push_for_notification_ids(db, pending_push_notification_ids)
    db.refresh(lead)
    return lead


@router.delete("/{lead_id}")
def delete_lead(
    lead_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_actor),
):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead nao encontrado")

    ensure_lead_visible_to_actor(db, lead, actor)
    add_lead_event(db, lead, actor, "EXCLUSAO", "Lead excluido definitivamente")
    db.delete(lead)
    db.commit()
    return {"deleted": True, "lead_id": lead_id}


@router.patch("/{lead_id}", response_model=LeadResponse)
def update_lead(
    lead_id: int,
    payload: LeadUpdate,
    db: Session = Depends(get_db),
    actor: User | None = Depends(get_actor),
):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead nao encontrado")

    ensure_lead_visible_to_actor(db, lead, actor)
    updates = payload.model_dump(exclude_unset=True)
    if "property_extra" in updates:
        updates["property_extra_json"] = property_extra_json(payload)
        updates.pop("property_extra", None)

    if "pipeline" in updates and updates["pipeline"]:
        stage = updates["pipeline"].upper()
        if stage not in PIPELINE_STAGES:
            raise HTTPException(status_code=400, detail="Etapa de pipeline invalida")
        if lead.pipeline != stage:
            previous_stage = lead.pipeline or "SEM ETAPA"
            lead.pipeline_updated_at = datetime.utcnow()
            add_lead_event(
                db,
                lead,
                actor,
                "PIPELINE",
                f"Moveu de {previous_stage} para {stage}",
            )
        updates["pipeline"] = stage

    assignment_previous_id = None
    assignment_new_id = None
    if "assigned_to_user_id" in updates:
        requested_responsible_id = updates["assigned_to_user_id"]
        current_responsible_id = lead.assigned_to_user_id
        if requested_responsible_id == current_responsible_id:
            updates.pop("assigned_to_user_id", None)
        else:
            if actor and actor.role == "BROKER":
                raise HTTPException(status_code=403, detail="Tecnico nao pode redistribuir clientes")
            validate_responsible(db, actor, requested_responsible_id)
            assignment_previous_id = current_responsible_id
            assignment_new_id = requested_responsible_id

    tracked_fields = {
        "nome",
        "contato",
        "email",
        "site",
        "instagram",
        "linkedin",
        "facebook",
        "redes_sociais",
        "nicho",
        "pais",
        "score",
        "valor_negocio",
        "endereco",
        "observacoes",
        "property_id",
        "tipo_imovel",
        "tipo_servico",
        "empresa",
        "pessoa_contato",
        "latitude",
        "longitude",
        "foto_fachada_url",
        "property_extra_json",
        "whatsapp",
        "colonia",
        "codigo_postal",
        "google_maps_url",
        "descripcion_problema",
        "urgencia",
        "origen",
        "origen_detalle",
        "proximo_contacto",
    }
    changed_fields = [
        field
        for field, value in updates.items()
        if field in tracked_fields and str(getattr(lead, field, "") or "") != str(value or "")
    ]

    for field, value in updates.items():
        setattr(lead, field, value)

    lead.updated_at = datetime.utcnow()
    service_order = ensure_service_order(db, lead, actor=actor)
    sync_service_order_from_lead(db, service_order, lead, actor=actor)
    if changed_fields:
        add_lead_event(
            db,
            lead,
            actor,
            "EDICAO",
            f"Editou dados do lead: {', '.join(changed_fields)}",
        )
    if assignment_previous_id != assignment_new_id:
        add_lead_event(
            db,
            lead,
            actor,
            "ATRIBUICAO",
            f"Responsavel alterado de {assignment_previous_id or 'sin asignar'} para {assignment_new_id or 'sin asignar'}",
        )
        pending_push_notification_ids = notify_assignment_change(
            db,
            lead=lead,
            actor=actor,
            previous_user_id=assignment_previous_id,
            new_user_id=assignment_new_id,
        )
    db.commit()
    if assignment_previous_id != assignment_new_id:
        dispatch_web_push_for_notification_ids(db, pending_push_notification_ids)
    db.refresh(lead)
    return lead
