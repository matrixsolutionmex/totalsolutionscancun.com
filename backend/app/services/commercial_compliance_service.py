import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.commercial_compliance import CommercialAuditEvent, CommercialOutreach, GlobalSuppression
from app.models.user import User


PRIVACY_NOTICE_VERSION = "TS_PRIVACY_2026_01"
SUPPRESSION_REASONS = {
    "OPT_OUT", "DO_NOT_CONTACT", "LEGAL_REQUEST", "COMPLAINT",
    "PERMANENT_BOUNCE", "MANUAL_BLOCK", "OTHER",
}
SUPPRESSION_SCOPES = {"EMAIL_ONLY", "DOMAIN", "ALL_MARKETING"}
OUTREACH_STATUSES = {
    "DRAFT", "READY_FOR_REVIEW", "APPROVED", "CONTACTED", "REPLIED",
    "REFERRED", "MEETING", "DECLINED", "OPTED_OUT", "SUPPRESSED",
}
COMPLIANCE_READY_STATUSES = {"VALID", "APPROVED", "COMPLIANT"}
AUTHORIZED_APPROVAL_ROLES = {"ROOT", "GERENTE"}


def privacy_contact_email() -> str:
    return os.getenv("PRIVACY_CONTACT_EMAIL", "").strip()


def legal_placeholders() -> dict:
    return {
        "legal_entity_name": os.getenv("LEGAL_ENTITY_NAME", "").strip(),
        "business_address": os.getenv("BUSINESS_ADDRESS", "").strip(),
        "privacy_contact": privacy_contact_email(),
        "controller_responsible": os.getenv("PRIVACY_CONTROLLER_NAME", "").strip(),
        "jurisdiction": os.getenv("PRIVACY_JURISDICTION", "").strip(),
        "effective_date": os.getenv("PRIVACY_EFFECTIVE_DATE", "").strip(),
    }


def missing_legal_placeholders() -> list[str]:
    return [key for key, value in legal_placeholders().items() if not value]


def privacy_notice_status() -> dict:
    missing = missing_legal_placeholders()
    return {
        "version": PRIVACY_NOTICE_VERSION,
        "ready": not missing,
        "missing": missing,
        "privacy_contact_email_configured": bool(privacy_contact_email()),
    }


def normalize_email(value: str | None) -> str | None:
    email = (value or "").strip().lower()
    if not email or len(email) > 320 or "@" not in email:
        return None
    local, domain = email.rsplit("@", 1)
    if not local or not normalize_domain(domain):
        return None
    if re.search(r"\s", email):
        return None
    return f"{local}@{normalize_domain(domain)}"


def normalize_domain(value: str | None) -> str | None:
    domain = (value or "").strip().lower()
    if not domain:
        return None
    if "://" in domain:
        domain = urlparse(domain).netloc
    domain = domain.split("/", 1)[0].split(":", 1)[0].strip(".")
    if not domain or len(domain) > 255 or "." not in domain:
        return None
    if not re.fullmatch(r"[a-z0-9.-]+", domain):
        return None
    return domain


def domain_from_email(email: str | None) -> str | None:
    normalized = normalize_email(email)
    if not normalized:
        return None
    return normalized.rsplit("@", 1)[1]


def audit_event(
    db: Session,
    event_type: str,
    *,
    actor_user_id: int | None = None,
    organization_id: int | None = None,
    object_type: str | None = None,
    object_id: int | None = None,
    reason: str | None = None,
    metadata: dict | None = None,
) -> CommercialAuditEvent:
    event = CommercialAuditEvent(
        event_type=event_type,
        actor_user_id=actor_user_id,
        organization_id=organization_id,
        object_type=object_type,
        object_id=object_id,
        reason=(reason or "")[:240] or None,
        metadata_json=json.dumps(metadata or {}, sort_keys=True, ensure_ascii=False)[:4000],
    )
    db.add(event)
    return event


def _suppression_query(db: Session, organization_id: int | None):
    return db.query(GlobalSuppression).filter(
        or_(
            GlobalSuppression.organization_id.is_(None),
            GlobalSuppression.organization_id == organization_id,
        )
    )


def find_suppression(
    db: Session,
    *,
    organization_id: int | None,
    email: str | None = None,
    domain: str | None = None,
) -> GlobalSuppression | None:
    normalized_email = normalize_email(email)
    normalized_domain = normalize_domain(domain) or domain_from_email(normalized_email)
    query = _suppression_query(db, organization_id)
    if normalized_email:
        row = query.filter(GlobalSuppression.normalized_email == normalized_email).order_by(GlobalSuppression.organization_id.desc()).first()
        if row:
            return row
    if normalized_domain:
        row = query.filter(GlobalSuppression.domain == normalized_domain).order_by(GlobalSuppression.organization_id.desc()).first()
        if row:
            return row
    return None


