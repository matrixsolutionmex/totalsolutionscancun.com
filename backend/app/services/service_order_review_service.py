from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.service_order import ServiceOrder
from app.models.service_order_completion import ServiceOrderCustomerAcceptance
from app.models.service_order_review import ServiceOrderReview
from app.models.service_order_warranty_claim import ServiceOrderWarrantyClaim
from app.models.user import User
from app.services.service_order_quote_service import scoped_order

COMPLETED_STATUSES = {"COMPLETED", "CONCLUIDA", "FINALIZADO", "FINALIZADA"}
VISIBLE_REVIEW_STATUSES = {"VISIBLE"}


def calculate_nps(scores: list[Any] | tuple[Any, ...]) -> float | None:
    """Calculate the standard NPS score from 0-10 recommendation answers."""
    values = [int(value) for value in scores if value is not None]
    if not values:
        return None
    promoters = sum(value >= 9 for value in values)
    detractors = sum(value <= 6 for value in values)
    return round((promoters - detractors) * 100 / len(values), 2)


def nps_summary(scores: list[Any] | tuple[Any, ...]) -> dict[str, Any]:
    values = [int(value) for value in scores if value is not None]
    promoters = sum(value >= 9 for value in values)
    passives = sum(7 <= value <= 8 for value in values)
    detractors = sum(value <= 6 for value in values)
    return {
        "nps": calculate_nps(values),
        "average_recommendation_score": round(sum(values) / len(values), 2) if values else None,
        "nps_responses": len(values),
        "nps_promoters": promoters,
        "nps_passives": passives,
        "nps_detractors": detractors,
    }


def _validate_rating(value: Any, field: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"{field} deve ser uma nota de 1 a 5") from exc
    if number < 1 or number > 5:
        raise HTTPException(status_code=422, detail=f"{field} deve ser uma nota de 1 a 5")
    return number


def _validate_nps(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="NPS deve ser uma nota de 0 a 10") from exc
    if number < 0 or number > 10:
        raise HTTPException(status_code=422, detail="NPS deve ser uma nota de 0 a 10")
    return number


def _assert_review_eligible(db: Session, order: ServiceOrder):
    if order.status not in COMPLETED_STATUSES:
        raise HTTPException(status_code=409, detail="A avaliação só está disponível após a conclusão")
    acceptance = db.query(ServiceOrderCustomerAcceptance).filter_by(
        service_order_id=order.id, organization_id=order.organization_id, status="ACCEPTED"
    ).first()
    if not acceptance:
        raise HTTPException(status_code=409, detail="O aceite do cliente é necessário antes da avaliação")
    return acceptance


def _technician_id(order: ServiceOrder) -> int | None:
    responsible = getattr(order, "responsible_user", None)
    if responsible and responsible.role in {"BROKER", "TECNICO", "TÉCNICO"}:
        return responsible.id
    return order.responsible_user_id


def public_review_projection(db: Session, order: ServiceOrder) -> dict[str, Any]:
    """Expose only review state and eligibility through the tracking token."""
    existing = get_review(db, order)
    if existing:
        return review_payload(existing, public=True)
    eligible = order.status in COMPLETED_STATUSES and db.query(ServiceOrderCustomerAcceptance).filter_by(
        service_order_id=order.id,
        organization_id=order.organization_id,
        status="ACCEPTED",
    ).first() is not None
    return {"eligible": eligible, "submitted": False}


def review_payload(row: ServiceOrderReview | None, *, public: bool = False) -> dict[str, Any]:
    if not row:
        return {"submitted": False}
    result = {
        "submitted": True,
        "overall_rating": row.overall_rating,
        "service_quality_rating": row.service_quality_rating,
        "punctuality_rating": row.punctuality_rating,
        "communication_rating": row.communication_rating,
        "nps_score": row.nps_score,
        "comment": row.comment,
        "created_at": row.created_at,
    }
    if not public:
        result.update({"id": row.id, "organization_id": row.organization_id, "service_order_id": row.service_order_id, "customer_lead_id": row.customer_lead_id, "technician_user_id": row.technician_user_id, "visibility": row.visibility, "moderation_reason": row.moderation_reason})
    return result


def get_review(db: Session, order: ServiceOrder) -> ServiceOrderReview | None:
    return db.query(ServiceOrderReview).filter_by(
        service_order_id=order.id, organization_id=order.organization_id
    ).first()


