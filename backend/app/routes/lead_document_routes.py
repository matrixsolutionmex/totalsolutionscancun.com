import shutil
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_current_user as get_actor, get_db
from app.core.storage import UPLOADS_DIR
from app.models.deletion_request import DeletionRequest
from app.models.lead import Lead
from app.models.lead_document import LeadDocument
from app.models.lead_event import LeadEvent
from app.models.service_order import ServiceOrder
from app.models.user import User
from app.routes.lead_routes import ensure_lead_visible_to_actor, actor_label

router = APIRouter(prefix="/leads", tags=["lead-documents"])

ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".xlsx", ".doc", ".docx", ".mp4", ".mov", ".webm"}
ALLOWED_DOCUMENT_TYPES = {
    "ANTES_SERVICIO",
    "DURANTE_SERVICIO",
    "DESPUES_SERVICIO",
    "PRESUPUESTO",
    "NOTA_FISCAL",
    "GARANTIA",
    "VIDEO",
    "OTROS",
}
MAX_UPLOAD_SIZE = 50 * 1024 * 1024


def ensure_deletion_reviewer(actor: User):
    if actor.role not in {"ROOT", "GERENTE"}:
        raise HTTPException(status_code=403, detail="Somente administrador ou supervisor pode revisar exclusoes")


def document_file_path(doc: LeadDocument) -> Path:
    return UPLOADS_DIR.parent / doc.file_path.lstrip("/")


def deletion_request_payload(
    request: DeletionRequest,
    lead: Lead | None,
    document: LeadDocument | None,
    requester: User | None,
    reviewer: User | None = None,
    order_number: str | None = None,
):
    return {
        "id": request.id,
        "lead_id": request.lead_id,
        "document_id": request.document_id,
        "target_type": request.target_type,
        "reason": request.reason,
        "status": request.status,
        "requested_by_user_id": request.requested_by_user_id,
        "requested_by_name": actor_label(requester) if requester else None,
        "requested_by_role": request.requested_by_role,
        "reviewed_by_user_id": request.reviewed_by_user_id,
        "reviewed_by_name": actor_label(reviewer) if reviewer else None,
        "decision_reason": request.decision_reason,
        "created_at": request.created_at,
        "reviewed_at": request.reviewed_at,
        "client_name": lead.nome if lead else None,
        "service_order": order_number,
        "document_name": document.file_name if document else None,
        "document_type": document.document_type if document else None,
    }


