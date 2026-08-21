"""Deterministic, auditable preliminary pricing for the Cancun service area."""

from decimal import Decimal, InvalidOperation
import json
import re

from sqlalchemy import and_, or_
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models.pricing_rate import PricingRate


PRICING_VERSION = "CANCUN_V1"
PRICING_CURRENCY = "MXN"
URGENCY_MULTIPLIERS = {"LOW": Decimal("1.00"), "NORMAL": Decimal("1.00"), "HIGH": Decimal("1.25"), "EMERGENCY": Decimal("1.60")}
SERVICE_TYPES = {
    "plomeria": "PLUMBING", "plomería": "PLUMBING", "hidraulica": "PLUMBING", "hidráulica": "PLUMBING", "plumbing": "PLUMBING",
    "electricidad": "ELECTRICAL", "electrical": "ELECTRICAL",
    "aire acondicionado": "AIR_CONDITIONING", "aire acondicionado y climatizacion": "AIR_CONDITIONING",
    "climatizacion": "AIR_CONDITIONING", "a/c": "AIR_CONDITIONING", "ac": "AIR_CONDITIONING",
    "pintura": "PAINTING", "painting": "PAINTING", "drywall": "DRYWALL", "tablaroca": "DRYWALL",
    "cerrajeria": "LOCKSMITH", "cerrajería": "LOCKSMITH", "locksmith": "LOCKSMITH",
    "carpinteria": "CARPENTRY", "carpintería": "CARPENTRY", "carpentry": "CARPENTRY",
    "fumigacion": "PEST_CONTROL", "fumigación": "PEST_CONTROL", "pest control": "PEST_CONTROL",
    "alberca": "POOL", "pool": "POOL", "piscina": "POOL",
}
DEFAULT_RATES = {
    "PLUMBING": (Decimal("450"), Decimal("700")), "ELECTRICAL": (Decimal("450"), Decimal("700")),
    "AIR_CONDITIONING": (Decimal("450"), Decimal("700")), "PAINTING": (Decimal("350"), Decimal("600")),
    "DRYWALL": (Decimal("450"), Decimal("750")), "LOCKSMITH": (Decimal("350"), Decimal("550")),
    "CARPENTRY": (Decimal("400"), Decimal("650")), "PEST_CONTROL": (Decimal("400"), Decimal("650")),
    "POOL": (Decimal("450"), Decimal("800")),
}
ZONE_SURCHARGES = {"Z0": Decimal("0"), "Z1": Decimal("100"), "Z2": Decimal("200")}


def _money(value, *, field="budget"):
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value).replace(",", ".")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} invalido") from exc
    if number < 0:
        raise ValueError(f"{field} invalido")
    return number


def normalize_service_type(value) -> str:
    raw = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return SERVICE_TYPES.get(raw, re.sub(r"[^A-Z0-9]+", "_", raw.upper()).strip("_") or "UNKNOWN")


def normalize_segment(value) -> str:
    raw = str(value or "").strip().lower()
    return "COMMERCIAL" if any(term in raw for term in ("commercial", "hotel", "airbnb", "empresa", "negocio")) else "RESIDENTIAL"


def normalize_urgency(value) -> str:
    raw = str(value or "NORMAL").strip().lower()
    if raw in {"baixa", "baja", "low", "programada"}:
        return "LOW"
    if raw in {"alta", "high"}:
        return "HIGH"
    if raw in {"emergencia", "emergência", "emergency", "urgente", "urgent"}:
        return "EMERGENCY"
    return "NORMAL"


def resolve_pricing_zone(location: dict | None) -> str:
    location = location or {}
    explicit = str(location.get("pricing_zone") or "").strip().upper()
    if explicit in ZONE_SURCHARGES:
        return explicit
    text = " ".join(str(location.get(key) or "") for key in ("address_line1", "district", "locality", "city")).lower()
    if any(term in text for term in ("bonfil", "periferia", "zona hotelera distante")):
        return "Z2"
    if any(term in text for term in ("puerto cancun", "puerto canción", "zona hotelera", "zona hotelera inicial")):
        return "Z1"
    locality = str(location.get("locality") or location.get("city") or "cancun").lower()
    return "Z0" if "cancun" in locality or "cancún" in locality else "OUT_OF_SCOPE"