def upsert_suppression(
    db: Session,
    *,
    organization_id: int | None,
    email: str | None = None,
    domain: str | None = None,
    reason: str = "MANUAL_BLOCK",
    scope: str = "EMAIL_ONLY",
    source: str = "MANUAL",
    notes: str | None = None,
    actor_user_id: int | None = None,
) -> tuple[GlobalSuppression, bool]:
    reason = (reason or "MANUAL_BLOCK").upper()
    scope = (scope or "EMAIL_ONLY").upper()
    if reason not in SUPPRESSION_REASONS:
        raise HTTPException(status_code=422, detail="Motivo de suppression inválido")
    if scope not in SUPPRESSION_SCOPES:
        raise HTTPException(status_code=422, detail="Escopo de suppression inválido")
    normalized_email = normalize_email(email)
    normalized_domain = normalize_domain(domain)
    if scope == "DOMAIN" and not normalized_domain:
        raise HTTPException(status_code=422, detail="Domínio inválido")
    if scope != "DOMAIN" and not normalized_email:
        raise HTTPException(status_code=422, detail="Email inválido")
    if scope != "DOMAIN":
        normalized_domain = None

    row = db.query(GlobalSuppression).filter(
        GlobalSuppression.organization_id.is_(None) if organization_id is None else GlobalSuppression.organization_id == organization_id,
        GlobalSuppression.domain == normalized_domain if scope == "DOMAIN" else GlobalSuppression.normalized_email == normalized_email,
    ).first()
    created = row is None
    if row is None:
        row = GlobalSuppression(
            organization_id=organization_id,
            normalized_email=normalized_email if scope != "DOMAIN" else None,
            domain=normalized_domain if scope == "DOMAIN" else None,
            reason=reason,
            scope=scope,
            source=source[:80],
            notes=notes,
            created_by_user_id=actor_user_id,
        )
        db.add(row)
        db.flush()
    else:
        row.reason = reason
        row.scope = scope
        row.source = source[:80]
        row.notes = notes
        row.updated_at = datetime.utcnow()

    audit_event(
        db,
        "domain_block_created" if scope == "DOMAIN" and created else "suppression_created" if created else "suppression_updated",
        actor_user_id=actor_user_id,
        organization_id=organization_id,
        object_type="GlobalSuppression",
        object_id=row.id,
        reason=reason,
        metadata={"scope": scope, "source": source},
    )
    return row, created


def public_opt_out(db: Session, *, email: str, action: str, reason: str | None = None, request_metadata: dict | None = None) -> GlobalSuppression:
    normalized = normalize_email(email)
    if not normalized:
        raise HTTPException(status_code=422, detail="Email inválido")
    action = (action or "").upper()
    if action not in {"STOP_MARKETING", "STOP_ALL_NON_TRANSACTIONAL"}:
        raise HTTPException(status_code=422, detail="Preferência inválida")
    scope = "ALL_MARKETING" if action == "STOP_ALL_NON_TRANSACTIONAL" else "EMAIL_ONLY"
    audit_event(
        db,
        "opt_out_received",
        object_type="GlobalSuppression",
        reason=action,
        metadata={"public": True, **(request_metadata or {})},
    )
    row, _created = upsert_suppression(
        db,
        organization_id=None,
        email=normalized,
        reason="OPT_OUT",
        scope=scope,
        source="PUBLIC_OPT_OUT",
        notes=reason,
    )
    return row


def create_outreach(db: Session, *, actor: User, payload: dict) -> CommercialOutreach:
    organization_id = payload.get("organization_id") if actor.role == "ROOT" else actor.organization_id
    if not organization_id:
        raise HTTPException(status_code=400, detail="Organização obrigatória")
    recipient = normalize_email(payload.get("recipient"))
    if not recipient:
        raise HTTPException(status_code=422, detail="Recipient inválido")
    row = CommercialOutreach(
        organization_id=organization_id,
        company_name=(payload.get("company_name") or "").strip()[:240],
        domain=normalize_domain(payload.get("domain")) or domain_from_email(recipient),
        recipient=recipient,
        channel=(payload.get("channel") or "EMAIL").upper()[:40],
        campaign_type=(payload.get("campaign_type") or "HUMAN_OUTREACH").upper()[:80],
        message_version=(payload.get("message_version") or "")[:80] or None,
        privacy_notice_version=PRIVACY_NOTICE_VERSION,
        contact_source=(payload.get("contact_source") or "").strip()[:120],
        contact_source_url=(payload.get("contact_source_url") or "").strip() or None,
        compliance_status=(payload.get("compliance_status") or "PENDING_REVIEW").upper()[:40],
        status=(payload.get("status") or "DRAFT").upper()[:40],
    )
    if not row.company_name or not row.contact_source:
        raise HTTPException(status_code=422, detail="Empresa e fonte do contato são obrigatórias")
    if row.status not in OUTREACH_STATUSES:
        raise HTTPException(status_code=422, detail="Status inválido")
    db.add(row)
    db.flush()
    audit_event(db, "outreach_created", actor_user_id=actor.id, organization_id=organization_id, object_type="CommercialOutreach", object_id=row.id)
    return row


