import json
import re
from io import BytesIO
from datetime import datetime

import pytest
from fastapi import HTTPException
from fastapi import UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.core.security import hash_password
from app.database.connection import Base
from app.models.deletion_request import DeletionRequest
from app.models.lead import Lead
from app.models.lead_document import LeadDocument
from app.models.lead_event import LeadEvent
from app.models.service_order import ServiceOrder
from app.models.user import User
from app.routes.integration_routes import create_integration_lead, require_integration_token
from app.routes.lead_document_routes import (
    decide_deletion_request,
    delete_lead_document,
    list_deletion_requests,
    list_lead_documents,
    request_lead_document_deletion,
    upload_lead_document,
)
from app.routes.lead_routes import assign_lead, create_lead, list_leads, service_dossier_pdf, update_lead, update_lead_pipeline
from app.schemas.lead_schema import IntegrationLeadCreate, LeadAssignUpdate, LeadCreate, LeadPipelineUpdate, LeadUpdate


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            User.__table__,
            Lead.__table__,
            ServiceOrder.__table__,
            LeadEvent.__table__,
            LeadDocument.__table__,
            DeletionRequest.__table__,
        ],
    )
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def make_user(db, username, role, manager_id=None):
    user = User(
        username=username,
        email=f"{username}@totalsolutions.test",
        full_name=username.title(),
        password_hash=hash_password("secret"),
        role=role,
        manager_id=manager_id,
        status="ACTIVE",
        is_active=True,
        email_verified=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_manual_client_creation_starts_unassigned(db):
    root = make_user(db, "root", "ROOT")
    next_contact = datetime(2026, 8, 1, 15, 30)

    lead = create_lead(
        LeadCreate(
            nombre="Cliente Uno",
            telefono="+52 998 111 2233",
            whatsapp="+52 998 111 2233",
            email="CLIENTE@EXAMPLE.COM",
            direccion="Av. Tulum 100",
            colonia="Centro",
            ciudad="Cancun",
            estado="Quintana Roo",
            codigo_postal="77500",
            google_maps_url="https://maps.google.com/?q=cancun",
            servicio_solicitado="Plomeria",
            tipo_imovel="HOTEL",
            tipo_servico="HIDRAULICA",
            empresa="Hotel Sol",
            pessoa_contato="Recepcion",
            latitude="21.1619",
            longitude="-86.8515",
            foto_fachada_url="https://example.com/fachada.jpg",
            property_extra={
                "hotel_nome": "Hotel Sol",
                "quarto": "1204",
                "andar": "12",
                "recepcao_avisada": "true",
                "contato_recepcao": "+52 998 000 0000",
            },
            descripcion_problema="Fuga en cocina",
            urgencia="ALTA",
            origen="WHATSAPP",
            origen_detalle="Mensaje directo",
            proximo_contacto=next_contact,
            observaciones="Prefiere manana",
        ),
        db,
        root,
    )

    assert lead.nome == "Cliente Uno"
    assert lead.nicho == "Plomeria"
    assert lead.property_id == "TS-000001"
    assert lead.tipo_imovel == "HOTEL"
    assert lead.tipo_servico == "HIDRAULICA"
    assert lead.empresa == "Hotel Sol"
    assert lead.pessoa_contato == "Recepcion"
    assert lead.latitude == "21.1619"
    assert lead.longitude == "-86.8515"
    assert json.loads(lead.property_extra_json)["quarto"] == "1204"
    assert lead.urgencia == "ALTA"
    assert lead.origen == "WHATSAPP"
    assert lead.assigned_to_user_id is None
    assert lead.pipeline == "NOVO LEAD"
    assert lead.proximo_contacto == next_contact
    assert lead.service_order is not None
    assert lead.service_order.order_number == "TS-2026-000001"
    assert lead.service_order.status == "ABERTA"
    assert lead.service_order.property_id == "TS-000001"
    assert lead.service_order.warranty_days == 90
    assert lead.service_order.signature_status == "PENDENTE"
    assert lead.service_order.qr_token is None
    assert lead.service_order.warranty_seal_status == "PENDENTE"
    assert lead.service_order.checklist_status == "PENDENTE"


def test_service_order_number_is_generated_sequentially(db):
    root = make_user(db, "root", "ROOT")

    first = create_lead(LeadCreate(nombre="Cliente Uno OS", telefono="9980000001"), db, root)
    second = create_lead(LeadCreate(nombre="Cliente Dos OS", telefono="9980000002"), db, root)

    assert first.service_order.order_number == "TS-2026-000001"
    assert second.service_order.order_number == "TS-2026-000002"
    assert first.property_id == "TS-000001"
    assert second.property_id == "TS-000002"


def test_integration_client_creation_uses_external_metadata(db, monkeypatch):
    monkeypatch.setenv("MATRIX_IMPORT_TOKEN", "integration-secret")
    assert require_integration_token("Bearer integration-secret") is None

    lead = create_integration_lead(
        IntegrationLeadCreate(
            nombre="Cliente Google",
            telefono="+52 998 222 3344",
            email="google@example.com",
            direccion="Zona Hotelera",
            ciudad="Cancun",
            servicio_solicitado="Aire acondicionado",
            tipo_imovel="AIRBNB",
            tipo_servico="AR_CONDICIONADO",
            property_extra={"codigo_reserva": "BK-77", "host": "Maria"},
            descripcion_problema="No enfria",
            origen="GOOGLE_ADS",
            origen_detalle="Campana verano",
            external_id="g-123",
        ),
        db,
        None,
    )

    assert lead.external_source == "GOOGLE_ADS"
    assert lead.external_id == "g-123"
    assert lead.property_id == "TS-000001"
    assert lead.tipo_imovel == "AIRBNB"
    assert lead.tipo_servico == "AR_CONDICIONADO"
    assert json.loads(lead.property_extra_json)["codigo_reserva"] == "BK-77"
    assert lead.received_at is not None
    assert lead.assigned_to_user_id is None
    assert lead.service_order is not None
    assert lead.service_order.order_number == "TS-2026-000001"


def test_service_order_tracks_assignment_and_pipeline(db):
    root = make_user(db, "root", "ROOT")
    manager = make_user(db, "supervisor", "GERENTE")
    technician = make_user(db, "tecnico", "BROKER", manager_id=manager.id)
    lead = create_lead(LeadCreate(nombre="Cliente OS", telefono="9987778899"), db, root)

    assigned = assign_lead(lead.id, LeadAssignUpdate(assigned_to_user_id=technician.id), db, root)
    moved = update_lead_pipeline(lead.id, LeadPipelineUpdate(pipeline="VENDA GANHA"), db, root)

    assert assigned.service_order.responsible_user_id == technician.id
    assert assigned.service_order.supervisor_user_id == manager.id
    assert moved.service_order.status == "APROVADA"
    assert moved.service_order.completed_at is not None


def test_integration_blocks_duplicates_by_external_id_phone_and_email(db):
    create_integration_lead(
        IntegrationLeadCreate(
            nombre="Cliente Original",
            telefono="+52 998 333 4455",
            email="dup@example.com",
            origen="META_ADS",
            external_id="meta-1",
        ),
        db,
        None,
    )

    with pytest.raises(HTTPException) as by_external:
        create_integration_lead(
            IntegrationLeadCreate(nombre="Otro", telefono="+52 998 999 9999", origen="META_ADS", external_id="meta-1"),
            db,
            None,
        )
    assert by_external.value.status_code == 409

    with pytest.raises(HTTPException) as by_phone:
        create_integration_lead(
            IntegrationLeadCreate(nombre="Otro", telefono="9983334455", origen="WHATSAPP", external_id="wa-2"),
            db,
            None,
        )
    assert by_phone.value.status_code == 409
    assert "Cliente duplicado" in by_phone.value.detail
    assert "TS-2026-000001" in by_phone.value.detail

    with pytest.raises(HTTPException) as by_email:
        create_integration_lead(
            IntegrationLeadCreate(nombre="Otro", telefono="+52 998 777 8888", email="DUP@example.com", origen="LLAMADA"),
            db,
            None,
        )
    assert by_email.value.status_code == 409


def test_root_can_distribute_to_supervisor_and_technician(db):
    root = make_user(db, "root", "ROOT")
    manager = make_user(db, "supervisor", "GERENTE")
    technician = make_user(db, "tecnico", "BROKER", manager_id=manager.id)
    lead = create_lead(LeadCreate(nombre="Cliente Asignable", telefono="9981002000"), db, root)

    assigned_to_manager = assign_lead(lead.id, LeadAssignUpdate(assigned_to_user_id=manager.id), db, root)
    assert assigned_to_manager.assigned_to_user_id == manager.id

    assigned_to_technician = assign_lead(lead.id, LeadAssignUpdate(assigned_to_user_id=technician.id), db, root)
    assert assigned_to_technician.assigned_to_user_id == technician.id


def test_supervisor_distributes_only_to_own_team(db):
    root = make_user(db, "root", "ROOT")
    manager_a = make_user(db, "supervisor-a", "GERENTE")
    manager_b = make_user(db, "supervisor-b", "GERENTE")
    technician_a = make_user(db, "tecnico-a", "BROKER", manager_id=manager_a.id)
    technician_b = make_user(db, "tecnico-b", "BROKER", manager_id=manager_b.id)
    lead = create_lead(LeadCreate(nombre="Cliente Equipo", telefono="9984445566"), db, root)

    assigned = assign_lead(lead.id, LeadAssignUpdate(assigned_to_user_id=technician_a.id), db, manager_a)
    assert assigned.assigned_to_user_id == technician_a.id

    with pytest.raises(HTTPException) as blocked:
        assign_lead(lead.id, LeadAssignUpdate(assigned_to_user_id=technician_b.id), db, manager_a)
    assert blocked.value.status_code == 403


def test_technician_cannot_redistribute(db):
    root = make_user(db, "root", "ROOT")
    manager = make_user(db, "supervisor", "GERENTE")
    technician = make_user(db, "tecnico", "BROKER", manager_id=manager.id)
    other_technician = make_user(db, "tecnico-2", "BROKER", manager_id=manager.id)
    lead = create_lead(
        LeadCreate(nombre="Cliente Tecnico", telefono="9985556677", assigned_to_user_id=technician.id),
        db,
        root,
    )

    with pytest.raises(HTTPException) as blocked:
        assign_lead(lead.id, LeadAssignUpdate(assigned_to_user_id=other_technician.id), db, technician)
    assert blocked.value.status_code == 403


def test_unassigned_filter_returns_only_clients_without_responsible(db):
    root = make_user(db, "root", "ROOT")
    manager = make_user(db, "supervisor", "GERENTE")
    technician = make_user(db, "tecnico", "BROKER", manager_id=manager.id)
    unassigned = create_lead(LeadCreate(nombre="Cliente Libre", telefono="9986667788"), db, root)
    create_lead(
        LeadCreate(nombre="Cliente Asignado", telefono="9986667799", assigned_to_user_id=technician.id),
        db,
        root,
    )

    leads = list_leads(db=db, actor=root, unassigned=True, limit=100, offset=0)

    assert [lead.id for lead in leads] == [unassigned.id]


def test_service_dossier_pdf_is_generated_for_visible_lead(db):
    root = make_user(db, "root", "ROOT")
    technician = make_user(db, "juan", "BROKER")
    lead = create_lead(
        LeadCreate(
            nombre="Cliente PDF",
            telefono="9981010101",
            whatsapp="9982020202",
            email="cliente.pdf@example.com",
            direccion="Av. Kukulkan 100",
            colonia="Zona Hotelera",
            ciudad="Cancun",
            estado="Quintana Roo",
            codigo_postal="77500",
            google_maps_url="https://maps.example/test",
            tipo_imovel="HOTEL",
            tipo_servico="HIDRAULICA",
            assigned_to_user_id=technician.id,
            descripcion_problema="Fuga en habitacion",
            urgencia="ALTA",
            origen="GOOGLE_ADS",
            origen_detalle="Campana busqueda",
            observaciones="Cliente solicita visita hoy",
        ),
        db,
        root,
    )
    db.add(
        LeadEvent(
            lead_id=lead.id,
            actor_id=root.id,
            actor_name=root.full_name,
            event_type="PIPELINE",
            message="Diagnostico concluido",
        )
    )
    db.add(
        LeadDocument(
            lead_id=lead.id,
            uploaded_by_user_id=root.id,
            document_type="ANTES_SERVICIO",
            file_name="Antes_Cozinha.jpg",
            file_path="/uploads/lead_documents/1/file.jpg",
            file_mime="image/jpeg",
            file_size=2048,
        )
    )
    db.commit()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "server": ("testserver", 80),
            "path": f"/leads/{lead.id}/dossier.pdf",
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"testserver")],
        }
    )

    response = service_dossier_pdf(lead.id, request, db, root)

    assert response.media_type == "application/pdf"
    assert response.body.startswith(b"%PDF-1.4")
    text = response.body.decode("latin-1")
    assert "TOTAL SOLUTIONS" in text
    assert "DOSSI" in text
    assert "ORDEM DE SERVICO" in text
    assert "TS-2026-000001" in text
    assert "Assinaturas digitais" in text
    assert "QR Code" in text
    assert "Checklist tecnico" in text
    assert "Cliente PDF" in text
    assert "9981010101" in text
    assert "Cancun" in text
    assert "Hidraulica" in text
    assert "Nuevo contacto" in text
    assert "Google Ads" in text
    assert "Alta" in text
    assert "Fuga en habitacion" in text
    assert "Juan" in text
    assert "Diagnostico concluido" in text
    assert "Antes_Cozinha.jpg" in text
    assert "Anexo registrado no sistema" in text
    assert "/uploads/lead_documents" not in text
    assert "RELAT" in text
    streams = re.findall(r"stream\n(.*?)\nendstream", text, flags=re.S)
    assert streams
    assert all(" Tj" in stream for stream in streams)


