from datetime import datetime, timezone

from app.services import route_intelligence_service as routing


class FakeResponse:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        return self.payload


def test_route_adapter_normalizes_osrm_route_and_caches_by_movement(monkeypatch):
    calls = []
    monkeypatch.setenv("ROUTING_PROVIDER_URL", "https://routing.test")
    routing.clear_route_cache()

    def fake_get(url, params, timeout):
        calls.append((url, params, timeout))
        return FakeResponse({
            "code": "Ok",
            "routes": [{
                "distance": 950,
                "duration": 480,
                "geometry": {"coordinates": [[-86.8450, 21.1550], [-86.8440, 21.1560]]},
            }],
        })

    monkeypatch.setattr(routing.httpx, "get", fake_get)
    result = routing.calculate_route(21.1550, -86.8450, 21.2, -86.8, cache_key="order:1")
    cached = routing.calculate_route(21.1551, -86.8451, 21.2, -86.8, cache_key="order:1")

    assert result["available"] is True
    assert result["distance_m"] == 950.0
    assert result["duration_s"] == 480.0
    assert datetime.fromisoformat(result["eta_at"]) > datetime.now(timezone.utc)
    assert result["geometry"] == [[21.155, -86.845], [21.156, -86.844]]
    assert cached["available"] is True
    assert cached["distance_m"] == result["distance_m"]
    assert cached["duration_s"] == result["duration_s"]
    assert len(calls) == 1


def test_route_adapter_returns_safe_fallback_when_provider_fails(monkeypatch):
    monkeypatch.setenv("ROUTING_PROVIDER_URL", "https://routing.test")
    routing.clear_route_cache()
    monkeypatch.setattr(routing.httpx, "get", lambda *args, **kwargs: (_ for _ in ()).throw(routing.httpx.ReadTimeout("timeout")))

    result = routing.calculate_route(21.1550, -86.8450, 21.2, -86.8, cache_key="order:failure")

    assert result["available"] is False
    assert result["distance_m"] is None
    assert result["duration_s"] is None
    assert result["eta_at"] is None
    assert result["geometry"] is None
    assert "provider" in result["reason"]


def test_route_adapter_preserves_recent_route_when_recalculation_fails(monkeypatch):
    monkeypatch.setenv("ROUTING_PROVIDER_URL", "https://routing.test")
    routing.clear_route_cache()
    responses = [
        FakeResponse({
            "code": "Ok",
            "routes": [{
                "distance": 1200,
                "duration": 600,
                "geometry": {"coordinates": [[-86.8450, 21.1550], [-86.8440, 21.1560]]},
            }],
        }),
        routing.httpx.ReadTimeout("temporary timeout"),
    ]

    def fake_get(*args, **kwargs):
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(routing.httpx, "get", fake_get)
    first = routing.calculate_route(21.1550, -86.8450, 21.2, -86.8, cache_key="order:resilient")
    routing._cache["order:resilient"]["created_monotonic"] -= routing.ROUTE_RECALC_INTERVAL_S + 1
    second = routing.calculate_route(21.1570, -86.8430, 21.2, -86.8, cache_key="order:resilient")

    assert first["available"] is True
    assert second["available"] is True
    assert second["stale"] is True
    assert second["reason"] == "provider_transient"
    assert second["geometry"] == first["geometry"]
    assert second["distance_m"] == first["distance_m"]
    assert second["duration_s"] == first["duration_s"]


def test_route_adapter_rejects_invalid_coordinates_without_provider_call(monkeypatch):
    monkeypatch.setenv("ROUTING_PROVIDER_URL", "https://routing.test")
    called = []
    monkeypatch.setattr(routing.httpx, "get", lambda *args, **kwargs: called.append(True))

    result = routing.calculate_route(float("nan"), -86.8, 21.2, -86.8)

    assert result["available"] is False
    assert result["reason"] == "invalid_coordinates"
    assert called == []