def seed_default_pricing_rates(db: Session) -> int:
    created = 0
    for service_type, (residential, commercial) in DEFAULT_RATES.items():
        for segment, base in (("RESIDENTIAL", residential), ("COMMERCIAL", commercial)):
            for zone, surcharge in ZONE_SURCHARGES.items():
                exists = db.query(PricingRate).filter_by(
                    organization_id=None, country="MX", city="CANCUN", service_type=service_type,
                    segment=segment, pricing_zone=zone, pricing_version=PRICING_VERSION,
                ).first()
                if not exists:
                    db.add(PricingRate(service_type=service_type, segment=segment, pricing_zone=zone,
                                       visit_base_price=base, travel_surcharge=surcharge,
                                       pricing_version=PRICING_VERSION))
                    created += 1
    if created:
        db.flush()
    return created


def _find_rate(db, service_type, segment, zone, organization_id=None):
    query = db.query(PricingRate).filter(
        PricingRate.country == "MX", PricingRate.city == "CANCUN", PricingRate.service_type == service_type,
        PricingRate.segment == segment, PricingRate.pricing_zone == zone, PricingRate.active.is_(True),
    ).filter(or_(PricingRate.organization_id == organization_id, PricingRate.organization_id.is_(None)))
    try:
        rows = query.order_by(PricingRate.organization_id.is_(None)).all()
    except OperationalError:
        return "__PRICING_TABLE_MISSING__"
    return rows[0] if rows else None


def calculate_preliminary_pricing(db: Session, service_type, segment=None, location=None, urgency="NORMAL",
                                  apparent_complexity=None, customer_budget_min=None, customer_budget_max=None,
                                  organization_id=None) -> dict:
    normalized_service = normalize_service_type(service_type)
    normalized_segment = normalize_segment(segment)
    normalized_urgency = normalize_urgency(urgency)
    zone = resolve_pricing_zone(location)
    budget_min = _money(customer_budget_min, field="budget_min")
    budget_max = _money(customer_budget_max, field="budget_max")
    if budget_min is not None and budget_max is not None and budget_min > budget_max:
        raise ValueError("budget_min no puede ser mayor que budget_max")
    result = {
        "pricing_version": PRICING_VERSION, "pricing_currency": PRICING_CURRENCY,
        "service_type": normalized_service, "segment": normalized_segment, "pricing_zone": zone,
        "urgency_level": normalized_urgency, "urgency_multiplier": float(URGENCY_MULTIPLIERS[normalized_urgency]),
        "customer_budget_min": float(budget_min) if budget_min is not None else None,
        "customer_budget_max": float(budget_max) if budget_max is not None else None,
        "visit_credit_policy": "APPLIED_AFTER_APPROVED_VISIT", "estimate_available": False,
        "requires_diagnosis": True, "visit_base_price": None, "travel_surcharge": None,
        "visit_calculated_price": None, "market_reference_min": None, "market_reference_max": None,
        "pricing_distance_km": location.get("distance_km") if location else None,
        "pricing_duration_minutes": location.get("duration_minutes") if location else None,
        "unavailable_reason": None,
    }
    rate = _find_rate(db, normalized_service, normalized_segment, zone, organization_id) if zone != "OUT_OF_SCOPE" else None
    if rate == "__PRICING_TABLE_MISSING__":
        if normalized_service not in DEFAULT_RATES:
            result["unavailable_reason"] = "service_or_area_not_configured"
            return result
        base = DEFAULT_RATES[normalized_service][1 if normalized_segment == "COMMERCIAL" else 0]
        surcharge = ZONE_SURCHARGES[zone]
    elif not rate:
        result["unavailable_reason"] = "service_or_area_not_configured"
        return result
    else:
        base = Decimal(rate.visit_base_price)
        surcharge = Decimal(rate.travel_surcharge or 0)
    total = ((base + surcharge) * URGENCY_MULTIPLIERS[normalized_urgency]).quantize(Decimal("0.01"))
    result.update({"visit_base_price": float(base), "travel_surcharge": float(surcharge), "visit_calculated_price": float(total)})
    return result


def pricing_snapshot(result: dict) -> str:
    return json.dumps(result, ensure_ascii=True, sort_keys=True)
