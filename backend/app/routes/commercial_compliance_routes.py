from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_db, require_admin_user
from app.core.auth_security import apply_public_rate_limits, request_ip
from app.models.commercial_compliance import CommercialAuditEvent, CommercialOutreach, GlobalSuppression
from app.models.user import User
from app.services.commercial_compliance_service import (
    PRIVACY_NOTICE_VERSION,
    approve_outreach,
    audit_event,
    can_contact,
    create_outreach,
    legal_placeholders,
    normalize_domain,
    normalize_email,
    privacy_notice_status,
    public_opt_out,
    upsert_suppression,
)


router = APIRouter(tags=["commercial-compliance"])


class SuppressionIn(BaseModel):
    email: str | None = None
    domain: str | None = None
    reason: str = "MANUAL_BLOCK"
    scope: str = "EMAIL_ONLY"
    source: str = "MANUAL"
    notes: str | None = None
    organization_id: int | None = None


class OutreachIn(BaseModel):
    organization_id: int | None = None
    company_name: str = Field(min_length=1, max_length=240)
    domain: str | None = None
    recipient: str = Field(min_length=3, max_length=320)
    channel: str = "EMAIL"
    campaign_type: str = "HUMAN_OUTREACH"
    message_version: str | None = None
    contact_source: str = Field(min_length=1, max_length=120)
    contact_source_url: str | None = None
    compliance_status: str = "PENDING_REVIEW"
    status: str = "DRAFT"


class PreferenceIn(BaseModel):
    email: str
    action: str
    reason: str | None = None
    website: str | None = None


def _html_page(title: str, body: str, lang: str = "es-MX") -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
<html lang="{lang}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} | Total Solutions Cancún</title>
  <link rel="canonical" href="https://totalsolutionscancun.com/aviso-de-privacidad">
  <link rel="icon" href="/assets/assets/favicon.svg" type="image/svg+xml">
  <link rel="stylesheet" href="/assets/public-site.css?v=9728e26">
</head>
<body class="public-site legal-page">
  <header><p class="public-kicker">Total Solutions Cancún</p><h1>{title}</h1></header>
  <main>{body}</main>
  <footer class="public-footer">
    <span><a href="/aviso-de-privacidad">Aviso de Privacidad</a> · <a href="/preferencias-comunicacion">Preferencias de comunicación</a></span>
  </footer>
</body>
</html>"""
    )


def _notice_copy(language: str) -> tuple[str, str, str]:
    status = privacy_notice_status()
    placeholders = legal_placeholders()
    missing = ", ".join(status["missing"]) if status["missing"] else ""
    contact = placeholders["privacy_contact"] or "[PRIVACY_CONTACT_EMAIL pendiente]"
    entity = placeholders["legal_entity_name"] or "[LEGAL_ENTITY_NAME pendiente]"
    address = placeholders["business_address"] or "[BUSINESS_ADDRESS pendiente]"
    controller = placeholders["controller_responsible"] or "[CONTROLLER/RESPONSIBLE pendiente]"
    jurisdiction = placeholders["jurisdiction"] or "[JURISDICTION pendiente]"
    effective = placeholders["effective_date"] or "[EFFECTIVE_DATE pendiente]"
    pending = f"<p><strong>Estado:</strong> NOT_READY. Pendientes legales: {missing}</p>" if missing else "<p><strong>Estado:</strong> READY.</p>"

    if language == "en":
        title = "Privacy Notice"
        intro = "This notice explains how Total Solutions Cancun handles personal data for service operations and human-reviewed commercial outreach."
        lang = "en"
    elif language == "pt":
        title = "Aviso de Privacidade"
        intro = "Este aviso explica como a Total Solutions Cancun trata dados pessoais para operações de serviço e prospecção comercial revisada por pessoas."
        lang = "pt-BR"
    else:
        title = "Aviso de Privacidad"
        intro = "Este aviso explica cómo Total Solutions Cancún trata datos personales para operación de servicios y prospección comercial revisada por personas."
        lang = "es-MX"

    body = f"""
