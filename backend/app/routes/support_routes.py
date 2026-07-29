from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth.jwt_handler import get_current_user as get_actor, get_db
from app.models.support_ticket import SupportTicket
from app.models.user import User
from app.schemas.support_schema import SupportTicketCreate, SupportTicketResponse, SupportTicketUpdate

router = APIRouter(prefix="/support", tags=["support"])


def require_manager_actor(actor: User = Depends(get_actor)):
    if actor.role not in {"ROOT", "GERENTE"}:
        raise HTTPException(status_code=403, detail="Somente root ou gerente pode acompanhar chamados")

    return actor


def serialize_ticket(ticket: SupportTicket) -> SupportTicketResponse:
    creator = ticket.created_by
    return SupportTicketResponse(
        id=ticket.id,
        protocol=ticket.protocol,
        module=ticket.module,
        priority=ticket.priority,
        message=ticket.message,
        status=ticket.status,
        created_by_user_id=ticket.created_by_user_id,
        created_by_username=creator.username if creator else None,
        created_by_full_name=creator.full_name if creator else None,
        created_by_role=creator.role if creator else None,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
    )


@router.post("/tickets", response_model=SupportTicketResponse)
def create_ticket(
    payload: SupportTicketCreate,
    db: Session = Depends(get_db),
    actor: User = Depends(get_actor),
):
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Mensagem obrigatoria")

    ticket = SupportTicket(
        module=payload.module.strip() or "Geral",
        priority=payload.priority.strip() or "Media",
        message=message,
        status="ABERTO",
        created_by_user_id=actor.id,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)

    ticket.protocol = f"LV-{ticket.id:06d}"
    db.commit()
    db.refresh(ticket)
    return serialize_ticket(ticket)


@router.get("/tickets", response_model=list[SupportTicketResponse])
def list_tickets(
    db: Session = Depends(get_db),
    _: User = Depends(require_manager_actor),
):
    tickets = db.query(SupportTicket).order_by(SupportTicket.created_at.desc()).all()
    return [serialize_ticket(ticket) for ticket in tickets]


@router.patch("/tickets/{ticket_id}", response_model=SupportTicketResponse)
def update_ticket(
    ticket_id: int,
    payload: SupportTicketUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_manager_actor),
):
    ticket = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Chamado nao encontrado")

    if payload.status is not None:
        status = payload.status.upper()
        if status not in {"ABERTO", "EM_ATENDIMENTO", "RESOLVIDO"}:
            raise HTTPException(status_code=400, detail="Status invalido")
        ticket.status = status

    db.commit()
    db.refresh(ticket)
    return serialize_ticket(ticket)
