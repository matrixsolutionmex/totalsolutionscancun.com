from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.connection import Base
from app.models.organization import Organization
from app.models.pricing_rate import PricingRate
from app.models.service_opportunity import ServiceOpportunity
from app.models.service_order_tracking import ServiceOrderTracking  # noqa: F401 - registers ServiceOrder relationship
from app.services.marketplace_service import public_opportunity
from app.services.pricing_engine_service import calculate_preliminary_pricing, seed_default_pricing_rates


def pricing_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine, tables=[Organization.__table__, PricingRate.__table__, ServiceOpportunity.__table__])
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
        assert emergency["estimate_available"] is True
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
        assert result["market_reference_min"] == 650.0
        assert result["market_reference_max"] == 2800.0
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


def test_market_reference_matrix_examples_are_segmented_and_not_final_price():
    db = pricing_db()
    try:
        seed_default_pricing_rates(db)
        cases = [
            ("Plomería", "Casa", (450.0, 1800.0)),
            ("Electricidad", "Hotel", (700.0, 3000.0)),
            ("Aire acondicionado", "residencial", (900.0, 2200.0)),
            ("Limpieza técnica", "comercial", (900.0, 3500.0)),
        ]
        for service, segment, expected in cases:
            result = calculate_preliminary_pricing(db, service, segment, {"locality": "Cancun"}, "EMERGENCIA")
            assert (result["market_reference_min"], result["market_reference_max"]) == expected
            assert result["market_reference_max"] != result["visit_calculated_price"]
        unknown_segment = calculate_preliminary_pricing(db, "Electricidad", "segmento futuro", {"locality": "Cancun"})
        assert unknown_segment["segment"] == "RESIDENTIAL"
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


def test_marketplace_preserves_customer_budget_snapshot_and_keeps_reference_separate():
    db = pricing_db()
    try:
        opportunity = ServiceOpportunity(
            public_id="MKT-BUDGET-001", organization_id=1, service_type="PLUMBING", segment="RESIDENTIAL",
            customer_budget_min=1200, customer_budget_max=3000, pricing_currency="MXN",
            market_reference_min=None, market_reference_max=None, visit_calculated_price=450,
            pricing_zone="Z0", description_public="Fuga detectada.",
        )
        db.add(opportunity)
        db.commit()
        serialized = public_opportunity(opportunity)
        assert serialized["customer_budget_min"] == 1200
        assert serialized["customer_budget_max"] == 3000
        assert serialized["customer_budget_range"] == "1200.00 – 3000.00"
        assert serialized["market_reference_min"] is None
        assert serialized["estimate_available"] is False
        assert serialized["pricing_currency"] == "MXN"
    finally:
        db.close()


def test_customer_budget_shapes_are_serialized_without_mixing_reference():
    db = pricing_db()
    try:
        shapes = [(1200, None, "Desde 1200"), (None, 3000, "Hasta 3000"), (1500, 1500, "1500"), (None, None, None)]
        for index, (low, high, expected) in enumerate(shapes):
            item = ServiceOpportunity(public_id=f"MKT-BUDGET-{index + 2:03d}", organization_id=1, service_type="POOL",
                                       customer_budget_min=low, customer_budget_max=high, pricing_currency="MXN")
            db.add(item)
            db.flush()
            assert public_opportunity(item)["customer_budget_range"] == expected
    finally:
        db.close()


def test_technician_marketplace_cards_render_customer_budget_and_reference_separately():
    frontend = Path(__file__).parents[2].joinpath("frontend", "index.html").read_text()
    assert "renderTechnicianPricing(item)" in frontend
    assert "Presupuesto del cliente" in frontend
    assert "Referencia Total Solutions" in frontend
    assert "marketplaceBudget(item)" in frontend
    assert "marketplaceReference(item)" in frontend


def test_operational_order_card_keeps_budget_independent_from_final_service_value():
    frontend = Path(__file__).parents[2].joinpath("frontend", "index.html").read_text()
    service = Path(__file__).parents[1].joinpath("app", "services", "marketplace_service.py").read_text()
    schema = Path(__file__).parents[1].joinpath("app", "schemas", "lead_schema.py").read_text()
    assert "renderOperationalPricing(lead, order)" in frontend
    assert "final_service_price" in frontend
    assert "customer_budget_min" in frontend and "customer_budget_max" in frontend
    assert "Pendiente de diagnóstico" in frontend
    assert "Keep the accepted opportunity's commercial snapshot" in service
    assert "final_service_price: float | None" in schema
