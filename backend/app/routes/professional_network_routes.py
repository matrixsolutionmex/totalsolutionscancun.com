import json
import os
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_db, require_admin_user
from app.core.storage import UPLOADS_DIR
from app.models.professional_application import ProfessionalApplication
from app.models.professional_network import ProfessionalApplicationEvent, ProfessionalApplicationNote, ProfessionalApplicationSkill


router = APIRouter(prefix="/professional-network", tags=["professional-network"])
VALID_STATUSES = {"RECEIVED", "UNDER_REVIEW", "CONTACTED", "INTERVIEW", "DOCUMENTATION", "TECHNICAL_VALIDATION", "APPROVED", "REJECTED", "ACTIVE", "SUSPENDED", "INACTIVE"}
VALID_SKILL_STATUSES = {"DECLARED", "UNDER_REVIEW", "VALIDATED", "REJECTED"}
TRANSITIONS = {
    "RECEIVED": {"UNDER_REVIEW"}, "UNDER_REVIEW": {"CONTACTED", "REJECTED"}, "CONTACTED": {"INTERVIEW", "REJECTED"},
    "INTERVIEW": {"DOCUMENTATION", "REJECTED"}, "DOCUMENTATION": {"TECHNICAL_VALIDATION"},
    "TECHNICAL_VALIDATION": {"APPROVED", "REJECTED"}, "APPROVED": set(), "REJECTED": set(),
    "ACTIVE": set(), "SUSPENDED": set(), "INACTIVE": set(),
}
SPECIALTIES = ["Plomería", "Electricidad", "Aire acondicionado", "Refrigeración", "Pintura", "Impermeabilización", "Cisternas / tinacos / bombas", "Carpintería", "Albañilería", "Tablaroca / drywall", "Herrería / soldadura", "Cerrajería", "Pisos / azulejos", "Vidrios / aluminio", "Piscinas", "Electrodomésticos", "CCTV / seguridad", "Redes / internet", "Automatización", "Jardinería", "Limpieza técnica", "Ingeniería civil", "Ingeniería eléctrica", "Ingeniería mecánica", "Arquitectura", "Supervisión de obra", "Mantenimiento general"]


class StatusInput(BaseModel):
    status: str
    note: str | None = Field(default=None, max_length=4000)


class NoteInput(BaseModel):
    note: str = Field(min_length=1, max_length=4000)


class SkillInput(BaseModel):
    validation_status: str
    note: str | None = Field(default=None, max_length=2000)


def _check_scope(row: ProfessionalApplication | None, actor):
    if actor.role not in {"ROOT", "GERENTE"}:
        raise HTTPException(status_code=403, detail="Sem permissao para Red Profesional")
    if not row or (actor.role != "ROOT" and row.organization_id != actor.organization_id):
        raise HTTPException(status_code=404, detail="Candidatura no encontrada")
    return row


def _ensure_skills(db: Session, row: ProfessionalApplication):
    declared = json.loads(row.specialties_json or "[]")
    existing = {skill.specialty for skill in db.query(ProfessionalApplicationSkill).filter_by(application_id=row.id).all()}
    for specialty in declared:
        if specialty not in existing:
            db.add(ProfessionalApplicationSkill(application_id=row.id, specialty=specialty, validation_status="DECLARED"))
    db.flush()