def submit_public_review(db: Session, order: ServiceOrder, payload: dict[str, Any]) -> ServiceOrderReview:
    _assert_review_eligible(db, order)
    existing = get_review(db, order)
    if existing:
        return existing
    comment = (payload.get("comment") or "").strip() or None
    if comment and len(comment) > 2000:
        raise HTTPException(status_code=422, detail="Comentário muito longo")
    row = ServiceOrderReview(
        organization_id=order.organization_id,
        service_order_id=order.id,
        customer_lead_id=order.lead_id,
        technician_user_id=_technician_id(order),
        overall_rating=_validate_rating(payload.get("overall_rating"), "overall_rating"),
        service_quality_rating=_validate_rating(payload.get("service_quality_rating"), "service_quality_rating"),
        punctuality_rating=_validate_rating(payload.get("punctuality_rating"), "punctuality_rating"),
        communication_rating=_validate_rating(payload.get("communication_rating"), "communication_rating"),
        nps_score=_validate_nps(payload.get("nps_score")),
        comment=comment,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        existing = get_review(db, order)
        if existing:
            return existing
        raise
    return row


def _visible_reviews(db: Session, organization_id: int, technician_id: int | None = None):
    query = db.query(ServiceOrderReview).filter(
        ServiceOrderReview.organization_id == organization_id,
        ServiceOrderReview.visibility.in_(VISIBLE_REVIEW_STATUSES),
    )
    if technician_id is not None:
        query = query.filter(ServiceOrderReview.technician_user_id == technician_id)
    return query


def _aggregate(db: Session, organization_id: int, technician_id: int | None = None) -> dict[str, Any]:
    reviews = _visible_reviews(db, organization_id, technician_id)
    values = reviews.with_entities(
        func.count(ServiceOrderReview.id),
        func.avg(ServiceOrderReview.overall_rating),
        func.avg(ServiceOrderReview.service_quality_rating),
        func.avg(ServiceOrderReview.punctuality_rating),
        func.avg(ServiceOrderReview.communication_rating),
        func.avg(ServiceOrderReview.nps_score),
    ).one()
    nps_values = [value for (value,) in reviews.with_entities(ServiceOrderReview.nps_score).all() if value is not None]
    distribution = dict(
        reviews.with_entities(ServiceOrderReview.overall_rating, func.count(ServiceOrderReview.id))
        .group_by(ServiceOrderReview.overall_rating).all()
    )
    completed = db.query(func.count(ServiceOrder.id)).filter(
        ServiceOrder.organization_id == organization_id,
        ServiceOrder.status.in_(COMPLETED_STATUSES),
    )
    if technician_id is not None:
        completed = completed.filter(ServiceOrder.responsible_user_id == technician_id)
    completed_count = completed.scalar() or 0
    completed_orders = db.query(ServiceOrder.id).filter(
        ServiceOrder.organization_id == organization_id,
        ServiceOrder.status.in_(COMPLETED_STATUSES),
    )
    if technician_id is not None:
        completed_orders = completed_orders.filter(ServiceOrder.responsible_user_id == technician_id)
    claim_query = db.query(ServiceOrderWarrantyClaim).filter(
        ServiceOrderWarrantyClaim.organization_id == organization_id,
        ServiceOrderWarrantyClaim.service_order_id.in_(select(completed_orders.subquery().c.id)),
    )
    claim_count = claim_query.count()
    claim_service_count = claim_query.with_entities(
        func.count(func.distinct(ServiceOrderWarrantyClaim.service_order_id))
    ).scalar() or 0
    nps = nps_summary(nps_values)
    return {
        "services_completed": completed_count,
        "reviews_total": int(values[0] or 0),
        "average_rating": round(float(values[1]), 2) if values[1] is not None else None,
        "average_service_quality": round(float(values[2]), 2) if values[2] is not None else None,
        "average_punctuality": round(float(values[3]), 2) if values[3] is not None else None,
        "average_communication": round(float(values[4]), 2) if values[4] is not None else None,
        **nps,
        # Kept as a compatibility alias; average_recommendation_score is explicit.
        "average_nps": round(float(values[5]), 2) if values[5] is not None else None,
        "rating_distribution": {str(key): int(value) for key, value in distribution.items()},
        "warranty_claims": int(claim_count),
        "warranty_claim_count": int(claim_count),
        "warranty_claim_service_count": int(claim_service_count),
        "warranty_claim_rate": round((claim_service_count / completed_count) * 100, 2) if completed_count else None,
    }


def quality_projection(db: Session, actor) -> dict[str, Any]:
    if actor.role == "ROOT":
        organization_id = actor.organization_id
    else:
        organization_id = actor.organization_id
    if not organization_id:
        return {"organization": _aggregate(db, organization_id), "technicians": []}
    if actor.role in {"BROKER", "TECNICO", "TÉCNICO"}:
        return {"organization": _aggregate(db, organization_id, actor.id), "technicians": []}
    technicians = db.query(User).filter(
        User.organization_id == organization_id,
        User.role.in_(["BROKER", "TECNICO", "TÉCNICO"]),
    )
    if actor.role == "SUPERVISOR":
        technicians = technicians.filter(User.manager_id == actor.id)
    return {
        "organization": _aggregate(db, organization_id),
        "technicians": [
            {"display_name": tech.full_name or tech.username, "metrics": _aggregate(db, organization_id, tech.id)}
            for tech in technicians.order_by(User.full_name, User.id).all()
        ],
    }


def get_scoped_review(db: Session, order_id: int, actor) -> dict[str, Any]:
    order = scoped_order(db, order_id, actor)
    return review_payload(get_review(db, order))
