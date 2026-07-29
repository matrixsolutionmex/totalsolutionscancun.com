import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_current_user as get_actor, get_db
from app.core.storage import UPLOADS_DIR
from app.models.lead import Lead
from app.models.lead_document import LeadDocument
from app.models.lead_event import LeadEvent
from app.models.user import User
from app.routes.lead_routes import ensure_lead_visible_to_actor, actor_label

router = APIRouter(prefix="/leads", tags=["lead-documents"])

ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".xlsx", ".doc", ".docx", ".mp4", ".mov", ".webm"}


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

    public_path = f"/uploads/lead_documents/{lead_id}/{safe_name}"

    doc = LeadDocument(
        lead_id=lead_id,
        uploaded_by_user_id=actor.id,
        document_type=document_type,
        file_name=original_name,
        file_path=public_path,
        file_mime=file.content_type,
        file_size=destination.stat().st_size,
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