def _application_payload(db: Session, row: ProfessionalApplication, *, detail=False):
    _ensure_skills(db, row)
    skills = db.query(ProfessionalApplicationSkill).filter_by(application_id=row.id).order_by(ProfessionalApplicationSkill.specialty.asc()).all()
    data = {"id": row.id, "public_code": row.public_code, "name": row.full_name, "professional_type": row.professional_type, "specialties": json.loads(row.specialties_json or "[]"), "experience": row.experience, "professional_level": row.professional_level, "city": row.city, "zone": row.zone, "availability": row.availability, "languages": row.languages, "status": row.status, "created_at": row.created_at.isoformat() if row.created_at else None, "skills": [{"specialty": s.specialty, "declared": s.declared, "validation_status": s.validation_status, "validated_by": s.validated_by, "validated_at": s.validated_at.isoformat() if s.validated_at else None, "note": s.note} for s in skills]}
    if detail:
        uploads = [{"name": upload.get("name"), "bytes": upload.get("bytes"), "content_type": upload.get("content_type"), "url": f"/professional-network/applications/{row.id}/files/{Path(str(upload.get('path', ''))).name}"} for upload in json.loads(row.uploads_json or "[]")]
        data.update({"whatsapp": row.whatsapp, "phone": row.phone, "email": row.email, "has_vehicle": row.has_vehicle, "has_tools": row.has_tools, "has_team": row.has_team, "can_invoice": row.can_invoice, "attention_emergencies": row.attention_emergencies, "coverage": json.loads(row.coverage_json or "[]"), "presentation": row.presentation, "uploads": uploads, "events": [{"previous_status": e.previous_status, "new_status": e.new_status, "actor_user_id": e.actor_user_id, "actor_role": e.actor_role, "note": e.note, "created_at": e.created_at.isoformat() if e.created_at else None} for e in db.query(ProfessionalApplicationEvent).filter_by(application_id=row.id).order_by(ProfessionalApplicationEvent.created_at.desc()).all()], "notes": [{"note": n.note, "actor_user_id": n.actor_user_id, "created_at": n.created_at.isoformat() if n.created_at else None} for n in db.query(ProfessionalApplicationNote).filter_by(application_id=row.id).order_by(ProfessionalApplicationNote.created_at.desc()).all()]})
    return data


@router.get("/applications")
def list_applications(status: str | None = None, specialty: str | None = None, level: str | None = None, experience: str | None = None, city: str | None = None, search: str | None = None, has_vehicle: bool | None = None, has_tools: bool | None = None, can_invoice: bool | None = None, language: str | None = None, db: Session = Depends(get_db), actor=Depends(require_admin_user)):
    if actor.role not in {"ROOT", "GERENTE"}:
        raise HTTPException(status_code=403, detail="Sem permissao para Red Profesional")
    query = db.query(ProfessionalApplication)
    if actor.role != "ROOT":
        query = query.filter(ProfessionalApplication.organization_id == actor.organization_id)
    if status: query = query.filter(ProfessionalApplication.status == status.upper())
    if level: query = query.filter(ProfessionalApplication.professional_level == level)
    if experience: query = query.filter(ProfessionalApplication.experience == experience)
    if city: query = query.filter(or_(ProfessionalApplication.city.ilike(f"%{city}%"), ProfessionalApplication.zone.ilike(f"%{city}%")))
    if search: query = query.filter(or_(ProfessionalApplication.full_name.ilike(f"%{search}%"), ProfessionalApplication.public_code.ilike(f"%{search}%"), ProfessionalApplication.email.ilike(f"%{search}%"), ProfessionalApplication.whatsapp.ilike(f"%{search}%")))
    if has_vehicle is not None: query = query.filter(ProfessionalApplication.has_vehicle.is_(has_vehicle))
    if has_tools is not None: query = query.filter(ProfessionalApplication.has_tools.is_(has_tools))
    if can_invoice is not None: query = query.filter(ProfessionalApplication.can_invoice.is_(can_invoice))
    rows = query.order_by(ProfessionalApplication.created_at.desc()).limit(250).all()
    if specialty or language:
        wanted = (specialty or "").casefold()
        rows = [row for row in rows if (not wanted or wanted in [s.casefold() for s in json.loads(row.specialties_json or "[]")]) and (not language or language.casefold() in row.languages.casefold())]
    return {"items": [_application_payload(db, row) for row in rows]}


@router.get("/applications/{application_id}")
def get_application(application_id: int, db: Session = Depends(get_db), actor=Depends(require_admin_user)):
    return _application_payload(db, _check_scope(db.get(ProfessionalApplication, application_id), actor), detail=True)


