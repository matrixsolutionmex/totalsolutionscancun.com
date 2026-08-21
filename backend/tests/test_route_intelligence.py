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


def test_route_adapter_rejects_invalid_coordinates_without_provider_call(monkeypatch):
    monkeypatch.setenv("ROUTING_PROVIDER_URL", "https://routing.test")
    called = []
    monkeypatch.setattr(routing.httpx, "get", lambda *args, **kwargs: called.append(True))

    result = routing.calculate_route(float("nan"), -86.8, 21.2, -86.8)

    assert result["available"] is False
    assert result["reason"] == "invalid_coordinates"
    assert called == []
