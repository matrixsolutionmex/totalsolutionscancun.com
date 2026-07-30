import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_current_user as get_actor, get_db
from app.core.storage import UPLOADS_DIR
from app.models.deletion_request import DeletionRequest
from app.models.lead import Lead
from app.models.lead_document import LeadDocument
from app.models.lead_event import LeadEvent
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
        .filter(LeadDocument.lead_id == lead_id)
        .order_by(LeadDocument.created_at.desc(), LeadDocument.id.desc())
        .all()
    )
    uploader_ids = [doc.uploaded_by_user_id for doc in documents if doc.uploaded_by_user_id]
    uploaders = {
        user.id: actor_label(user)
        for user in db.query(User).filter(User.id.in_(uploader_ids)).all()
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
        .filter(LeadDocument.id == document_id, LeadDocument.lead_id == lead_id)
        .first()
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Documento não encontrado")

    file_path = UPLOADS_DIR.parent / doc.file_path.lstrip("/")
    if file_path.exists():
        file_path.unlink()

    db.add(
        LeadEvent(
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
        .filter(LeadDocument.id == document_id, LeadDocument.lead_id == lead_id)
        .first()
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Documento não encontrado")

    request = DeletionRequest(
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
