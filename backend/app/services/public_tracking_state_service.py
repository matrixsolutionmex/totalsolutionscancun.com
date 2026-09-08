"""Canonical public state for a service-order tracking page."""


PUBLIC_TRACKING_LABELS = {
    "es": {
        "REQUEST_RECEIVED": "Solicitud recibida",
        "VISIT_PAYMENT_PENDING": "Pago de visita pendiente",
        "VISIT_PAID": "Visita pagada",
        "TECHNICIAN_ASSIGNED": "Técnico asignado",
        "TECHNICIAN_EN_ROUTE": "Técnico en camino",
        "TECHNICIAN_ARRIVED": "Técnico llegó",
        "DIAGNOSIS_IN_PROGRESS": "Diagnóstico en progreso",
        "QUOTE_PENDING": "Cotización pendiente",
        "QUOTE_SENT": "Cotización enviada",
        "QUOTE_APPROVED": "Cotización aprobada",
        "SERVICE_IN_PROGRESS": "Servicio en ejecución",
        "SERVICE_COMPLETED": "Servicio finalizado",
        "ROUTE_FINISHED": "Ruta finalizada",
        "CANCELLED": "Servicio cancelado",
    },
    "en": {
        "REQUEST_RECEIVED": "Request received",
        "VISIT_PAYMENT_PENDING": "Visit payment pending",
        "VISIT_PAID": "Visit paid",
        "TECHNICIAN_ASSIGNED": "Technician assigned",
        "TECHNICIAN_EN_ROUTE": "Technician on the way",
        "TECHNICIAN_ARRIVED": "Technician arrived",
        "DIAGNOSIS_IN_PROGRESS": "Diagnosis in progress",
        "QUOTE_PENDING": "Quote pending",
        "QUOTE_SENT": "Quote sent",
        "QUOTE_APPROVED": "Quote approved",
        "SERVICE_IN_PROGRESS": "Service in progress",
        "SERVICE_COMPLETED": "Service completed",
        "ROUTE_FINISHED": "Route finished",
        "CANCELLED": "Service cancelled",
    },
    "pt-BR": {
        "REQUEST_RECEIVED": "Solicitação recebida",
        "VISIT_PAYMENT_PENDING": "Pagamento da visita pendente",
        "VISIT_PAID": "Visita paga",
        "TECHNICIAN_ASSIGNED": "Técnico atribuído",
        "TECHNICIAN_EN_ROUTE": "Técnico a caminho",
        "TECHNICIAN_ARRIVED": "Técnico chegou",
        "DIAGNOSIS_IN_PROGRESS": "Diagnóstico em andamento",
        "QUOTE_PENDING": "Orçamento pendente",
        "QUOTE_SENT": "Orçamento enviado",
        "QUOTE_APPROVED": "Orçamento aprovado",
        "SERVICE_IN_PROGRESS": "Serviço em execução",
        "SERVICE_COMPLETED": "Serviço finalizado",
        "ROUTE_FINISHED": "Rota finalizada",
        "CANCELLED": "Serviço cancelado",
    },
}


def resolve_public_tracking_state(
    service_order,
    tracking=None,
    payment=None,
    diagnosis=None,
    quote=None,
    *,
    language="es",
) -> dict:
    """Resolve operational state without allowing payment or stale GPS to override it."""
    status = (getattr(service_order, "status", None) or "SALES_QUEUE").strip().upper()
    tracking_active = bool(tracking and tracking.tracking_active)
    stopped = bool(tracking and tracking.stopped_at)

    # EN_CAMINO is authoritative for the public operational state, including
    # legacy sessions that were stopped without changing the order status.
    if status == "EN_CAMINO" or tracking_active:
        code = "TECHNICIAN_EN_ROUTE"
        route_state = "EN_CAMINO"
        map_state = "ACTIVE" if tracking_active else "UNAVAILABLE"
    elif status in {"ARRIVED"}:
        code, route_state, map_state = "TECHNICIAN_ARRIVED", "ARRIVED", "AVAILABLE"
    elif status in {"COMPLETED", "CONCLUIDA", "FINALIZADA"}:
        code, route_state, map_state = "SERVICE_COMPLETED", "FINISHED", "FINISHED"
    elif status in {"CANCELLED", "CANCELADA"}:
        code, route_state, map_state = "CANCELLED", "FINISHED", "FINISHED"
    elif status in {"EM_ATENDIMENTO", "IN_PROGRESS"}:
        code, route_state, map_state = "SERVICE_IN_PROGRESS", "SERVICE", "AVAILABLE"
    elif status in {"ASSIGNED", "ACCEPTED"}:
        code, route_state, map_state = "TECHNICIAN_ASSIGNED", "NOT_STARTED", "UNAVAILABLE"
    else:
        code, route_state, map_state = "REQUEST_RECEIVED", "NOT_STARTED", "UNAVAILABLE"

    labels = PUBLIC_TRACKING_LABELS.get(language, PUBLIC_TRACKING_LABELS["es"])
    return {
        "code": code,
        "label": labels[code],
        "route_state": route_state,
        "map_state": map_state,
        "tracking_active": tracking_active,
        "payment_complementary": payment is not None,
        "diagnosis_present": diagnosis is not None,
        "quote_status": quote.get("status") if isinstance(quote, dict) else getattr(quote, "status", None),
        "stopped_session": stopped,
    }