<section>
  <p>{intro}</p>
  <p><strong>Versión:</strong> {PRIVACY_NOTICE_VERSION}</p>
  {pending}
  <h2>Responsable / Controller</h2>
  <p>{entity}<br>{address}<br>{controller}<br>{jurisdiction}<br>{effective}</p>
  <h2>Contacto de privacidad</h2>
  <p>{contact}</p>
  <h2>Datos y finalidades</h2>
  <p>Podemos tratar datos de contacto, identificación profesional, empresa, dominio, fuente pública o comercial, preferencias de comunicación y registros de auditoría para atender solicitudes, operar el CRM, documentar cumplimiento, gestionar oposiciones y evaluar contactos comerciales iniciados manualmente.</p>
  <h2>Origen, proveedores y transferencias</h2>
  <p>Los datos pueden provenir de formularios propios, interacciones operativas, fuentes entregadas por el interesado o fuentes comerciales documentadas. Podemos usar proveedores técnicos necesarios para hospedaje, seguridad, almacenamiento y operación del CRM, sin vender la base ni autorizar envíos automáticos desde este módulo.</p>
  <h2>Derechos, oposición y opt-out</h2>
  <p>Puede oponerse a comunicaciones no transaccionales o registrar una preferencia en <a href="/preferencias-comunicacion">Preferencias de comunicación</a>. La respuesta pública es genérica para proteger contra enumeración de emails.</p>
  <h2>Retención, seguridad y cambios</h2>
  <p>Conservamos registros mientras sean necesarios para operación, cumplimiento, auditoría o supresión. Aplicamos controles de acceso, auditoría y bloqueo por suppression. Los cambios relevantes se versionan.</p>
</section>"""
    return title, body, lang


@router.get("/aviso-de-privacidad", response_class=HTMLResponse, include_in_schema=False)
def privacy_notice(lang: str = Query("es")):
    normalized = (lang or "es").lower()
    language = "en" if normalized.startswith("en") else "pt" if normalized.startswith("pt") else "es"
    title, body, html_lang = _notice_copy(language)
    return _html_page(title, body, html_lang)


@router.get("/preferencias-comunicacion", response_class=HTMLResponse, include_in_schema=False)
def communication_preferences():
    body = """
<section>
  <p>Registre su preferencia de comunicación. La respuesta no confirma si el email existe en nuestra base.</p>
  <form method="post" action="/preferencias-comunicacion">
    <label>Email <input name="email" type="email" required maxlength="320"></label>
    <label>Preferencia
      <select name="action">
        <option value="STOP_MARKETING">No recibir marketing</option>
        <option value="STOP_ALL_NON_TRANSACTIONAL">No recibir comunicaciones no transaccionales</option>
      </select>
    </label>
    <label>Motivo opcional <input name="reason" maxlength="240"></label>
    <input name="website" tabindex="-1" autocomplete="off" style="position:absolute;left:-9999px" aria-hidden="true">
    <button type="submit">Registrar preferencia</button>
  </form>
