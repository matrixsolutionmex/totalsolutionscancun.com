import json
import os
import re
import secrets
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_db
from app.core.auth_security import apply_public_rate_limits, hash_value
from app.core.storage import UPLOADS_DIR
from app.models.notification import EmailOutbox
from app.models.professional_application import ProfessionalApplication


router = APIRouter(prefix="/public", tags=["professional-applications"])
APPLICATION_UPLOAD_DIR = UPLOADS_DIR / "professional-applications"
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_FILE_BYTES = 25 * 1024 * 1024
MAX_FILES = 8
ALLOWED_FILE_TYPES = {
    "image/jpeg": {".jpg", ".jpeg"},
    "image/png": {".png"},
    "image/webp": {".webp"},
    "application/pdf": {".pdf"},
    "video/mp4": {".mp4"},
}
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PROFESSIONAL_APPLICATION_EMAIL = os.getenv("PROFESSIONAL_APPLICATION_EMAIL", "inscripciones@totalsolutionscancun.com").strip()


def _clean(value: str | None, *, field: str, maximum: int, required: bool = True) -> str:
    cleaned = " ".join((value or "").strip().split())
    if required and not cleaned:
        raise HTTPException(status_code=422, detail=f"{field} es obligatorio")
    if len(cleaned) > maximum:
        raise HTTPException(status_code=422, detail=f"{field} es demasiado largo")
    return cleaned


def _json_list(value: str | None, *, field: str, maximum: int = 20) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"{field} invalido") from exc
    if not isinstance(parsed, list):
        raise HTTPException(status_code=422, detail=f"{field} invalido")
    result = [_clean(str(item), field=field, maximum=100) for item in parsed if str(item).strip()]
    if not result or len(result) > maximum:
        raise HTTPException(status_code=422, detail=f"{field} invalido")
    return result


def _form_bool(value: bool | str | None) -> bool:
    return value is True or str(value or "").strip().lower() in {"1", "true", "on", "yes", "si"}


def _file_signature(content_type: str, data: bytes) -> bool:
    if content_type == "application/pdf":
        return data.startswith(b"%PDF-")
    if content_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if content_type == "video/mp4":
        return len(data) >= 12 and data[4:8] == b"ftyp"
    return False


async def _store_uploads(files: list[UploadFile], public_code: str) -> list[dict[str, str | int]]:
    if len(files) > MAX_FILES:
        raise HTTPException(status_code=422, detail="Demasiados archivos")
    folder = APPLICATION_UPLOAD_DIR / public_code
    stored: list[dict[str, str | int]] = []
    total = 0
    try:
        for upload in files:
            filename = Path(upload.filename or "").name
            suffix = Path(filename).suffix.lower()
            if upload.content_type not in ALLOWED_FILE_TYPES or suffix not in ALLOWED_FILE_TYPES[upload.content_type]:
                raise HTTPException(status_code=422, detail="Tipo de archivo no permitido")
            data = await upload.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES or not _file_signature(upload.content_type, data):
                raise HTTPException(status_code=422, detail="Archivo invalido o demasiado grande")
            total += len(data)
            if total > MAX_TOTAL_FILE_BYTES:
                raise HTTPException(status_code=422, detail="El total de archivos es demasiado grande")
            folder.mkdir(parents=True, exist_ok=True)
            safe_name = f"{uuid4().hex}{suffix}"
            path = folder / safe_name
            path.write_bytes(data)
            stored.append({"name": filename[:160], "path": f"/uploads/professional-applications/{public_code}/{safe_name}", "bytes": len(data), "content_type": upload.content_type})
    except Exception:
        for item in stored:
            (UPLOADS_DIR / str(item["path"])[len("/uploads/"):]).unlink(missing_ok=True)
        raise
    return stored


def _new_public_code(db: Session) -> str:
    for _ in range(8):
        code = f"TS-PRO-{secrets.randbelow(1_000_000):06d}"
        if not db.query(ProfessionalApplication.id).filter(ProfessionalApplication.public_code == code).first():
            return code
    raise HTTPException(status_code=503, detail="No fue posible generar el codigo publico")


