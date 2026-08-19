"""Short-lived, actor-scoped location sessions for Pablo."""

from datetime import datetime, timedelta
from threading import RLock
from uuid import uuid4

from fastapi import HTTPException

from app.models.user import User

LOCATION_TTL = timedelta(minutes=15)
_sessions: dict[tuple[int | None, int], dict] = {}
_lock = RLock()

def _key(actor: User) -> tuple[int | None, int]:
    return actor.organization_id, actor.id

def _cleanup(now: datetime | None = None) -> None:
    current = now or datetime.utcnow()
    for key, session in list(_sessions.items()):
        if session["expires_at"] <= current:
            _sessions.pop(key, None)

def create_location_session(actor: User, latitude: float, longitude: float, accuracy: float | None) -> dict:
    now = datetime.utcnow()
    session = {"location_id": uuid4().hex, "organization_id": actor.organization_id, "user_id": actor.id,
               "latitude": latitude, "longitude": longitude, "accuracy": accuracy,
               "created_at": now, "expires_at": now + LOCATION_TTL}
    with _lock:
        _cleanup(now)
        _sessions[_key(actor)] = session
    return public_location(session)

def get_active_location(actor: User, location_id: str | None = None) -> dict | None:
    with _lock:
        _cleanup()
        session = _sessions.get(_key(actor))
        if not session or (location_id and session["location_id"] != location_id):
            return None
        return dict(session)

def discard_location(actor: User) -> bool:
    with _lock:
        return _sessions.pop(_key(actor), None) is not None

def expire_location_for_test(actor: User) -> None:
    with _lock:
        session = _sessions.get(_key(actor))
        if session:
            session["expires_at"] = datetime.utcnow() - timedelta(seconds=1)

def public_location(session: dict | None) -> dict | None:
    if not session:
        return None
    accuracy = session.get("accuracy")
    return {"location_id": session["location_id"], "status": "AVAILABLE",
            "display": {"title": "LOCALIZAÇÃO RECEBIDA",
                        "accuracy_meters": round(accuracy, 1) if accuracy is not None else None,
                        "expires_at": session["expires_at"].isoformat()},
            "available_actions": ["USE_ON_CLIENT", "USE_ON_SERVICE_ORDER", "DISCARD"]}

def coordinates_for_action(actor: User, location_id: str | None = None) -> dict:
    session = get_active_location(actor, location_id)
    if not session:
        raise HTTPException(status_code=410, detail="A sessão de localização expirou")
    return session
