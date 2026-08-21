"""Derived health for an active route and its last GPS sample."""

from datetime import datetime


TRACKING_LIVE_THRESHOLD_SECONDS = 30
TRACKING_STALE_THRESHOLD_SECONDS = 120


def _age_seconds(value: datetime | None, now: datetime) -> int | None:
    if not value:
        return None
    return max(0, int((now - value).total_seconds()))


def _health_for_age(age_seconds: int | None, *, active: bool) -> str:
    if not active:
        return "OFFLINE"
    if age_seconds is None:
        return "STALE"
    if age_seconds <= TRACKING_LIVE_THRESHOLD_SECONDS:
        return "LIVE"
    if age_seconds <= TRACKING_STALE_THRESHOLD_SECONDS:
        return "STALE"
    return "OFFLINE"


def tracking_health(tracking, now: datetime | None = None) -> dict:
    """Keep session heartbeat and GPS freshness distinct."""
    if not tracking:
        return {
            "tracking_health": "OFFLINE",
            "location_health": "OFFLINE",
            "heartbeat_health": "OFFLINE",
            "last_location_at": None,
            "seconds_since_last_update": None,
            "last_heartbeat_at": None,
            "seconds_since_last_heartbeat": None,
        }
    now = now or datetime.utcnow()
    has_location = tracking.current_lat is not None and tracking.current_lng is not None
    last_location_at = tracking.updated_at if has_location else None
    location_age = _age_seconds(last_location_at, now)
    heartbeat_age = _age_seconds(tracking.last_heartbeat_at or tracking.started_at, now)
    location_health = _health_for_age(location_age, active=bool(tracking.tracking_active))
    heartbeat_health = _health_for_age(heartbeat_age, active=bool(tracking.tracking_active))
    return {
        "tracking_health": location_health,
        "location_health": location_health,
        "heartbeat_health": heartbeat_health,
        "last_location_at": last_location_at,
        "seconds_since_last_update": location_age,
        "last_heartbeat_at": tracking.last_heartbeat_at,
        "seconds_since_last_heartbeat": heartbeat_age,
    }
