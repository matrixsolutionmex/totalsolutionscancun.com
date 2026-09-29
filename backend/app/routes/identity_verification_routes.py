import os

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_current_user as get_actor, get_db
from app.database.connection import SessionLocal
from app.models.user import User
from app.services.identity_verification_service import get_identity_verification_payload
from app.services.identity_provider_service import (
    CONSENT_VERSION,
    create_attempt,
    parse_json_body,
    process_metamap_event,
    provider_config,
    verify_metamap_signature,
)


router = APIRouter(prefix="/identity-verification", tags=["identity-verification"])


class IdentityVerificationStartRequest(BaseModel):
    consent: bool
    consent_version: str
    policy: str = "MEXICAN"


@router.get("/provider-config")
def get_provider_config(actor: User = Depends(get_actor)):
    config = provider_config(actor.id, actor.role)
    enabled = bool(config["enabled"])
    return {
        "enabled": enabled,
        "provider": config["provider"] if enabled else None,
        "mode": config["mode"] if enabled else "disabled",
        "client_id": config["client_id"] if enabled else None,
        "flow_id": config["flow_id"] if enabled else None,
        "consent_version": CONSENT_VERSION,
    }


@router.post("/attempts")
def start_identity_attempt(
    request: IdentityVerificationStartRequest,
    actor: User = Depends(get_actor),
    db: Session = Depends(get_db),
):
    if not request.consent:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="consent_required")
    if request.policy not in {"MEXICAN", "FOREIGN_RESIDENT", "MANUAL_REVIEW"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unsupported_identity_policy")
    try:
        attempt, attempt_key, config = create_attempt(
            db,
            user=actor,
            organization_id=actor.organization_id,
            policy=request.policy,
            consent_version=request.consent_version,
        )
        db.commit()
    except RuntimeError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="identity_provider_unavailable") from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="identity_provider_unavailable") from exc
    return {"attempt_id": attempt.id, "attempt_key": attempt_key, **config}


@router.post("/webhooks/metamap")
async def receive_metamap_webhook(request: Request, x_signature: list[str] | None = Header(default=None)):
    raw_body = await request.body()
    secret = os.getenv("METAMAP_WEBHOOK_SECRET", "").strip()
    if not x_signature or len(x_signature) != 1 or not verify_metamap_signature(raw_body, x_signature[0], secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_provider_signature")
    db = SessionLocal()
    try:
        payload = parse_json_body(raw_body)
        result = process_metamap_event(db, raw_body=raw_body, payload=payload)
        db.commit()
        return {"received": True, "result": result}
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="identity_provider_unavailable") from exc
    finally:
        db.close()


@router.get("/me")
def get_my_identity_verification(
    actor: User = Depends(get_actor),
    db: Session = Depends(get_db),
):
    return get_identity_verification_payload(db, actor)