@router.patch("/applications/{application_id}/status")
def update_application_status(application_id: int, payload: StatusInput, db: Session = Depends(get_db), actor=Depends(require_admin_user)):
    row = _check_scope(db.get(ProfessionalApplication, application_id), actor)
    new_status = payload.status.upper()
    if new_status not in VALID_STATUSES or new_status not in TRANSITIONS.get(row.status, set()):
        raise HTTPException(status_code=409, detail="Transicion de candidatura no permitida")
    previous = row.status
    row.status = new_status
    db.add(ProfessionalApplicationEvent(application_id=row.id, previous_status=previous, new_status=new_status, actor_user_id=actor.id, actor_role=actor.role, note=payload.note))
    db.commit()
    return _application_payload(db, row)


@router.post("/applications/{application_id}/notes", status_code=201)
def add_application_note(application_id: int, payload: NoteInput, db: Session = Depends(get_db), actor=Depends(require_admin_user)):
    row = _check_scope(db.get(ProfessionalApplication, application_id), actor)
    db.add(ProfessionalApplicationNote(application_id=row.id, actor_user_id=actor.id, note=payload.note.strip()))
    db.commit()
    return {"status": "created"}


@router.patch("/applications/{application_id}/skills/{specialty}")
def validate_application_skill(application_id: int, specialty: str, payload: SkillInput, db: Session = Depends(get_db), actor=Depends(require_admin_user)):
    row = _check_scope(db.get(ProfessionalApplication, application_id), actor)
    status = payload.validation_status.upper()
    if status not in VALID_SKILL_STATUSES:
        raise HTTPException(status_code=422, detail="Estado de especialidad invalido")
    _ensure_skills(db, row)
    skill = db.query(ProfessionalApplicationSkill).filter_by(application_id=row.id, specialty=specialty).first()
    if not skill: raise HTTPException(status_code=404, detail="Especialidad no declarada")
    skill.validation_status = status
    skill.validated_by = actor.id if status in {"VALIDATED", "REJECTED"} else None
    skill.validated_at = datetime.utcnow() if skill.validated_by else None
    skill.note = payload.note
    db.commit()
    return _application_payload(db, row, detail=True)


@router.get("/coverage")
def coverage(db: Session = Depends(get_db), actor=Depends(require_admin_user)):
    if actor.role not in {"ROOT", "GERENTE"}:
        raise HTTPException(status_code=403, detail="Sem permissao para Red Profesional")
    query = db.query(ProfessionalApplicationSkill.specialty, func.count(func.distinct(ProfessionalApplicationSkill.application_id))).join(ProfessionalApplication, ProfessionalApplication.id == ProfessionalApplicationSkill.application_id).filter(ProfessionalApplication.status.in_(["APPROVED", "ACTIVE"]), ProfessionalApplicationSkill.validation_status == "VALIDATED")
    if actor.role != "ROOT": query = query.filter(ProfessionalApplication.organization_id == actor.organization_id)
    counts = dict(query.group_by(ProfessionalApplicationSkill.specialty).all())
    items = []
    for name in SPECIALTIES:
        count = int(counts.get(name, 0)); target = 2
        items.append({"name": name, "approved_count": count, "active_count": count, "target": target, "remaining": max(0, target - count), "coverage_status": "Sin cobertura" if count == 0 else "Cobertura parcial" if count < target else "Meta alcanzada"})
    return {"location": "Cancún", "target_per_specialty": 2, "specialties": items}


@router.get("/applications/{application_id}/files/{file_name}")
def application_file(application_id: int, file_name: str, db: Session = Depends(get_db), actor=Depends(require_admin_user)):
    row = _check_scope(db.get(ProfessionalApplication, application_id), actor)
    for upload in json.loads(row.uploads_json or "[]"):
        if Path(str(upload.get("path", ""))).name == file_name:
            path = (UPLOADS_DIR / "professional-applications" / row.public_code / file_name).resolve()
            base = (UPLOADS_DIR / "professional-applications" / row.public_code).resolve()
            if path.parent == base and path.is_file():
                return FileResponse(path, filename=str(upload.get("name", file_name)))
    raise HTTPException(status_code=404, detail="Archivo no encontrado")
