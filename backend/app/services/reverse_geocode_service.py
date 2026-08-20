"""Small, server-side reverse geocoder for the public service form."""

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastapi import HTTPException


DEFAULT_REVERSE_GEOCODER_URL = "https://nominatim.openstreetmap.org/reverse"


def _validate_coordinates(latitude: float, longitude: float) -> tuple[float, float]:
    try:
        lat = float(latitude)
        lng = float(longitude)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Coordenadas invalidas") from exc
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise HTTPException(status_code=400, detail="Coordenadas invalidas")
    return lat, lng


def _first(address: dict, *keys: str) -> str | None:
    for key in keys:
        value = str(address.get(key) or "").strip()
        if value:
            return value
    return None


def _normalize_result(payload: dict) -> dict:
    address = payload.get("address") or {}
    road = _first(address, "road", "pedestrian", "footway", "street")
    house_number = _first(address, "house_number")
    address_line1 = " ".join(part for part in (road, house_number) if part) or None
    return {
        "address_line1": address_line1,
        "district": _first(address, "neighbourhood", "suburb", "quarter", "city_district"),
        "locality": _first(address, "city", "town", "village", "municipality"),
        "administrative_area": _first(address, "state", "region"),
        "postal_code": _first(address, "postcode"),
        "country_code": (_first(address, "country_code") or "").upper() or None,
    }


def reverse_geocode(latitude: float, longitude: float) -> dict:
    lat, lng = _validate_coordinates(latitude, longitude)
    endpoint = os.getenv("REVERSE_GEOCODER_URL", DEFAULT_REVERSE_GEOCODER_URL)
    query = urlencode({"lat": f"{lat:.7f}", "lon": f"{lng:.7f}", "format": "jsonv2", "addressdetails": "1"})
    request = Request(
        f"{endpoint}?{query}",
        headers={"Accept": "application/json", "User-Agent": "TotalSolutionsCRM/1.0"},
    )
    try:
        with urlopen(request, timeout=5) as response:  # nosec B310 - endpoint is configuration-controlled.
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="No fue posible consultar la direccion") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="Respuesta de direccion invalida")
    return _normalize_result(payload)
