"""Small, provider-neutral road routing adapter with bounded in-process cache."""

import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx


ROUTE_RECALC_DISTANCE_M = 100
ROUTE_RECALC_INTERVAL_S = 60
ROUTE_TIMEOUT_S = 4.0
_cache = {}
_cache_lock = threading.Lock()


def _valid_point(latitude, longitude):
    try:
        lat = float(latitude)
        lng = float(longitude)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(lat) or not math.isfinite(lng) or not -90 <= lat <= 90 or not -180 <= lng <= 180:
        return None
    return lat, lng


def _distance_m(first, second):
    if not first or not second:
        return float("inf")
    lat1, lng1 = map(math.radians, first)
    lat2, lng2 = map(math.radians, second)
    value = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 6371000 * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def _unavailable(reason="provider_unavailable"):
    return {
        "available": False,
        "distance_m": None,
        "duration_s": None,
        "eta_at": None,
        "geometry": None,
        "reason": reason,
    }


def _cached_result(cache_key, origin, destination):
    if not cache_key:
        return None
    with _cache_lock:
        item = _cache.get(cache_key)
    if not item or time.monotonic() - item["created_monotonic"] > ROUTE_RECALC_INTERVAL_S:
        return None
    if _distance_m(item["origin"], origin) >= ROUTE_RECALC_DISTANCE_M:
        return None
    return dict(item["result"])


def calculate_route(origin_lat, origin_lng, destination_lat, destination_lng, *, cache_key=None):
    origin = _valid_point(origin_lat, origin_lng)
    destination = _valid_point(destination_lat, destination_lng)
    if not origin or not destination:
        return _unavailable("invalid_coordinates")

    provider_url = os.getenv("ROUTING_PROVIDER_URL", "").strip().rstrip("/")
    if not provider_url:
        return _unavailable("provider_not_configured")
    cached = _cached_result(cache_key, origin, destination)
    if cached:
        if cached.get("duration_s") is not None:
            cached["eta_at"] = (datetime.now(timezone.utc) + timedelta(seconds=float(cached["duration_s"]))).isoformat()
        return cached

    url = f"{provider_url}/route/v1/driving/{origin[1]},{origin[0]};{destination[1]},{destination[0]}"
    params = {"overview": "full", "geometries": "geojson", "steps": "false"}
    try:
        response = httpx.get(url, params=params, timeout=float(os.getenv("ROUTING_TIMEOUT_SECONDS", ROUTE_TIMEOUT_S)))
        response.raise_for_status()
        payload = response.json()
        route = (payload.get("routes") or [None])[0]
        coordinates = ((route or {}).get("geometry") or {}).get("coordinates") or []
        distance_m = float((route or {}).get("distance"))
        duration_s = float((route or {}).get("duration"))
        geometry = [[float(lat), float(lng)] for lng, lat in coordinates]
        if not geometry or not math.isfinite(distance_m) or distance_m <= 0 or not math.isfinite(duration_s) or duration_s <= 0:
            return _unavailable("invalid_provider_response")
    except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError):
        return _unavailable("provider_error")

    result = {
        "available": True,
        "distance_m": round(distance_m, 1),
        "duration_s": round(duration_s, 1),
        "eta_at": (datetime.now(timezone.utc) + timedelta(seconds=duration_s)).isoformat(),
        "geometry": geometry,
    }
    if cache_key:
        with _cache_lock:
            _cache[cache_key] = {
                "created_monotonic": time.monotonic(),
                "origin": origin,
                "result": result,
            }
    return dict(result)


def clear_route_cache():
    with _cache_lock:
        _cache.clear()