@router.get("/deletion-requests")
def list_deletion_requests(
    status: str | None = "PENDENTE",
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    ensure_deletion_reviewer(actor)

    query = db.query(DeletionRequest).order_by(DeletionRequest.created_at.desc(), DeletionRequest.id.desc())
    if actor.organization_id:
        query = query.filter(DeletionRequest.organization_id == actor.organization_id)
    if status:
        query = query.filter(DeletionRequest.status == status)

    requests = query.all()
    if not requests:
        return []

    lead_ids = {request.lead_id for request in requests}
    document_ids = {request.document_id for request in requests if request.document_id}
    user_ids = {
        user_id
        for request in requests
        for user_id in (request.requested_by_user_id, request.reviewed_by_user_id)
        if user_id
    }

    leads = {lead.id: lead for lead in db.query(Lead).filter(Lead.id.in_(lead_ids)).all()}
    documents = {
        doc.id: doc
        for doc in (
            db.query(LeadDocument)
            .filter(LeadDocument.id.in_(document_ids), LeadDocument.organization_id == actor.organization_id)
            .all()
        )
    } if document_ids else {}
    users = {
        user.id: user
        for user in db.query(User).filter(User.id.in_(user_ids), User.organization_id == actor.organization_id).all()
    } if user_ids else {}
    orders = {
        order.lead_id: order.order_number
        for order in (
            db.query(ServiceOrder)
            .filter(ServiceOrder.lead_id.in_(lead_ids), ServiceOrder.organization_id == actor.organization_id)
            .all()
        )
    }

    payloads = []
    for request in requests:
        lead = leads.get(request.lead_id)
        if not lead:
            continue
        try:
            ensure_lead_visible_to_actor(db, lead, actor)
        except HTTPException:
            continue

        payloads.append(
            deletion_request_payload(
                request,
                lead,
                documents.get(request.document_id),
                users.get(request.requested_by_user_id),
                users.get(request.reviewed_by_user_id),
                orders.get(request.lead_id),
            )
        )

    return payloads


@router.patch("/deletion-requests/{request_id}")
def decide_deletion_request(
    request_id: int,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    ensure_deletion_reviewer(actor)

    decision = str(payload.get("status") or "").strip().upper()
    if decision not in {"APROVADA", "REJEITADA"}:
        raise HTTPException(status_code=400, detail="Status de revisao invalido")

    request = db.query(DeletionRequest).filter(DeletionRequest.id == request_id).first()
    if not request:
        raise HTTPException(status_code=404, detail="Solicitacao nao encontrada")
    if actor.organization_id and request.organization_id != actor.organization_id:
        raise HTTPException(status_code=403, detail="Solicitacao fora da sua organizacao")

    if request.status != "PENDENTE":
        raise HTTPException(status_code=409, detail="Solicitacao ja revisada")

    lead = db.query(Lead).filter(Lead.id == request.lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Cliente nao encontrado")

    ensure_lead_visible_to_actor(db, lead, actor)

    doc = None
    document_label = "documento"
    if request.document_id:
        doc = (
            db.query(LeadDocument)
            .filter(
                LeadDocument.id == request.document_id,
                LeadDocument.lead_id == request.lead_id,
                LeadDocument.organization_id == lead.organization_id,
            )
            .first()
        )
        if doc:
            document_label = f"{doc.document_type} - {doc.file_name}"

    request.status = decision
    request.reviewed_by_user_id = actor.id
    request.reviewed_at = datetime.utcnow()
    request.decision_reason = str(payload.get("decision_reason") or "").strip() or None

    if decision == "APROVADA":
        if not doc:
            raise HTTPException(status_code=404, detail="Documento nao encontrado para exclusao")

        file_path = document_file_path(doc)
        if file_path.exists():
            file_path.unlink()

        request.document_id = None
        db.delete(doc)
        event_type = "EXCLUSAO_APROVADA"
        message = f"Aprovou exclusao de documento: {document_label}"
    else:
        event_type = "EXCLUSAO_REJEITADA"
        message = f"Rejeitou exclusao de documento: {document_label}"

    db.add(
        LeadEvent(
            organization_id=lead.organization_id or actor.organization_id,
            lead_id=request.lead_id,
            actor_id=actor.id,
            actor_name=actor_label(actor),
            event_type=event_type,
            message=message,
        )
    )
    db.commit()
    db.refresh(request)

    return deletion_request_payload(request, lead, None if decision == "APROVADA" else doc, None, actor)


@router.get("/{lead_id}/documents")
def list_lead_documents(
    lead_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")

    ensure_lead_visible_to_actor(db, lead, actor)

    documents = (
        db.query(LeadDocument)
        .filter(LeadDocument.lead_id == lead_id, LeadDocument.organization_id == lead.organization_id)
        .order_by(LeadDocument.created_at.desc(), LeadDocument.id.desc())
        .all()
    )
    uploader_ids = [doc.uploaded_by_user_id for doc in documents if doc.uploaded_by_user_id]
    uploaders = {
        user.id: actor_label(user)
        for user in db.query(User).filter(User.id.in_(uploader_ids), User.organization_id == actor.organization_id).all()
    } if uploader_ids else {}

    return [
        {
            "id": doc.id,
            "lead_id": doc.lead_id,
            "uploaded_by_user_id": doc.uploaded_by_user_id,
            "uploaded_by_user_name": uploaders.get(doc.uploaded_by_user_id),
            "document_type": doc.document_type,
            "file_name": doc.file_name,
            "file_path": doc.file_path,
            "file_mime": doc.file_mime,
            "file_size": doc.file_size,
            "created_at": doc.created_at,
        }
        for doc in documents
    ]


@router.post("/{lead_id}/documents")
def upload_lead_document(
    lead_id: int,
    document_type: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")

    ensure_lead_visible_to_actor(db, lead, actor)

    if document_type not in ALLOWED_DOCUMENT_TYPES:
        raise HTTPException(status_code=400, detail="Categoria de documento invalida")

    original_name = file.filename or "arquivo"
    ext = Path(original_name).suffix.lower()

    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Formato não permitido")

    folder = UPLOADS_DIR / "lead_documents" / str(lead_id)
    folder.mkdir(parents=True, exist_ok=True)

    safe_name = f"{uuid4().hex}{ext}"
    destination = folder / safe_name

    with destination.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    file_size = destination.stat().st_size
    if file_size > MAX_UPLOAD_SIZE:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=413, detail="Arquivo excede o tamanho maximo permitido")

    public_path = f"/uploads/lead_documents/{lead_id}/{safe_name}"

    doc = LeadDocument(
        organization_id=lead.organization_id or actor.organization_id,
        lead_id=lead_id,
        uploaded_by_user_id=actor.id,
        document_type=document_type,
        file_name=original_name,
        file_path=public_path,
        file_mime=file.content_type,
        file_size=file_size,
    )

    db.add(doc)
    db.add(
        LeadEvent(
            organization_id=lead.organization_id or actor.organization_id,
            lead_id=lead_id,
            actor_id=actor.id,
            actor_name=actor_label(actor),
            event_type="DOCUMENTO",
            message=f"Anexou documento: {document_type} - {original_name}",
        )
    )
    db.commit()
    db.refresh(doc)

    return {
        "id": doc.id,
        "lead_id": doc.lead_id,
        "uploaded_by_user_id": doc.uploaded_by_user_id,
        "uploaded_by_user_name": actor_label(actor),
        "document_type": doc.document_type,
        "file_name": doc.file_name,
        "file_path": doc.file_path,
        "file_mime": doc.file_mime,
        "file_size": doc.file_size,
        "created_at": doc.created_at,
    }


@router.delete("/{lead_id}/documents/{document_id}")
def delete_lead_document(
    lead_id: int,
    document_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")

    ensure_lead_visible_to_actor(db, lead, actor)
    if actor.role == "BROKER":
        raise HTTPException(status_code=403, detail="Tecnico deve solicitar exclusao de documentos")

    doc = (
        db.query(LeadDocument)
        .filter(LeadDocument.id == document_id, LeadDocument.lead_id == lead_id, LeadDocument.organization_id == lead.organization_id)
        .first()
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Documento não encontrado")

    file_path = document_file_path(doc)
    if file_path.exists():
        file_path.unlink()

    db.add(
        LeadEvent(
            organization_id=lead.organization_id or actor.organization_id,
            lead_id=lead_id,
            actor_id=actor.id,
            actor_name=actor_label(actor),
            event_type="DOCUMENTO",
            message=f"Excluiu documento: {doc.document_type} - {doc.file_name}",
        )
    )

    db.delete(doc)
    db.commit()

    return {"ok": True}


@router.post("/{lead_id}/documents/{document_id}/deletion-request")
def request_lead_document_deletion(
    lead_id: int,
    document_id: int,
    reason: str = Form(...),
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    clean_reason = reason.strip()
    if not clean_reason:
        raise HTTPException(status_code=400, detail="Motivo obrigatorio")

    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")

    ensure_lead_visible_to_actor(db, lead, actor)

    doc = (
        db.query(LeadDocument)
        .filter(LeadDocument.id == document_id, LeadDocument.lead_id == lead_id, LeadDocument.organization_id == lead.organization_id)
        .first()
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Documento não encontrado")

    pending_request = (
        db.query(DeletionRequest)
        .filter(
            DeletionRequest.organization_id == lead.organization_id,
            DeletionRequest.document_id == document_id,
            DeletionRequest.lead_id == lead_id,
            DeletionRequest.status == "PENDENTE",
        )
        .first()
    )
    if pending_request:
        raise HTTPException(status_code=409, detail="Ja existe solicitacao pendente para este documento")

    request = DeletionRequest(
        organization_id=lead.organization_id or actor.organization_id,
        lead_id=lead_id,
        document_id=document_id,
        target_type="DOCUMENT",
        reason=clean_reason,
        status="PENDENTE",
        requested_by_user_id=actor.id,
        requested_by_role=actor.role,
    )
    db.add(request)
    db.add(
        LeadEvent(
            organization_id=lead.organization_id or actor.organization_id,
            lead_id=lead_id,
            actor_id=actor.id,
            actor_name=actor_label(actor),
            event_type="SOLICITACAO_EXCLUSAO",
            message=f"Solicitou exclusao de documento: {doc.document_type} - {doc.file_name}",
        )
    )
    db.commit()
    db.refresh(request)

    return {
        "id": request.id,
        "lead_id": request.lead_id,
        "document_id": request.document_id,
        "target_type": request.target_type,
        "status": request.status,
        "requested_by_user_id": request.requested_by_user_id,
        "requested_by_role": request.requested_by_role,
        "created_at": request.created_at,
    }
