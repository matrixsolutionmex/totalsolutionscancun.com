from datetime import datetime

from sqlalchemy.orm import Session

from app.models.lead import Lead
from app.models.service_order import ServiceOrder
from app.models.user import User


OS_STATUS_BY_PIPELINE = {
    "NOVO LEAD": "ABERTA",
    "ATENDIMENTO": "EM_ATENDIMENTO",
    "TENTATIVA DE CONTATO": "AGENDADA",
    "VISITA": "EM_DIAGNOSTICO",
    "MONTAGEM DE PASTA": "COTIZACAO_ENVIADA",
    "VENDA GANHA": "APROVADA",
    "PERDIDO": "CANCELADA",
}


def service_order_number(order: ServiceOrder, *, opened_at: datetime | None = None) -> str:
    year = (opened_at or order.opened_at or order.created_at or datetime.utcnow()).year
    return f"TS-{year}-{order.id:06d}"


def supervisor_id_for_responsible(db: Session, responsible_user_id: int | None, actor: User | None = None) -> int | None:
    if not responsible_user_id:
        return actor.id if actor and actor.role == "GERENTE" else None

    responsible = db.query(User).filter(User.id == responsible_user_id).first()
    if not responsible:
        return actor.id if actor and actor.role == "GERENTE" else None
    if responsible.role == "GERENTE":
        return responsible.id
    if responsible.manager_id:
        return responsible.manager_id
    return actor.id if actor and actor.role == "GERENTE" else None


def ensure_service_order(db: Session, lead: Lead, *, actor: User | None = None) -> ServiceOrder:
    service_order = db.query(ServiceOrder).filter(ServiceOrder.lead_id == lead.id).first()
    if service_order:
        sync_service_order_from_lead(db, service_order, lead, actor=actor)
        lead.service_order = service_order
        return service_order

    opened_at = lead.created_at or datetime.utcnow()
    service_order = ServiceOrder(
        lead_id=lead.id,
        property_id=lead.property_id,
        status=OS_STATUS_BY_PIPELINE.get(lead.pipeline or "", "ABERTA"),
        warranty_days=90,
        opened_at=opened_at,
        scheduled_at=lead.proximo_contacto,
        completed_at=lead.updated_at if lead.pipeline == "VENDA GANHA" else None,
        responsible_user_id=lead.assigned_to_user_id,
        supervisor_user_id=supervisor_id_for_responsible(db, lead.assigned_to_user_id, actor),
    )
    db.add(service_order)
    db.flush()
    service_order.order_number = service_order_number(service_order, opened_at=opened_at)
    lead.service_order = service_order
    return service_order


def sync_service_order_from_lead(
    db: Session,
    service_order: ServiceOrder,
    lead: Lead,
    *,
    actor: User | None = None,
) -> ServiceOrder:
    service_order.property_id = lead.property_id
    service_order.status = OS_STATUS_BY_PIPELINE.get(lead.pipeline or "", service_order.status or "ABERTA")
    service_order.scheduled_at = lead.proximo_contacto
    service_order.responsible_user_id = lead.assigned_to_user_id
    service_order.supervisor_user_id = supervisor_id_for_responsible(db, lead.assigned_to_user_id, actor)
    if lead.pipeline == "VENDA GANHA" and not service_order.completed_at:
        service_order.completed_at = lead.updated_at or datetime.utcnow()
    if lead.pipeline != "VENDA GANHA":
        service_order.completed_at = None
    service_order.updated_at = datetime.utcnow()
    return service_order
