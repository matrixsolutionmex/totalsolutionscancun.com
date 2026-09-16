import json

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.connection import Base
from app.models.organization import Organization
from app.models.professional_application import ProfessionalApplication
from app.models.professional_network import ProfessionalApplicationEvent, ProfessionalApplicationNote, ProfessionalApplicationSkill
from app.models.user import User
from app.routes.professional_network_routes import SkillInput, StatusInput, coverage, get_application, list_applications, update_application_status, validate_application_skill


def setup():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine, tables=[Organization.__table__, User.__table__, ProfessionalApplication.__table__, ProfessionalApplicationEvent.__table__, ProfessionalApplicationNote.__table__, ProfessionalApplicationSkill.__table__])
    return sessionmaker(bind=engine)()


def seed():
    db = setup()
    org_a, org_b = Organization(name="A", slug="a", status="ACTIVE"), Organization(name="B", slug="b", status="ACTIVE")
    db.add_all([org_a, org_b]); db.flush()
    root = User(username="root", full_name="Root", password_hash="x", role="ROOT", status="ACTIVE", is_active=True, email_verified=True)
    manager = User(username="manager", full_name="Manager", password_hash="x", role="GERENTE", organization_id=org_a.id, status="ACTIVE", is_active=True, email_verified=True)
    technician = User(username="tech", full_name="Tech", password_hash="x", role="BROKER", organization_id=org_a.id, status="ACTIVE", is_active=True, email_verified=True)
    db.add_all([root, manager, technician]); db.flush()
    rows = [
        ProfessionalApplication(public_code="TS-PRO-000001", organization_id=org_a.id, full_name="Carlos", email="c@example.com", whatsapp="1", city="Cancun", zone="Centro", professional_type="Tecnico", experience="5", professional_level="Senior", specialties_json=json.dumps(["Plomería", "Electricidad"]), coverage_json=json.dumps(["Cancun"]), availability="Siempre", languages="es", status="APPROVED", consent_data=True, consent_contact=True, consent_profile=True),
        ProfessionalApplication(public_code="TS-PRO-000002", organization_id=org_a.id, full_name="Ana", email="a@example.com", whatsapp="2", city="Cancun", zone="Centro", professional_type="Tecnico", experience="3", professional_level="Tecnico", specialties_json=json.dumps(["Plomería"]), coverage_json=json.dumps(["Cancun"]), availability="Siempre", languages="es", status="UNDER_REVIEW", consent_data=True, consent_contact=True, consent_profile=True),
        ProfessionalApplication(public_code="TS-PRO-000003", organization_id=org_b.id, full_name="Beto", email="b@example.com", whatsapp="3", city="Cancun", zone="Centro", professional_type="Tecnico", experience="3", professional_level="Tecnico", specialties_json=json.dumps(["Plomería"]), coverage_json=json.dumps(["Cancun"]), availability="Siempre", languages="es", status="APPROVED", consent_data=True, consent_contact=True, consent_profile=True),
    ]
    db.add_all(rows); db.flush()
    db.add_all([ProfessionalApplicationSkill(application_id=rows[0].id, specialty="Plomería", validation_status="VALIDATED"), ProfessionalApplicationSkill(application_id=rows[0].id, specialty="Electricidad", validation_status="DECLARED"), ProfessionalApplicationSkill(application_id=rows[2].id, specialty="Plomería", validation_status="VALIDATED")]); db.commit()
    return db, root, manager, technician, rows


def test_list_detail_and_tenant_isolation():
    db, root, manager, _technician, rows = seed()
    assert {item["public_code"] for item in list_applications(db=db, actor=manager)["items"]} == {"TS-PRO-000001", "TS-PRO-000002"}
    detail = get_application(rows[0].id, db=db, actor=manager)
    assert detail["email"] == "c@example.com"
    with pytest.raises(HTTPException) as exc_info:
        get_application(rows[2].id, db=db, actor=manager)
    assert exc_info.value.status_code == 404
    assert len(list_applications(db=db, actor=root)["items"]) == 3


def test_status_transition_and_audit_are_controlled():
    db, _root, manager, technician, rows = seed()
    assert update_application_status(rows[1].id, StatusInput(status="CONTACTED", note="Hablamos"), db=db, actor=manager)["status"] == "CONTACTED"
    assert rows[1].status == "CONTACTED"
    event = db.query(ProfessionalApplicationEvent).one()
    assert (event.previous_status, event.new_status, event.actor_user_id) == ("UNDER_REVIEW", "CONTACTED", manager.id)
    with pytest.raises(HTTPException) as exc_info:
        update_application_status(rows[1].id, StatusInput(status="ACTIVE"), db=db, actor=technician)
    assert exc_info.value.status_code == 403
    with pytest.raises(HTTPException) as exc_info:
        update_application_status(rows[1].id, StatusInput(status="APPROVED"), db=db, actor=manager)
    assert exc_info.value.status_code == 409


def test_skill_validation_and_coverage_require_validated_skill():
    db, root, manager, _technician, rows = seed()
    data = validate_application_skill(rows[0].id, "Electricidad", SkillInput(validation_status="VALIDATED", note="Confirmado"), db=db, actor=manager)
    assert any(skill["specialty"] == "Electricidad" and skill["validation_status"] == "VALIDATED" for skill in data["skills"])
    result = coverage(db=db, actor=root)
    counts = {item["name"]: item for item in result["specialties"]}
    assert counts["Plomería"]["approved_count"] == 2
    assert counts["Electricidad"]["approved_count"] == 1
    assert counts["Plomería"]["coverage_status"] == "Meta alcanzada"
