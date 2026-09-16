import asyncio
import io
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.requests import Request

from app.database.connection import Base
from app.models.auth_security import AuthRateLimit
from app.models.notification import EmailOutbox
from app.models.organization import Organization
from app.models.professional_application import ProfessionalApplication
from app.routes.professional_application_routes import create_professional_application
from app.main import app
import httpx


def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine, tables=[
        Organization.__table__, AuthRateLimit.__table__, EmailOutbox.__table__, ProfessionalApplication.__table__,
    ])
    return sessionmaker(bind=engine)()


def request():
    return Request({"type": "http", "method": "POST", "path": "/public/professional-applications", "headers": [], "client": ("127.0.0.1", 8000), "scheme": "http", "server": ("test", 80)})


def fields(key="submission-1"):
    return dict(
        request=request(), full_name="Ana Tecnica", email="ana@example.com", phone=None, whatsapp="+52 998 000 0000",
        city="Cancun", zone="Zona Hotelera", professional_type="Tecnico independiente", experience="4 a 7 anos",
        professional_level="Avanzado", specialties=json.dumps(["Plomeria", "Electricidad"]), coverage=json.dumps(["Cancun"]),
        availability="Lunes a sabado", languages="Espanol, ingles", presentation="Experiencia residencial.",
        has_vehicle=True, has_tools=True, has_team=False, can_invoice=True, attention_emergencies=False,
        consent_data=True, consent_contact=True, consent_profile=True, submission_key=key, website="", files=None, db=session(),
    )


def test_professional_application_persists_received_and_enqueues_email():
    values = fields()
    db = values["db"]
    result = asyncio.run(create_professional_application(**values))

    row = db.query(ProfessionalApplication).one()
    outbox = db.query(EmailOutbox).one()
    assert result == {"public_code": row.public_code, "status": "RECEIVED", "duplicate": False}
    assert row.public_code.startswith("TS-PRO-")
    assert row.status == "RECEIVED"
    assert json.loads(row.specialties_json) == ["Plomeria", "Electricidad"]
    assert outbox.to_email == "inscripciones@totalsolutionscancun.com"
    assert row.public_code in outbox.body_text


def test_duplicate_submission_key_returns_existing_application_without_duplicate_email():
    first = fields("same-key")
    db = first["db"]
    result_one = asyncio.run(create_professional_application(**first))
    second = fields("same-key")
    second["db"] = db
    result_two = asyncio.run(create_professional_application(**second))

    assert result_two["duplicate"] is True
    assert result_two["public_code"] == result_one["public_code"]
    assert db.query(ProfessionalApplication).count() == 1
    assert db.query(EmailOutbox).count() == 1


def test_application_rejects_honeypot_and_invalid_upload():
    honeypot = fields("honeypot")
    honeypot["website"] = "https://spam.example"
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(create_professional_application(**honeypot))
    assert exc_info.value.status_code == 422

    invalid = fields("invalid-file")
    invalid["files"] = [StarletteUploadFile(file=io.BytesIO(b"not-an-image"), filename="proof.jpg", headers={"content-type": "image/jpeg"})]
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(create_professional_application(**invalid))
    assert exc_info.value.status_code == 422


def test_network_page_and_sitemap_are_public():
    async def request_pages():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/unete-a-la-red"), await client.get("/sitemap.xml")

    page, sitemap = asyncio.run(request_pages())
    assert page.status_code == 200
    assert "Únete a la red de profesionales" in page.text
    assert "/public/professional-applications" in page.text or "professionalApplicationForm" in page.text
    assert sitemap.status_code == 200
    assert "https://totalsolutionscancun.com/unete-a-la-red" in sitemap.text
