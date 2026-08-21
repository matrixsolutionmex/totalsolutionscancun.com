from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.connection import Base
from app.models.organization import Organization
from app.models.pricing_rate import PricingRate
from app.services.pricing_engine_service import calculate_preliminary_pricing, seed_default_pricing_rates


def pricing_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine, tables=[Organization.__table__, PricingRate.__table__])
    return sessionmaker(bind=engine)()


def test_cancun_matrix_calculates_visit_and_urgency_without_osrm():
    db = pricing_db()
    try:
        seed_default_pricing_rates(db)
        normal = calculate_preliminary_pricing(db, "Electricidad", "residencial", {"locality": "Cancún"}, "NORMAL")
        high = calculate_preliminary_pricing(db, "Electricidad", "residencial", {"locality": "Cancún"}, "ALTA")
        emergency = calculate_preliminary_pricing(db, "Aire acondicionado", "hotel", {"district": "Zona Hotelera"}, "EMERGENCIA")
        assert normal["visit_calculated_price"] == 450.0
        assert high["visit_calculated_price"] == 562.5
        assert emergency["pricing_zone"] == "Z1"
        assert emergency["visit_calculated_price"] == 1280.0
        assert emergency["estimate_available"] is False
        assert emergency["requires_diagnosis"] is True
    finally:
        db.close()


def test_matrix_separates_residential_and_commercial_and_preserves_budget():
    db = pricing_db()
    try:
        seed_default_pricing_rates(db)
        result = calculate_preliminary_pricing(
            db, "plomeria", "commercial", {"locality": "Cancun", "pricing_zone": "Z2"},
            "NORMAL", customer_budget_min="1200", customer_budget_max="3000",
        )
        assert result["segment"] == "COMMERCIAL"
        assert result["visit_base_price"] == 700.0
        assert result["travel_surcharge"] == 200.0
        assert result["visit_calculated_price"] == 900.0
        assert result["customer_budget_min"] == 1200.0
        assert result["customer_budget_max"] == 3000.0
        assert result["market_reference_min"] is None
    finally:
        db.close()


def test_unknown_service_or_city_does_not_invent_price():
    db = pricing_db()
    try:
        seed_default_pricing_rates(db)
        outside = calculate_preliminary_pricing(db, "Electricidad", "residencial", {"locality": "Playa del Carmen"})
        unknown = calculate_preliminary_pricing(db, "Servicio no configurado", "residencial", {"locality": "Cancun"})
        assert outside["estimate_available"] is False
        assert outside["visit_calculated_price"] is None
        assert unknown["visit_calculated_price"] is None
    finally:
        db.close()


def test_organization_override_does_not_leak_between_organizations():
    db = pricing_db()
    try:
        seed_default_pricing_rates(db)
        first = Organization(name="One", slug="one")
        second = Organization(name="Two", slug="two")
        db.add_all([first, second])
        db.flush()
        db.add(PricingRate(organization_id=first.id, service_type="ELECTRICAL", segment="RESIDENTIAL",
                           pricing_zone="Z0", visit_base_price=Decimal("999"), travel_surcharge=0,
                           pricing_version="CANCUN_V1"))
        db.commit()
        own = calculate_preliminary_pricing(db, "electricidad", "residencial", {"locality": "Cancun"}, organization_id=first.id)
        other = calculate_preliminary_pricing(db, "electricidad", "residencial", {"locality": "Cancun"}, organization_id=second.id)
        assert own["visit_calculated_price"] == 999.0
        assert other["visit_calculated_price"] == 450.0
    finally:
        db.close()
