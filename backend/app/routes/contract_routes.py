
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_current_user as get_actor, get_db
from app.models.contract import Contract
from app.models.contract_event import ContractEvent
from app.models.lead import Lead
from app.models.user import User
from app.schemas.contract_schema import ContractCreate, ContractResponse
from app.routes.lead_routes import ensure_lead_visible_to_actor

router = APIRouter(prefix="/contracts", tags=["contracts"])


@router.get("/", response_model=list[ContractResponse])
def list_contracts(
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    query = db.query(Contract)

    if actor.role == "BROKER":
        query = query.filter(Contract.created_by_user_id == actor.id)

    return query.order_by(Contract.id.desc()).limit(100).all()


@router.post("/", response_model=ContractResponse)
def create_contract(
    payload: ContractCreate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    lead = db.query(Lead).filter(Lead.id == payload.lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")

    ensure_lead_visible_to_actor(db, lead, actor)

    contract = Contract(
        lead_id=payload.lead_id,
        created_by_user_id=actor.id,
        contract_type=payload.contract_type,
        client_name=payload.client_name,
        client_email=payload.client_email,
        client_phone=payload.client_phone,
        property_address=payload.property_address,
        business_value=payload.business_value,
        commission_value=payload.commission_value,
        notes=payload.notes,
        status="RASCUNHO",
    )

    db.add(contract)
    db.commit()
    db.refresh(contract)

    db.add(
        ContractEvent(
            contract_id=contract.id,
            actor_id=actor.id,
            actor_name=actor.full_name or actor.username,
            event_type="CRIADO",
            message=f"Contrato {contract.contract_type} criado para o lead #{contract.lead_id}",
        )
    )
    db.commit()

    return contract


@router.get("/{contract_id}", response_model=ContractResponse)
def get_contract(
    contract_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    contract = db.query(Contract).filter(Contract.id == contract_id).first()
    if not contract:
        raise HTTPException(status_code=404, detail="Contrato não encontrado")

    lead = db.query(Lead).filter(Lead.id == contract.lead_id).first()
    if lead:
        ensure_lead_visible_to_actor(db, lead, actor)

    return contract

@router.get("/{contract_id}/events")
def get_contract_events(
    contract_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    contract = db.query(Contract).filter(Contract.id == contract_id).first()
    if not contract:
        raise HTTPException(status_code=404, detail="Contrato não encontrado")

    lead = db.query(Lead).filter(Lead.id == contract.lead_id).first()
    if lead:
        ensure_lead_visible_to_actor(db, lead, actor)

    events = (
        db.query(ContractEvent)
        .filter(ContractEvent.contract_id == contract_id)
        .order_by(ContractEvent.created_at.desc(), ContractEvent.id.desc())
        .all()
    )

    return [
        {
            "id": event.id,
            "contract_id": event.contract_id,
            "actor_id": event.actor_id,
            "actor_name": event.actor_name,
            "event_type": event.event_type,
            "message": event.message,
            "created_at": event.created_at,
        }
        for event in events
    ]