@router.post("/professional-applications", status_code=201)
async def create_professional_application(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    phone: str | None = Form(default=None),
    whatsapp: str = Form(...),
    city: str = Form(...),
    zone: str = Form(...),
    professional_type: str = Form(...),
    experience: str = Form(...),
    professional_level: str = Form(...),
    specialties: str = Form(...),
    coverage: str = Form(...),
    availability: str = Form(...),
    languages: str = Form(...),
    presentation: str | None = Form(default=None),
    has_vehicle: bool = Form(default=False),
    has_tools: bool = Form(default=False),
    has_team: bool = Form(default=False),
    can_invoice: bool = Form(default=False),
    attention_emergencies: bool = Form(default=False),
    consent_data: bool = Form(default=False),
    consent_contact: bool = Form(default=False),
    consent_profile: bool = Form(default=False),
    submission_key: str | None = Form(default=None),
    website: str | None = Form(default=None),
    files: list[UploadFile] | None = File(default=None),
    db: Session = Depends(get_db),
    accept_language: str | None = Header(default=None),
):
    del accept_language
    if website and website.strip():
        raise HTTPException(status_code=422, detail="No fue posible registrar la candidatura")
    name = _clean(full_name, field="Nombre", maximum=160)
    normalized_email = _clean(email, field="Email", maximum=320).lower()
    if not EMAIL_RE.fullmatch(normalized_email):
        raise HTTPException(status_code=422, detail="Email invalido")
    whatsapp_value = _clean(whatsapp, field="WhatsApp", maximum=40)
    identity = f"{normalized_email}:{whatsapp_value}"
    apply_public_rate_limits(db, request, identity, "professional_application")
    if not _form_bool(consent_data) or not _form_bool(consent_contact) or not _form_bool(consent_profile):
        raise HTTPException(status_code=422, detail="Los consentimientos son obligatorios")
    key_hash = hash_value(_clean(submission_key, field="submission_key", maximum=128) if submission_key else None)
    if key_hash:
        existing = db.query(ProfessionalApplication).filter(ProfessionalApplication.submission_key_hash == key_hash).first()
        if existing:
            db.commit()
            return {"public_code": existing.public_code, "status": existing.status, "duplicate": True}
    specialties_list = _json_list(specialties, field="Especialidades")
    coverage_list = _json_list(coverage, field="Cobertura")
    values = {
        "full_name": name,
        "email": normalized_email,
        "phone": _clean(phone, field="Telefono", maximum=40, required=False) or None,
        "whatsapp": whatsapp_value,
        "city": _clean(city, field="Ciudad", maximum=120),
        "zone": _clean(zone, field="Zona", maximum=120),
        "professional_type": _clean(professional_type, field="Tipo profesional", maximum=80),
        "experience": _clean(experience, field="Experiencia", maximum=80),
        "professional_level": _clean(professional_level, field="Nivel", maximum=40),
        "specialties_json": json.dumps(specialties_list, ensure_ascii=False),
        "coverage_json": json.dumps(coverage_list, ensure_ascii=False),
        "availability": _clean(availability, field="Disponibilidad", maximum=160),
        "languages": _clean(languages, field="Idiomas", maximum=160),
        "has_vehicle": _form_bool(has_vehicle),
        "has_tools": _form_bool(has_tools),
        "has_team": _form_bool(has_team),
        "can_invoice": _form_bool(can_invoice),
        "attention_emergencies": _form_bool(attention_emergencies),
        "presentation": _clean(presentation, field="Presentacion", maximum=4000, required=False) or None,
        "submission_key_hash": key_hash,
        "status": "RECEIVED",
        "consent_data": True,
        "consent_contact": True,
        "consent_profile": True,
    }
    application = ProfessionalApplication(public_code=_new_public_code(db), **values)
    try:
        db.add(application)
        db.flush()
        uploaded = await _store_uploads(files or [], application.public_code)
        application.uploads_json = json.dumps(uploaded, ensure_ascii=False) if uploaded else None
        db.commit()
        db.refresh(application)
        base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
        action_url = f"{base_url}/admin/professional-applications/{application.public_code}" if base_url else f"/admin/professional-applications/{application.public_code}"
        body = "\n".join([
            "Nueva candidatura para la red de profesionales de Total Solutions.",
            f"Codigo: {application.public_code}",
            f"Nombre: {application.full_name}",
            f"Email: {application.email}",
            f"WhatsApp: {application.whatsapp}",
            f"Perfil: {application.professional_type} / {application.professional_level}",
            f"Especialidades: {', '.join(specialties_list)}",
            f"Cobertura: {', '.join(coverage_list)}",
            f"Abrir candidatura: {action_url}",
        ])
        try:
            db.add(EmailOutbox(
                to_email=PROFESSIONAL_APPLICATION_EMAIL,
                subject=f"Nueva candidatura profesional {application.public_code}",
                body_text=body,
                body_html=None,
                template_type="PROFESSIONAL_APPLICATION",
                idempotency_key=f"professional-application:{application.public_code}",
                next_attempt_at=application.created_at,
            ))
            db.commit()
        except Exception:  # noqa: BLE001 - notification failure must not lose the application.
            db.rollback()
        return {"public_code": application.public_code, "status": application.status, "duplicate": False}
    except IntegrityError:
        db.rollback()
        if key_hash:
            existing = db.query(ProfessionalApplication).filter(ProfessionalApplication.submission_key_hash == key_hash).first()
            if existing:
                return {"public_code": existing.public_code, "status": existing.status, "duplicate": True}
        raise HTTPException(status_code=409, detail="No fue posible registrar la candidatura")
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:  # noqa: BLE001 - persistence must return a safe public error.
        db.rollback()
        raise HTTPException(status_code=500, detail="No fue posible registrar la candidatura") from exc