def make_upload_file(filename: str, content: bytes = b"fake file", content_type: str = "image/jpeg"):
    return UploadFile(filename=filename, file=BytesIO(content), headers={"content-type": content_type})


def test_technician_creates_client_assigned_to_self_and_edits_operational_fields(db):
    technician = make_user(db, "tecnico-campo", "BROKER")

    lead = create_lead(
        LeadCreate(nombre="Cliente Campo", telefono="9981212121", assigned_to_user_id=999),
        db,
        technician,
    )

    assert lead.assigned_to_user_id == technician.id

    updated = update_lead(
        lead.id,
        LeadUpdate(
            nome="Cliente Campo Actualizado",
            descripcion_problema="Diagnostico actualizado",
            assigned_to_user_id=technician.id,
        ),
        db,
        technician,
    )

    assert updated.nome == "Cliente Campo Actualizado"
    assert updated.descripcion_problema == "Diagnostico actualizado"
    assert updated.assigned_to_user_id == technician.id


def test_technician_cannot_change_responsible_through_update_payload(db):
    root = make_user(db, "root", "ROOT")
    technician = make_user(db, "tecnico-payload", "BROKER")
    other_technician = make_user(db, "tecnico-outro", "BROKER")
    lead = create_lead(
        LeadCreate(nombre="Cliente Protegido", telefono="9981313131", assigned_to_user_id=technician.id),
        db,
        root,
    )

    with pytest.raises(HTTPException) as blocked:
        update_lead(
            lead.id,
            LeadUpdate(nome="Tentativa", assigned_to_user_id=other_technician.id),
            db,
            technician,
        )

    assert blocked.value.status_code == 403
    db.refresh(lead)
    assert lead.assigned_to_user_id == technician.id
    assert lead.nome == "Cliente Protegido"


