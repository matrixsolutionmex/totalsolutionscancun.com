"""Canonical interpretation of a service-order tracking session."""


def tracking_session_state(order, tracking) -> str:
    """Return ACTIVE, STOPPED, ORPHANED, or INACTIVE for a tracking session."""
    if not tracking:
        return "INACTIVE"
    active = bool(tracking.tracking_active)
    stopped = tracking.stopped_at is not None
    en_route = (getattr(order, "status", None) or "").strip().upper() == "EN_CAMINO"

    if active and not stopped and en_route:
        return "ACTIVE"
    if not active and stopped:
        return "STOPPED"
    if active or stopped or (not active and en_route):
        return "ORPHANED"
    return "INACTIVE"


def is_tracking_session_active(order, tracking) -> bool:
    return tracking_session_state(order, tracking) == "ACTIVE"
