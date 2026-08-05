import os

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_db
from app.core.organization import get_or_create_default_organization
from app.models.lead import Lead
from app.schemas.lead_schema import IntegrationLeadCreate, LeadResponse
from app.services.lead_entry_service import duplicate_lead, duplicate_lead_message, ensure_property_id, lead_mapping_from_integration
from app.services.service_order_service import ensure_service_order

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


def require_integration_token(authorization: str | None = Header(default=None)):
    expected = os.getenv("MATRIX_IMPORT_TOKEN", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Token de integracao nao configurado")

    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or token.strip() != expected:
        raise HTTPException(status_code=401, detail="Token de integracao invalido")


@router.post("/leads", response_model=LeadResponse, status_code=201)
def create_integration_lead(
    payload: IntegrationLeadCreate,
    db: Session = Depends(get_db),
    _token: None = Depends(require_integration_token),
):
    organization = get_or_create_default_organization(db)
    mapping = lead_mapping_from_integration(payload)
    mapping["organization_id"] = organization.id
    duplicate = duplicate_lead(
        db,
        email=mapping.get("email"),
        contato=mapping.get("contato"),
        whatsapp=mapping.get("whatsapp"),
        external_source=mapping.get("external_source"),
        external_id=mapping.get("external_id"),
        organization_id=organization.id,
    )
    if duplicate:
        raise HTTPException(status_code=409, detail=duplicate_lead_message(duplicate))

    lead = Lead(**mapping)
    db.add(lead)
    db.flush()
    ensure_property_id(lead)
    ensure_service_order(db, lead)
    db.commit()
    db.refresh(lead)
    return lead
