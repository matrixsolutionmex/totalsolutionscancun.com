import json
import re
from io import BytesIO
from datetime import datetime
from pathlib import Path

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
from app.models.notification import EmailOutbox, Notification, NotificationPreference, WebPushSubscription
from app.models.organization import Organization
from app.models.service_order import ServiceOrder
from app.models.service_property import ServiceProperty
from app.models.service_request import ServiceRequest, ServiceRequestMedia
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
from app.routes.notification_routes import (
    deactivate_web_push_subscription,
    deactivate_web_push_subscription_by_id,
    get_web_push_state,
    list_notifications,
    mark_all_notifications_read,
    mark_notification_read,
    register_web_push_subscription,
    unread_notification_count,
)
from app.routes.service_request_routes import (
    AssignUserPayload,
    TriagePayload,
    assign_service_order_supervisor,
    assign_service_order_technician,
    list_sales_service_requests,
    triage_service_request,
)
from app.schemas.lead_schema import IntegrationLeadCreate, LeadAssignUpdate, LeadCreate, LeadPipelineUpdate, LeadUpdate
from app.schemas.notification_schema import WebPushDeactivateRequest, WebPushKeys, WebPushSubscriptionCreate
from app.services.customer_portal_service import create_customer_request_and_order, service_request_public_status
from app.services.import_service import import_lead_records
from app.services.notification_service import notification_push_payload, notify_client_created


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
            Organization.__table__,
            User.__table__,
            Lead.__table__,
            ServiceProperty.__table__,
            ServiceRequest.__table__,
            ServiceRequestMedia.__table__,
            ServiceOrder.__table__,
            LeadEvent.__table__,
            LeadDocument.__table__,
            DeletionRequest.__table__,
            Notification.__table__,
            NotificationPreference.__table__,
            EmailOutbox.__table__,
            WebPushSubscription.__table__,
        ],
    )
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def make_user(db, username, role, manager_id=None, organization_id=None, status="ACTIVE", is_active=True):
    user = User(
        username=username,
        email=f"{username}@totalsolutions.test",
        full_name=username.title(),
        password_hash=hash_password("secret"),
        role=role,
        manager_id=manager_id,
        organization_id=organization_id,
        status=status,
        is_active=is_active,
        email_verified=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def make_organization(db, slug, name):
    organization = Organization(name=name, slug=slug)
    db.add(organization)
    db.commit()
    db.refresh(organization)
    return organization


def test_root_scope_is_limited_to_own_organization(db):
    org_a = make_organization(db, "org-a", "Empresa A")
    org_b = make_organization(db, "org-b", "Empresa B")
    root_a = make_user(db, "root-a", "ROOT")
    root_b = make_user(db, "root-b", "ROOT")
    root_a.organization_id = org_a.id
    root_b.organization_id = org_b.id
    lead_a = Lead(nome="Cliente A", contato="9980000001", pipeline="NOVO LEAD", organization_id=org_a.id)
    lead_b = Lead(nome="Cliente B", contato="9980000002", pipeline="NOVO LEAD", organization_id=org_b.id)
    db.add_all([lead_a, lead_b])
    db.commit()
    db.refresh(lead_a)

    visible = list_leads(db=db, actor=root_a, limit=100, offset=0)

    assert [lead.id for lead in visible] == [lead_a.id]


def test_duplicate_detection_is_scoped_by_organization(db):
    from app.services.lead_entry_service import duplicate_lead

    org_a = make_organization(db, "dup-org-a", "Duplicados A")
    org_b = make_organization(db, "dup-org-b", "Duplicados B")
    lead_a = Lead(nome="Cliente Compartilhado", email="cliente@example.com", organization_id=org_a.id)
    db.add(lead_a)
    db.commit()
    db.refresh(lead_a)

    assert duplicate_lead(db, email="cliente@example.com", organization_id=org_a.id).id == lead_a.id
    assert duplicate_lead(db, email="cliente@example.com", organization_id=org_b.id) is None


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


def test_client_created_notifies_other_active_admin_in_same_organization(db):
    org = make_organization(db, "client-created-org", "Cliente Created Org")
    actor = make_user(db, "admin-criador", "ROOT", organization_id=org.id)
    recipient = make_user(db, "admin-destino", "ROOT", organization_id=org.id)

    lead = create_lead(LeadCreate(nombre="Cliente Privado", telefono="9980001111"), db, actor)

    actor_notifications = list_notifications(20, None, db, actor)
    recipient_notifications = list_notifications(20, None, db, recipient)
    assert actor_notifications == []
    assert len(recipient_notifications) == 1
    notification = recipient_notifications[0]
    assert notification.type == "client_created"
    assert notification.title == "Nuevo cliente registrado"
    assert notification.message == "Admin-Criador registró un nuevo cliente."
    assert notification.lead_id == lead.id
    assert notification.action_url == f"/?lead_id={lead.id}&source=client_created"
    assert notification.priority == "NORMAL"
    assert notification.metadata["event_type"] == "CLIENT_CREATED"
    assert notification.metadata["entity_type"] == "client"
    assert notification.metadata["entity_id"] == lead.id
    assert notification.metadata["deduplication_key"] == f"client_created:{org.id}:{lead.id}"


def test_client_created_by_technician_notifies_admin_without_sensitive_push_payload(db):
    org = make_organization(db, "client-created-tech", "Cliente Created Tech")
    admin = make_user(db, "admin-tecnico", "ROOT", organization_id=org.id)
    technician = make_user(db, "juan-tecnico", "BROKER", organization_id=org.id)

    lead = create_lead(
        LeadCreate(
            nombre="Cliente Sensible",
            telefono="9989998877",
            email="cliente.sensible@example.com",
            direccion="Direccion privada 123",
            assigned_to_user_id=999,
        ),
        db,
        technician,
    )

    notifications = list_notifications(20, None, db, admin)
    assert len(notifications) == 1
    notification = notifications[0]
    assert notification.type == "client_created"
    assert notification.actor_user_id == technician.id
    assert notification.message == "Juan-Tecnico registró un nuevo cliente."
    payload = notification_push_payload(db, db.query(Notification).filter(Notification.id == notification.id).first())
    serialized = json.dumps(payload, ensure_ascii=False)
    assert payload["url"].endswith(f"/?lead_id={lead.id}&source=client_created")
    assert "Cliente Sensible" not in serialized
    assert "9989998877" not in serialized
    assert "cliente.sensible@example.com" not in serialized
    assert "Direccion privada" not in serialized


def test_client_update_does_not_create_client_created_again(db):
    org = make_organization(db, "client-update-org", "Cliente Update Org")
    actor = make_user(db, "admin-update", "ROOT", organization_id=org.id)
    recipient = make_user(db, "admin-update-destino", "ROOT", organization_id=org.id)
    lead = create_lead(LeadCreate(nombre="Cliente Actualizable", telefono="9981001000"), db, actor)
    assert unread_notification_count(db, recipient)["unread"] == 1

    update_lead(lead.id, LeadUpdate(nome="Cliente Actualizado"), db, actor)

    notifications = list_notifications(20, None, db, recipient)
    assert len([notification for notification in notifications if notification.type == "client_created"]) == 1


def test_client_created_notification_is_idempotent_per_recipient(db):
    org = make_organization(db, "client-idempotent-org", "Cliente Idempotente Org")
    actor = make_user(db, "admin-idempotent", "ROOT", organization_id=org.id)
    recipient = make_user(db, "admin-idempotent-destino", "ROOT", organization_id=org.id)
    lead = create_lead(LeadCreate(nombre="Cliente Dedupe", telefono="9981002000"), db, actor)
    assert unread_notification_count(db, recipient)["unread"] == 1

    first = notify_client_created(db, lead=lead, actor=actor)
    second = notify_client_created(db, lead=lead, actor=actor)
    db.commit()

    notifications = list_notifications(20, None, db, recipient)
    assert first == second == [notifications[0].id]
    assert len([notification for notification in notifications if notification.type == "client_created"]) == 1


def test_client_created_excludes_inactive_admin_and_other_organization(db):
    org_a = make_organization(db, "client-org-a", "Cliente Org A")
    org_b = make_organization(db, "client-org-b", "Cliente Org B")
    actor = make_user(db, "admin-org-a", "ROOT", organization_id=org_a.id)
    active_admin = make_user(db, "admin-org-a-destino", "ROOT", organization_id=org_a.id)
    inactive_admin = make_user(db, "admin-inativo", "ROOT", organization_id=org_a.id, status="SUSPENDED", is_active=False)
    other_admin = make_user(db, "admin-org-b", "ROOT", organization_id=org_b.id)

    create_lead(LeadCreate(nombre="Cliente Isolado", telefono="9981112222"), db, actor)

    assert unread_notification_count(db, active_admin)["unread"] == 1
    assert unread_notification_count(db, inactive_admin)["unread"] == 0
    assert unread_notification_count(db, other_admin)["unread"] == 0


def test_client_created_single_admin_is_audit_only_without_push(db, monkeypatch):
    from app.services import notification_service

    org = make_organization(db, "client-single-admin", "Cliente Single Admin")
    actor = make_user(db, "admin-solo", "ROOT", organization_id=org.id)
    calls = []

    def unexpected_webpush(**kwargs):
        calls.append(kwargs)

    monkeypatch.setenv("WEB_PUSH_VAPID_PUBLIC_KEY", "public-key")
    monkeypatch.setenv("WEB_PUSH_VAPID_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("WEB_PUSH_CONTACT_EMAIL", "admin@totalsolutions.test")
    monkeypatch.setattr(notification_service, "webpush", unexpected_webpush)
    db.add(NotificationPreference(user_id=actor.id, organization_id=org.id, browser_enabled=True))
    db.add(
        WebPushSubscription(
            organization_id=org.id,
            user_id=actor.id,
            endpoint="https://push.example.test/actor",
            p256dh="client-public-key",
            auth="client-auth-secret",
            active=True,
        )
    )
    db.commit()

    create_lead(LeadCreate(nombre="Cliente Auditoria", telefono="9982223333"), db, actor)

    notifications = list_notifications(20, None, db, actor)
    assert len(notifications) == 1
    assert notifications[0].type == "client_created"
    assert calls == []


def test_web_push_failure_does_not_rollback_client_created(db, monkeypatch):
    from app.services import notification_service

    org = make_organization(db, "client-push-failure", "Cliente Push Failure")
    actor = make_user(db, "admin-push-criador", "ROOT", organization_id=org.id)
    recipient = make_user(db, "admin-push-destino", "ROOT", organization_id=org.id)

    class ProviderFailure(Exception):
        response = type("Response", (), {"status_code": 500})()

    def failing_webpush(**_kwargs):
        raise ProviderFailure()

    monkeypatch.setenv("WEB_PUSH_VAPID_PUBLIC_KEY", "public-key")
    monkeypatch.setenv("WEB_PUSH_VAPID_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("WEB_PUSH_CONTACT_EMAIL", "admin@totalsolutions.test")
    monkeypatch.setattr(notification_service, "webpush", failing_webpush)
    monkeypatch.setattr(notification_service, "WebPushException", ProviderFailure)
    db.add(NotificationPreference(user_id=recipient.id, organization_id=org.id, browser_enabled=True))
    db.add(
        WebPushSubscription(
            organization_id=org.id,
            user_id=recipient.id,
            endpoint="https://push.example.test/failure",
            p256dh="client-public-key",
            auth="client-auth-secret",
            active=True,
        )
    )
    db.commit()

    lead = create_lead(LeadCreate(nombre="Cliente Push Falha", telefono="9983334444"), db, actor)

    assert db.query(Lead).filter(Lead.id == lead.id).first() is not None
    assert unread_notification_count(db, recipient)["unread"] == 1


def test_bulk_import_does_not_emit_individual_client_created_notifications(db):
    org = make_organization(db, "client-import-org", "Cliente Import Org")
    admin = make_user(db, "admin-import", "ROOT", organization_id=org.id)

    stats = import_lead_records(
        db,
        [
            {"nome": "Importado Uno", "contato": "9984440001"},
            {"nome": "Importado Dos", "contato": "9984440002"},
        ],
        organization_id=org.id,
    )

    assert stats.inserted == 2
    assert unread_notification_count(db, admin)["unread"] == 0
    assert db.query(Notification).filter(Notification.type == "client_created").count() == 0


def test_assignment_push_payload_hides_client_name_but_keeps_existing_alert(db):
    org = make_organization(db, "assignment-payload-org", "Assignment Payload Org")
    root = make_user(db, "root-payload", "ROOT", organization_id=org.id)
    technician = make_user(db, "tecnico-payload-safe", "BROKER", organization_id=org.id)
    lead = create_lead(
        LeadCreate(nombre="Cliente Tela Bloqueada", telefono="9985556666", assigned_to_user_id=technician.id),
        db,
        root,
    )

    notification = (
        db.query(Notification)
        .filter(Notification.type == "lead_assigned", Notification.recipient_user_id == technician.id)
        .one()
    )
    assert "Cliente Tela Bloqueada" in notification.message
    payload = notification_push_payload(db, notification)
    assert payload["title"] == "Nueva solicitud asignada"
    assert lead.service_order.order_number in payload["body"]
    assert "Cliente Tela Bloqueada" not in payload["body"]


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


def test_assignment_creates_isolated_persistent_notification_and_email_outbox(db):
    root = make_user(db, "root", "ROOT")
    manager = make_user(db, "supervisor", "GERENTE")
    technician = make_user(db, "tecnico-alerta", "BROKER", manager_id=manager.id)
    lead = create_lead(LeadCreate(nombre="Cliente Alerta", telefono="9988881111", urgencia="EMERGENCIA"), db, root)

    assigned = assign_lead(lead.id, LeadAssignUpdate(assigned_to_user_id=technician.id), db, root)

    assert assigned.assigned_to_user_id == technician.id
    technician_notifications = list_notifications(20, None, db, technician)
    root_notifications = list_notifications(20, None, db, root)
    assert len(technician_notifications) == 1
    assert root_notifications == []
    assert technician_notifications[0].type == "lead_assigned"
    assert technician_notifications[0].priority == "URGENTE"
    assert technician_notifications[0].lead_id == lead.id
    assert unread_notification_count(db, technician)["unread"] == 1
    assert db.query(EmailOutbox).count() == 1

    marked = mark_notification_read(technician_notifications[0].id, db, technician)
    assert marked.read_at is not None
    assert unread_notification_count(db, technician)["unread"] == 0

    reassigned = assign_lead(lead.id, LeadAssignUpdate(assigned_to_user_id=manager.id), db, root)
    assert reassigned.assigned_to_user_id == manager.id
    assert unread_notification_count(db, technician)["unread"] == 1
    assert unread_notification_count(db, manager)["unread"] == 1
    assert mark_all_notifications_read(db, technician)["updated"] == 1
    assert unread_notification_count(db, technician)["unread"] == 0


def test_web_push_subscription_lifecycle_is_bound_to_current_user(db, monkeypatch):
    monkeypatch.setenv("WEB_PUSH_VAPID_PUBLIC_KEY", "public-key")
    root = make_user(db, "root", "ROOT")
    technician = make_user(db, "tecnico-push", "BROKER")

    payload = WebPushSubscriptionCreate(
        endpoint="https://push.example.test/device-1",
        keys=WebPushKeys(p256dh="client-public-key", auth="client-auth-secret"),
        device_label="Celular tecnico",
    )
    subscription = register_web_push_subscription(payload, db, technician, "UnitTest/1.0")

    assert subscription.device_label == "Celular tecnico"
    state = get_web_push_state(db, technician)
    assert state.supported is True
    assert state.subscribed is True
    assert len(state.subscriptions) == 1

    with pytest.raises(HTTPException) as blocked:
        deactivate_web_push_subscription_by_id(subscription.id, db, root)
    assert blocked.value.status_code == 404

    result = deactivate_web_push_subscription(
        WebPushDeactivateRequest(endpoint="https://push.example.test/device-1"),
        db,
        technician,
    )
    assert result["deactivated"] is True
    assert get_web_push_state(db, technician).subscribed is False


def test_web_push_delivery_failure_does_not_rollback_assignment(db, monkeypatch):
    from app.services import notification_service

    monkeypatch.setenv("WEB_PUSH_VAPID_PUBLIC_KEY", "public-key")
    monkeypatch.setenv("WEB_PUSH_VAPID_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("WEB_PUSH_CONTACT_EMAIL", "admin@totalsolutions.test")

    class GonePush(Exception):
        response = type("Response", (), {"status_code": 410})()

    def failing_webpush(**_kwargs):
        raise GonePush()

    monkeypatch.setattr(notification_service, "webpush", failing_webpush)
    monkeypatch.setattr(notification_service, "WebPushException", GonePush)

    root = make_user(db, "root", "ROOT")
    technician = make_user(db, "tecnico-push-failure", "BROKER")
    subscription = WebPushSubscription(
        user_id=technician.id,
        endpoint="https://push.example.test/gone",
        p256dh="client-public-key",
        auth="client-auth-secret",
        active=True,
    )
    db.add(subscription)
    db.add(NotificationPreference(user_id=technician.id, browser_enabled=True))
    db.commit()

    lead = create_lead(LeadCreate(nombre="Cliente Push", telefono="9981231234"), db, root)
    assigned = assign_lead(lead.id, LeadAssignUpdate(assigned_to_user_id=technician.id), db, root)

    assert assigned.assigned_to_user_id == technician.id
    db.refresh(subscription)
    assert subscription.active is False


def test_pwa_manifest_and_service_worker_are_present():
    project_root = Path(__file__).resolve().parents[2]
    manifest = json.loads((project_root / "frontend" / "manifest.webmanifest").read_text())
    service_worker = (project_root / "frontend" / "sw.js").read_text()

    assert manifest["name"] == "Total Solutions CRM"
    assert manifest["display"] == "standalone"
    assert manifest["scope"] == "/"
    assert manifest["start_url"].startswith("/")
    assert "push" in service_worker
    assert "showNotification" in service_worker
    assert "fetch" not in service_worker


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


class FakePortalUpload:
    def __init__(self, filename: str, content_type: str, content: bytes = b"portal file"):
        self.filename = filename
        self.content_type = content_type
        self.file = BytesIO(content)


def make_portal_payload(**overrides):
    payload = {
        "requester_name": "Cliente Portal",
        "requester_phone": "+52 998 000 0101",
        "requester_email": "cliente.portal@example.com",
        "property_type": "Hotel",
        "service_category": "Hidraulica",
        "problem_description": "Fuga en cocina",
        "urgency": "NORMAL",
        "address_line1": "Av. Tulum 100",
        "district": "Centro",
        "locality": "Cancun",
        "administrative_area": "Quintana Roo",
        "country_code": "MX",
        "postal_code": "77500",
        "google_maps_url": "https://maps.google.com/?q=cancun",
        "consent_privacy": True,
        "consent_images": True,
        "idempotency_key": "portal-key-1",
        "public_language": "es-MX",
    }
    payload.update(overrides)
    return payload


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


def test_customer_portal_creates_request_property_and_service_order(db, tmp_path, monkeypatch):
    import app.services.customer_portal_service as portal_service

    monkeypatch.setattr(portal_service, "UPLOADS_DIR", tmp_path)
    org = make_organization(db, "portal-org", "Portal Org")

    service_request = create_customer_request_and_order(
        db,
        make_portal_payload(idempotency_key="portal-create-1"),
        files=[FakePortalUpload("fachada.jpg", "image/jpeg", b"imagen")],
    )
    db.commit()
    db.refresh(service_request)

    assert service_request.status == "SALES_QUEUE"
    assert service_request.organization_id == org.id
    assert service_request.lead is not None
    assert service_request.property_record is not None
    assert service_request.property_record.property_code == f"TS-PROP-{service_request.property_record.id:06d}"
    assert service_request.service_order is not None
    assert service_request.service_order.order_number == "TS-2026-000001"
    assert service_request.service_order.service_request_id == service_request.id
    assert service_request.service_order.property_record_id == service_request.property_id
    assert service_request.service_order.lead_id == service_request.lead_id
    assert service_request.media[0].original_filename == "fachada.jpg"
    assert (tmp_path / service_request.media[0].storage_path).exists()
    portal_documents = db.query(LeadDocument).filter(LeadDocument.lead_id == service_request.lead_id).all()
    assert len(portal_documents) == 1
    portal_document = portal_documents[0]
    assert portal_document.organization_id == org.id
    assert portal_document.uploaded_by_user_id is None
    assert portal_document.document_type == "ANTES_SERVICIO"
    assert portal_document.file_name == "fachada.jpg"
    assert portal_document.file_path == f"/uploads/{service_request.media[0].storage_path}"
    assert portal_document.file_mime == "image/jpeg"
    assert portal_document.file_size == len(b"imagen")


def test_customer_portal_allows_multiple_orders_for_same_client(db):
    make_organization(db, "portal-multi-org", "Portal Multi Org")
    first = create_customer_request_and_order(
        db,
        make_portal_payload(
            idempotency_key="multi-1",
            requester_email="multi@example.com",
            requester_phone="+52 998 000 0201",
            problem_description="Primer servicio",
        ),
    )
    second = create_customer_request_and_order(
        db,
        make_portal_payload(
            idempotency_key="multi-2",
            requester_email="multi@example.com",
            requester_phone="+52 998 000 0201",
            problem_description="Segundo servicio",
        ),
    )
    db.commit()
    db.refresh(first)
    db.refresh(second)

    assert first.lead_id == second.lead_id
    assert first.id != second.id
    assert first.service_order.id != second.service_order.id
    assert first.service_order.order_number == "TS-2026-000001"
    assert second.service_order.order_number == "TS-2026-000002"
    assert db.query(ServiceOrder).filter(ServiceOrder.lead_id == first.lead_id).count() == 2


def test_customer_portal_is_idempotent_per_request_key(db):
    make_organization(db, "portal-dedupe-org", "Portal Dedupe Org")
    payload = make_portal_payload(idempotency_key="dedupe-1")

    first = create_customer_request_and_order(db, payload)
    second = create_customer_request_and_order(db, payload)
    db.commit()

    assert first.id == second.id
    assert db.query(ServiceRequest).count() == 1
    assert db.query(ServiceOrder).count() == 1


def test_customer_portal_rejects_invalid_upload(db):
    make_organization(db, "portal-invalid-upload", "Portal Invalid Upload")

    with pytest.raises(HTTPException) as blocked:
        create_customer_request_and_order(
            db,
            make_portal_payload(idempotency_key="invalid-upload-1"),
            files=[FakePortalUpload("malware.exe", "application/x-msdownload", b"x")],
        )

    assert blocked.value.status_code == 400
    assert db.query(ServiceRequest).count() == 0


def test_customer_portal_requires_description_or_media(db):
    make_organization(db, "portal-missing-body", "Portal Missing Body")

    with pytest.raises(HTTPException) as blocked:
        create_customer_request_and_order(
            db,
            make_portal_payload(idempotency_key="missing-body-1", problem_description=""),
        )

    assert blocked.value.status_code == 400
    assert db.query(ServiceRequest).count() == 0


def test_public_request_status_does_not_expose_private_customer_data(db):
    make_organization(db, "portal-public-safe", "Portal Public Safe")
    service_request = create_customer_request_and_order(
        db,
        make_portal_payload(
            idempotency_key="public-safe-1",
            requester_name="Cliente Confidencial",
            requester_email="confidencial@example.com",
            requester_phone="+52 998 555 0101",
            address_line1="Direccion Privada 123",
        ),
    )
    db.commit()

    status = service_request_public_status(service_request)
    serialized = json.dumps(status, default=str, ensure_ascii=False)

    assert status["tracking_token"] == service_request.tracking_token
    assert status["order_number"] == "TS-2026-000001"
    assert status["tracking_url"].endswith(f"/acompanhar/{service_request.tracking_token}")
    assert "requester_email" not in status
    assert "requester_phone" not in status
    assert "client_name" not in status
    assert "address" not in status
    assert "Cliente Confidencial" not in serialized
    assert "confidencial@example.com" not in serialized
    assert "9985550101" not in serialized
    assert "Direccion Privada" not in serialized


def test_sales_queue_triage_and_assignments_are_org_scoped(db):
    org_a = make_organization(db, "portal-sales-a", "Portal Sales A")
    org_b = make_organization(db, "portal-sales-b", "Portal Sales B")
    admin = make_user(db, "admin-sales", "ROOT", organization_id=org_a.id)
    supervisor = make_user(db, "supervisor-sales", "GERENTE", organization_id=org_a.id)
    technician = make_user(db, "tecnico-sales", "BROKER", organization_id=org_a.id)
    other_admin = make_user(db, "admin-sales-b", "ROOT", organization_id=org_b.id)
    other_technician = make_user(db, "tecnico-sales-b", "BROKER", organization_id=org_b.id)
    service_request = create_customer_request_and_order(
        db,
        make_portal_payload(idempotency_key="sales-queue-1"),
        actor=admin,
    )
    db.commit()
    db.refresh(service_request)

    visible = list_sales_service_requests(db=db, actor=admin)
    hidden = list_sales_service_requests(db=db, actor=other_admin)
    assert len(visible) == 1
    assert visible[0]["id"] == service_request.id
    assert visible[0]["order_number"] == service_request.service_order.order_number
    assert visible[0]["client_name"] == "Cliente Portal"
    assert hidden == []

    triaged = triage_service_request(
        service_request.id,
        TriagePayload(status="TRIAGED", supervisor_user_id=supervisor.id),
        db=db,
        actor=admin,
    )
    assert triaged["status"] == "TRIAGED"
    db.refresh(service_request)
    order_id = service_request.service_order.id

    supervisor_result = assign_service_order_supervisor(
        order_id,
        AssignUserPayload(user_id=supervisor.id),
        db=db,
        actor=admin,
    )
    assert supervisor_result["supervisor_user_id"] == supervisor.id

    technician_result = assign_service_order_technician(
        order_id,
        AssignUserPayload(user_id=technician.id),
        db=db,
        actor=admin,
    )
    assert technician_result["responsible_user_id"] == technician.id

    with pytest.raises(HTTPException) as blocked:
        assign_service_order_technician(
            order_id,
            AssignUserPayload(user_id=other_technician.id),
            db=db,
            actor=admin,
        )
    assert blocked.value.status_code == 400