</section>"""
    return _html_page("Preferencias de comunicación", body, "es-MX")


GENERIC_PREFERENCE_RESPONSE = {"message": "Su preferencia ha sido registrada."}


def _register_preference(db: Session, request: Request, payload: PreferenceIn):
    if payload.website:
        return GENERIC_PREFERENCE_RESPONSE
    normalized = normalize_email(payload.email)
    if normalized:
        apply_public_rate_limits(db, request, normalized, "communication-preferences")
        public_opt_out(
            db,
            email=normalized,
            action=payload.action,
            reason=payload.reason,
            request_metadata={"ip": bool(request_ip(request)), "channel": "public_form"},
        )
        db.commit()
    return GENERIC_PREFERENCE_RESPONSE


@router.post("/preferencias-comunicacion")
def communication_preferences_form(
    request: Request,
    email: str = Form(...),
    action: str = Form(...),
    reason: str | None = Form(default=None),
    website: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    return _register_preference(db, request, PreferenceIn(email=email, action=action, reason=reason, website=website))


@router.post("/public/communication-preferences")
def communication_preferences_api(payload: PreferenceIn, request: Request, db: Session = Depends(get_db)):
    return _register_preference(db, request, payload)


def _organization_scope(actor: User, requested: int | None = None) -> int | None:
    if actor.role == "ROOT":
        return requested
    if requested is not None and requested != actor.organization_id:
        raise HTTPException(status_code=403, detail="Organização fora do escopo")
    return actor.organization_id


def _outreach_or_404(db: Session, actor: User, outreach_id: int) -> CommercialOutreach:
    row = db.query(CommercialOutreach).filter(CommercialOutreach.id == outreach_id).first()
    if not row or (actor.role != "ROOT" and row.organization_id != actor.organization_id):
        raise HTTPException(status_code=404, detail="Outreach não encontrado")
    return row


def suppression_payload(row: GlobalSuppression) -> dict:
    return {
        "id": row.id, "organization_id": row.organization_id, "email": row.normalized_email,
        "domain": row.domain, "reason": row.reason, "scope": row.scope, "source": row.source,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def outreach_payload(row: CommercialOutreach) -> dict:
    return {
        "id": row.id, "organization_id": row.organization_id, "company_name": row.company_name,
        "domain": row.domain, "recipient": row.recipient, "channel": row.channel,
        "campaign_type": row.campaign_type, "message_version": row.message_version,
        "privacy_notice_version": row.privacy_notice_version, "contact_source": row.contact_source,
        "contact_source_url": row.contact_source_url, "compliance_status": row.compliance_status,
        "status": row.status, "human_approved_by": row.human_approved_by,
        "human_approved_at": row.human_approved_at.isoformat() if row.human_approved_at else None,
        "suppression_checked_at": row.suppression_checked_at.isoformat() if row.suppression_checked_at else None,
    }


@router.get("/admin/commercial-compliance/config")
def compliance_config(actor: User = Depends(require_admin_user)):
    return {"privacy": privacy_notice_status(), "version": PRIVACY_NOTICE_VERSION, "role": actor.role}


@router.get("/admin/commercial-compliance/suppressions")
def list_suppressions(
    search: str | None = None,
    organization_id: int | None = None,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin_user),
):
    scope_org = _organization_scope(actor, organization_id)
    query = db.query(GlobalSuppression)
    if actor.role != "ROOT":
        query = query.filter(or_(GlobalSuppression.organization_id.is_(None), GlobalSuppression.organization_id == scope_org))
    elif scope_org is not None:
        query = query.filter(or_(GlobalSuppression.organization_id.is_(None), GlobalSuppression.organization_id == scope_org))
    if search:
        term = f"%{search.strip().lower()}%"
        query = query.filter(or_(GlobalSuppression.normalized_email.ilike(term), GlobalSuppression.domain.ilike(term)))
    return {"items": [suppression_payload(row) for row in query.order_by(GlobalSuppression.id.desc()).limit(100).all()]}


@router.post("/admin/commercial-compliance/suppressions")
def create_suppression(payload: SuppressionIn, db: Session = Depends(get_db), actor: User = Depends(require_admin_user)):
    row, created = upsert_suppression(
        db,
        organization_id=_organization_scope(actor, payload.organization_id),
        email=payload.email,
        domain=payload.domain,
        reason=payload.reason,
        scope=payload.scope,
        source=payload.source,
        notes=payload.notes,
        actor_user_id=actor.id,
    )
    db.commit()
    return {"created": created, "suppression": suppression_payload(row)}


@router.get("/admin/commercial-compliance/outreach")
def list_outreach(db: Session = Depends(get_db), actor: User = Depends(require_admin_user)):
    query = db.query(CommercialOutreach)
    if actor.role != "ROOT":
        query = query.filter(CommercialOutreach.organization_id == actor.organization_id)
    return {"items": [outreach_payload(row) for row in query.order_by(CommercialOutreach.id.desc()).limit(100).all()]}


@router.post("/admin/commercial-compliance/outreach")
def create_outreach_route(payload: OutreachIn, db: Session = Depends(get_db), actor: User = Depends(require_admin_user)):
    row = create_outreach(db, actor=actor, payload=payload.model_dump())
    db.commit()
    return outreach_payload(row)


@router.post("/admin/commercial-compliance/outreach/{outreach_id}/approve")
def approve_outreach_route(outreach_id: int, db: Session = Depends(get_db), actor: User = Depends(require_admin_user)):
    row = approve_outreach(db, outreach=_outreach_or_404(db, actor, outreach_id), actor=actor)
    db.commit()
    return outreach_payload(row)


@router.get("/admin/commercial-compliance/outreach/{outreach_id}/can-contact")
def can_contact_route(outreach_id: int, db: Session = Depends(get_db), actor: User = Depends(require_admin_user)):
    row = _outreach_or_404(db, actor, outreach_id)
    decision = can_contact(db, outreach_id=row.id)
    if decision.eligible:
        audit_event(db, "suppression_checked", actor_user_id=actor.id, organization_id=row.organization_id, object_type="CommercialOutreach", object_id=row.id, metadata=decision.as_dict())
    db.commit()
    return decision.as_dict()


@router.get("/admin/commercial-compliance/audit")
def list_audit(db: Session = Depends(get_db), actor: User = Depends(require_admin_user)):
    query = db.query(CommercialAuditEvent)
    if actor.role != "ROOT":
        query = query.filter(or_(CommercialAuditEvent.organization_id.is_(None), CommercialAuditEvent.organization_id == actor.organization_id))
    return {"items": [
        {
            "id": row.id, "event_type": row.event_type, "organization_id": row.organization_id,
            "object_type": row.object_type, "object_id": row.object_id, "reason": row.reason,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in query.order_by(CommercialAuditEvent.id.desc()).limit(100).all()
    ]}
