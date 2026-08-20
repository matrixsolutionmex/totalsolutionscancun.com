"""Validation and normalization for service-location coordinates."""

import math
from datetime import datetime

from fastapi import HTTPException


LOCATION_SOURCES = {"MAP_PIN", "DEVICE_GPS"}


def normalize_service_location(latitude, longitude, accuracy=None, source=None, *, confirmed=False):
    has_latitude = latitude not in (None, "")
    has_longitude = longitude not in (None, "")
    has_any = has_latitude or has_longitude or accuracy not in (None, "") or source not in (None, "")
    if not has_any:
        return {"location_lat": None, "location_lng": None, "location_accuracy_m": None,
                "location_source": None, "location_confirmed_at": None}
    if not has_latitude or not has_longitude:
        raise HTTPException(status_code=400, detail="Latitude e longitude devem ser informadas juntas")
    try:
        lat = float(latitude)
        lng = float(longitude)
        accuracy_value = None if accuracy in (None, "") else float(accuracy)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Coordenadas invalidas") from exc
    if not math.isfinite(lat) or not math.isfinite(lng) or (accuracy_value is not None and not math.isfinite(accuracy_value)):
        raise HTTPException(status_code=400, detail="Coordenadas invalidas")
    if not -90 <= lat <= 90:
        raise HTTPException(status_code=400, detail="Latitude invalida")
    if not -180 <= lng <= 180:
        raise HTTPException(status_code=400, detail="Longitude invalida")
    if accuracy_value is not None and accuracy_value < 0:
        raise HTTPException(status_code=400, detail="Precisao invalida")
    normalized_source = str(source or "MAP_PIN").strip().upper()
    if normalized_source not in LOCATION_SOURCES:
        raise HTTPException(status_code=400, detail="Origem da localizacao invalida")
    return {
        "location_lat": lat,
        "location_lng": lng,
        "location_accuracy_m": accuracy_value,
        "location_source": normalized_source,
        "location_confirmed_at": datetime.utcnow() if confirmed else None,
    }
