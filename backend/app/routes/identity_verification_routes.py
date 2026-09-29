import os

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
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
from app.services.identity_human_review_service import (
    decide_review,
    get_review_item,
    list_review_queue,
)


router = APIRouter(prefix="/identity-verification", tags=["identity-verification"])


class IdentityVerificationStartRequest(BaseModel):
    consent: bool
    consent_version: str
    policy: str = "MEXICAN"


class IdentityHumanReviewDecisionRequest(BaseModel):
    decision: str = Field(min_length=1, max_length=32)
    reason_code: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=128)


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


@router.get("/human-review/queue")
def get_human_review_queue(
    actor: User = Depends(get_actor),
    db: Session = Depends(get_db),
):
    try:
        return {"items": list_review_queue(db, reviewer=actor)}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="human_review_not_permitted") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="human_review_unavailable") from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="human_review_unavailable") from exc


@router.get("/human-review/attempts/{attempt_id}")
def get_human_review_attempt(
    attempt_id: int,
    actor: User = Depends(get_actor),
    db: Session = Depends(get_db),
):
    try:
        return get_review_item(db, attempt_id=attempt_id, reviewer=actor)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="human_review_not_permitted") from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="review_item_not_found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="review_item_not_pending") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="human_review_unavailable") from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="human_review_unavailable") from exc


@router.post("/human-review/attempts/{attempt_id}/decision")
def post_human_review_decision(
    attempt_id: int,
    request: IdentityHumanReviewDecisionRequest,
    actor: User = Depends(get_actor),
    db: Session = Depends(get_db),
):
    try:
        result = decide_review(
            db,
            attempt_id=attempt_id,
            reviewer=actor,
            decision=request.decision,
            reason_code=request.reason_code,
            idempotency_key=request.idempotency_key,
        )
        db.commit()
        return result
    except PermissionError as exc:
        db.rollback()
        raise HTTPException(status_code=403, detail="human_review_not_permitted") from exc
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail="review_item_not_found") from exc
    except RuntimeError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="human_review_unavailable") from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="review_decision_not_applied") from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="human_review_unavailable") from exc