def test_technician_uploads_documents_but_cannot_delete_directly(db, tmp_path, monkeypatch):
    import app.routes.lead_document_routes as document_routes

    monkeypatch.setattr(document_routes, "UPLOADS_DIR", tmp_path)
    root = make_user(db, "root", "ROOT")
    technician = make_user(db, "tecnico-docs", "BROKER")
    lead = create_lead(
        LeadCreate(nombre="Cliente Evidencias", telefono="9981414141", assigned_to_user_id=technician.id),
        db,
        root,
    )

    upload_cases = [
        ("ANTES_SERVICIO", "antes.jpg", "image/jpeg"),
        ("DURANTE_SERVICIO", "durante.jpg", "image/jpeg"),
        ("DESPUES_SERVICIO", "despues.jpg", "image/jpeg"),
        ("PRESUPUESTO", "presupuesto.pdf", "application/pdf"),
        ("NOTA_FISCAL", "nota.pdf", "application/pdf"),
        ("GARANTIA", "garantia.pdf", "application/pdf"),
        ("VIDEO", "video.mp4", "video/mp4"),
        ("OTROS", "archivo.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ]

    uploaded = [
        upload_lead_document(
            lead.id,
            document_type,
            make_upload_file(filename, content_type=mime_type),
            db,
            technician,
        )
        for document_type, filename, mime_type in upload_cases
    ]

    assert len(uploaded) == len(upload_cases)
    assert {doc["document_type"] for doc in uploaded} == {case[0] for case in upload_cases}
    assert all(doc["uploaded_by_user_id"] == technician.id for doc in uploaded)

    documents = list_lead_documents(lead.id, db, technician)
    assert len(documents) == len(upload_cases)

    with pytest.raises(HTTPException) as blocked:
        delete_lead_document(lead.id, uploaded[0]["id"], db, technician)
    assert blocked.value.status_code == 403

    deletion_request = request_lead_document_deletion(
        lead.id,
        uploaded[0]["id"],
        "Foto duplicada enviada por engano",
        db,
        technician,
    )
    assert deletion_request["status"] == "PENDENTE"
    assert deletion_request["requested_by_user_id"] == technician.id
    assert db.query(LeadDocument).filter(LeadDocument.id == uploaded[0]["id"]).first() is not None

    visible_requests = list_deletion_requests("PENDENTE", db, root)
    assert len(visible_requests) == 1
    assert visible_requests[0]["document_name"] == "antes.jpg"
    assert visible_requests[0]["requested_by_name"] == "Tecnico-Docs"

    rejected = decide_deletion_request(
        deletion_request["id"],
        {"status": "REJEITADA", "decision_reason": "Documento necesario"},
        db,
        root,
    )
    assert rejected["status"] == "REJEITADA"
    assert db.query(LeadDocument).filter(LeadDocument.id == uploaded[0]["id"]).first() is not None

    second_request = request_lead_document_deletion(
        lead.id,
        uploaded[0]["id"],
        "Foto duplicada confirmada",
        db,
        technician,
    )
    approved = decide_deletion_request(
        second_request["id"],
        {"status": "APROVADA"},
        db,
        root,
    )
    assert approved["status"] == "APROVADA"
    assert db.query(LeadDocument).filter(LeadDocument.id == uploaded[0]["id"]).first() is None