def approve_outreach(db: Session, *, outreach: CommercialOutreach, actor: User) -> CommercialOutreach:
    if actor.role not in AUTHORIZED_APPROVAL_ROLES:
        raise HTTPException(status_code=403, detail="Aprovação comercial requer gerente ou root")
    if actor.role != "ROOT" and outreach.organization_id != actor.organization_id:
        raise HTTPException(status_code=404, detail="Outreach não encontrado")
    outreach.status = "APPROVED"
    outreach.human_approved_by = actor.id
    outreach.human_approved_at = datetime.utcnow()
    outreach.updated_at = datetime.utcnow()
    audit_event(db, "outreach_approved", actor_user_id=actor.id, organization_id=outreach.organization_id, object_type="CommercialOutreach", object_id=outreach.id)
    return outreach


@dataclass
class ContactDecision:
    eligible: bool
    decision: str
    reason: str
    suppression_match: int | None
    privacy_notice_version: str
    human_approval: bool

    def as_dict(self) -> dict:
        return {
            "eligible": self.eligible,
            "decision": self.decision,
            "reason": self.reason,
            "suppression_match": self.suppression_match,
            "privacy_notice_version": self.privacy_notice_version,
            "human_approval": self.human_approval,
        }


def can_contact(db: Session, *, email: str | None = None, domain: str | None = None, outreach_id: int | None = None) -> ContactDecision:
    outreach = db.query(CommercialOutreach).filter(CommercialOutreach.id == outreach_id).first() if outreach_id else None
    organization_id = outreach.organization_id if outreach else None
    recipient = normalize_email(email or (outreach.recipient if outreach else None))
    normalized_domain = normalize_domain(domain or (outreach.domain if outreach else None)) or domain_from_email(recipient)
    if not recipient:
        return ContactDecision(False, "BLOCKED_INVALID_RECIPIENT", "Recipient inválido", None, PRIVACY_NOTICE_VERSION, False)

    email_match = find_suppression(db, organization_id=organization_id, email=recipient)
    if email_match and email_match.normalized_email:
        return ContactDecision(False, "BLOCKED_SUPPRESSION", email_match.reason, email_match.id, PRIVACY_NOTICE_VERSION, False)
    domain_match = find_suppression(db, organization_id=organization_id, domain=normalized_domain)
    if domain_match and domain_match.domain:
        return ContactDecision(False, "BLOCKED_DOMAIN", domain_match.reason, domain_match.id, PRIVACY_NOTICE_VERSION, False)

    if not privacy_notice_status()["ready"]:
        return ContactDecision(False, "BLOCKED_PRIVACY_NOT_READY", "Privacy notice legal placeholders missing", None, PRIVACY_NOTICE_VERSION, bool(outreach and outreach.human_approved_by))
    if outreach:
        if normalize_email(outreach.recipient) != recipient:
            return ContactDecision(False, "BLOCKED_INVALID_RECIPIENT", "Recipient não corresponde ao outreach", None, PRIVACY_NOTICE_VERSION, False)
        if outreach.compliance_status not in COMPLIANCE_READY_STATUSES:
            return ContactDecision(False, "BLOCKED_COMPLIANCE", outreach.compliance_status, None, PRIVACY_NOTICE_VERSION, bool(outreach.human_approved_by))
        if outreach.status != "APPROVED" or not outreach.human_approved_by or not outreach.human_approved_at:
            return ContactDecision(False, "BLOCKED_NO_HUMAN_APPROVAL", "Aprovação humana obrigatória", None, PRIVACY_NOTICE_VERSION, False)
        outreach.suppression_checked_at = datetime.utcnow()
        return ContactDecision(True, "ELIGIBLE_FOR_HUMAN_INITIATED_CONTACT", "Human initiated contact allowed", None, PRIVACY_NOTICE_VERSION, True)
    return ContactDecision(False, "BLOCKED_NO_HUMAN_APPROVAL", "Outreach obrigatório", None, PRIVACY_NOTICE_VERSION, False)
