import os
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi.routing import APIRoute
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.connection import Base
from app.main import app
from app.models.lead import Lead
from app.models.lead_event import LeadEvent
from app.models.lead_document import LeadDocument
from app.models.notification import Notification
from app.models.organization import Organization
from app.models.service_order import ServiceOrder
from app.models.service_opportunity import ServiceOpportunity
from app.models.commercial_subscription import CommercialSubscription, PlanChangeEvent
from app.models.commercial_upgrade_intent import CommercialUpgradeIntent
from app.routes.pablo_routes import PabloChatRequest, pablo_chat
from app.models.support_ticket import SupportTicket
from app.models.user import User
from app.models.notification import WebPushSubscription
from app.services.pablo_actions_service import (
    _drafts,
    _lead_payload,
    cancel_client_draft,
    cancel_operational_proposal,
    correct_operational_proposal,
    confirm_client_draft,
    confirm_operational_proposal,
    is_create_client_request,
    process_operational_message,
    process_client_message,
)
from app.services.pablo_audio_service import transcribe_audio
from app.services.pablo_context_service import build_context
from app.services.pablo_vision_service import (
    create_vision_session,
    discard_vision,
    expire_vision_for_test,
    get_active_vision,
)
from app.services.pablo_location_service import (
    create_location_session,
    discard_location,
    expire_location_for_test,
    get_active_location,
)
from app.services.marketplace_service import claim_opportunity, list_opportunities


class PabloLiveContextTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(
            bind=cls.engine,
            tables=[
                Organization.__table__,
                User.__table__,
                Lead.__table__,
                LeadEvent.__table__,
                ServiceOrder.__table__,
                LeadDocument.__table__,
                SupportTicket.__table__,
                Notification.__table__,
                WebPushSubscription.__table__,
                ServiceOpportunity.__table__,
                CommercialSubscription.__table__,
                PlanChangeEvent.__table__,
                CommercialUpgradeIntent.__table__,
            ],
        )
        cls.Session = sessionmaker(bind=cls.engine)

    def setUp(self):
        self.db = self.Session()
        suffix = uuid4().hex[:10]
        self.org = Organization(name="Org Teste", slug=f"org-pablo-live-{suffix}")
        self.other_org = Organization(name="Outra Org", slug=f"outra-org-pablo-live-{suffix}")
        self.db.add_all([self.org, self.other_org])
        self.db.flush()
        self.actor = User(
            username=f"tecnico-pablo-{suffix}",
            full_name="Tecnico Pablo",
            password_hash="hash",
            role="BROKER",
            organization_id=self.org.id,
            status="ACTIVE",
        )
        self.other_user = User(
            username=f"outro-tecnico-{suffix}",
            full_name="Outro Tecnico",
            password_hash="hash",
            role="BROKER",
            organization_id=self.org.id,
            status="ACTIVE",
        )
        self.db.add_all([self.actor, self.other_user])
        self.db.flush()

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def tearDown(self):
        cancel_client_draft(self.actor)
        cancel_operational_proposal(self.actor)
        self.db.rollback()
        self.db.close()

    def test_pablo_reports_free_for_new_organization(self):
        response = pablo_chat(PabloChatRequest(message="qual plano estou usando?"), self.db, self.actor)
        assert response.intent == "commercial"
        assert "FREE" in response.reply

    def test_context_is_bounded_and_excludes_other_actor_records(self):
        actor_leads = [
            Lead(
                organization_id=self.org.id,
                nome=f"Cliente {index}",
                contato=f"555-{index:04d}",
                email=f"cliente{index}@test.local",
                assigned_to_user_id=self.actor.id,
                urgencia="ALTA" if index == 0 else "NORMAL",
                pipeline="DIAGNOSTICO",
                valor_negocio=100 + index,
            )
            for index in range(35)
        ]
        hidden_lead = Lead(
            organization_id=self.org.id,
            nome="Cliente de outro tecnico",
            assigned_to_user_id=self.other_user.id,
        )
        other_org_lead = Lead(organization_id=self.other_org.id, nome="Cliente outra empresa")
        self.db.add_all(actor_leads + [hidden_lead, other_org_lead])
        self.db.flush()

        self.db.add_all([
            ServiceOrder(
                organization_id=self.org.id,
                lead_id=lead.id,
                order_number=f"OS-{index}",
                status="ABERTA",
            )
            for index, lead in enumerate(actor_leads[:25])
        ])
        self.db.add_all([
            SupportTicket(
                organization_id=self.org.id,
                protocol=f"T-{index}",
                module="CRM",
                priority="Alta",
                message=f"Problema {index}",
                status="ABERTO",
                created_by_user_id=self.actor.id,
            )
            for index in range(12)
        ] + [SupportTicket(
            organization_id=self.org.id,
            protocol="T-HIDDEN",
            module="CRM",
            priority="Alta",
            message="Não deve aparecer",
            status="ABERTO",
            created_by_user_id=self.other_user.id,
        )])
        self.db.add_all([
            Notification(
                organization_id=self.org.id,
                recipient_user_id=self.actor.id,
                type="TEST",
                title=f"Aviso {index}",
                message="Aviso autorizado",
                priority="NORMAL",
                idempotency_key=f"pablo-{index}",
            )
            for index in range(12)
        ] + [Notification(
            organization_id=self.org.id,
            recipient_user_id=self.other_user.id,
            type="TEST",
            title="Aviso privado",
            message="Não deve aparecer",
            priority="NORMAL",
            idempotency_key="pablo-hidden",
        )])
        self.db.commit()

        context = build_context(self.db, self.actor)

        self.assertEqual(context["limits"]["clients"], {"total_available": 35, "sent": 30})
        self.assertEqual(context["limits"]["service_orders"], {"total_available": 25, "sent": 20})
        self.assertEqual(context["limits"]["tickets"], {"total_available": 12, "sent": 10})
        self.assertEqual(context["limits"]["notifications"], {"total_available": 12, "sent": 10})
        self.assertEqual(context["clients"][0]["name"], "Cliente 0")
        self.assertNotIn("password_hash", context)
        self.assertNotIn("Cliente de outro tecnico", {client["name"] for client in context["clients"]})
        self.assertNotIn("Cliente outra empresa", {client["name"] for client in context["clients"]})
        self.assertNotIn("T-HIDDEN", {ticket["protocol"] for ticket in context["tickets"]})
        self.assertNotIn("Aviso privado", {notification["title"] for notification in context["notifications"]})

    def test_transcription_is_mocked_and_does_not_store_audio(self):
        response = httpx.Response(
            200,
            json={"text": "quantos clientes tenho"},
            request=httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions"),
        )
        with patch.dict(os.environ, {
            "PABLO_AI_API_KEY": "test-audio-key",
            "PABLO_AUDIO_MODEL": "test-transcription-model",
        }, clear=True), patch("httpx.Client.post", return_value=response) as post:
            text = transcribe_audio(
                filename="voice.webm",
                content=b"audio-bytes",
                content_type="audio/webm",
            )
        self.assertEqual(text, "quantos clientes tenho")
        self.assertEqual(post.call_args.args[0], "https://api.openai.com/v1/audio/transcriptions")

    def test_broker_builds_isolated_draft_and_confirmation_uses_authenticated_actor(self):
        proposal = process_client_message(self.actor, "Quero cadastrar cliente")
        self.assertEqual(proposal["status"], "PENDING_INPUT")
        process_client_message(self.actor, "Carlos Gomez")
        proposal = process_client_message(self.actor, "+52 998 123 4567")
        self.assertEqual(proposal["status"], "PENDING_CONFIRMATION")

        other_proposal = process_client_message(self.other_user, "Quero cadastrar cliente")
        self.assertNotEqual(proposal["data"], other_proposal["data"])

        with patch("app.services.pablo_actions_service.dispatch_web_push_for_notification_ids"):
            result = confirm_client_draft(self.db, self.actor)
        self.assertEqual(result["message"], "Cliente criado com sucesso.")
        lead = self.db.query(Lead).filter(Lead.id == result["client_id"]).one()
        self.assertEqual(lead.organization_id, self.actor.organization_id)
        self.assertEqual(lead.assigned_to_user_id, self.actor.id)
        self.assertEqual(lead.nome, "Carlos Gomez")
        untrusted_payload = _lead_payload({
            "name": "Ignorado",
            "phone": "+52 998 999 0000",
            "organization_id": 999999,
            "created_by_user_id": 999999,
        })
        self.assertNotIn("organization_id", untrusted_payload.model_dump())
        self.assertNotIn("created_by_user_id", untrusted_payload.model_dump())
        self.assertIsNone(process_client_message(self.actor, "confirmar"))
        with self.assertRaises(Exception):
            confirm_client_draft(self.db, self.actor)
        audit = self.db.query(LeadEvent).filter(
            LeadEvent.lead_id == lead.id,
            LeadEvent.event_type == "PABLO_ACTION_CREATE_CLIENT",
        ).one()
        self.assertEqual(audit.actor_id, self.actor.id)

    def test_draft_cancel_and_expiration_do_not_create_client(self):
        existing_leads = self.db.query(Lead).count()
        process_client_message(self.actor, "Quero cadastrar cliente")
        self.assertTrue(cancel_client_draft(self.actor))
        self.assertIsNone(process_client_message(self.actor, "confirmar"))

        process_client_message(self.actor, "Quero cadastrar cliente")
        _drafts[(self.actor.organization_id, self.actor.id)]["expires_at"] -= __import__("datetime").timedelta(minutes=16)
        replacement = process_client_message(self.actor, "Quero cadastrar cliente")
        self.assertEqual(replacement["status"], "PENDING_INPUT")
        self.assertEqual(self.db.query(Lead).count(), existing_leads)

    def test_production_roberto_regression_creates_complete_pending_draft(self):
        message = """Pablo, crie um novo cliente para mim.
Nome: Roberto Almeida Ferreira
Telefone/WhatsApp: +52 998 555 7821
E-mail: roberto.almeida.teste@example.com
Empresa: Hotel Mar Azul Teste
Tipo de serviço: Manutenção de ar-condicionado
Valor estimado: MX$ 48.500,00
Urgência: Alta
Cidade: Cancún
Estado: Quintana Roo
País: México
Observação: Cliente solicitou diagnóstico de 12 aparelhos de ar-condicionado e possível contrato de manutenção preventiva."""
        proposal = process_client_message(self.actor, message)
        self.assertEqual(proposal["action"], "CREATE_CLIENT")
        self.assertEqual(proposal["status"], "PENDING_CONFIRMATION")
        self.assertEqual(proposal["missing_fields"], [])
        self.assertEqual(proposal["data"]["name"], "Roberto Almeida Ferreira")
        self.assertEqual(proposal["data"]["phone"], "+52 998 555 7821")
        self.assertEqual(proposal["data"]["email"], "roberto.almeida.teste@example.com")
        self.assertEqual(proposal["data"]["company"], "Hotel Mar Azul Teste")
        self.assertEqual(proposal["data"]["service_value"], 48500.0)
        self.assertEqual(proposal["data"]["urgency"], "Alta")

    def test_create_client_intent_supports_portuguese_spanish_and_english(self):
        self.assertTrue(is_create_client_request("crie um novo cliente"))
        self.assertTrue(is_create_client_request("quiero registrar un cliente"))
        self.assertTrue(is_create_client_request("create a new customer"))

    def test_pablo_chat_and_transcribe_routes_are_registered(self):
        routes = {
            (route.path, method)
            for route in app.routes
            if isinstance(route, APIRoute)
            for method in route.methods
        }
        self.assertIn(("/pablo/chat", "POST"), routes)
        self.assertIn(("/pablo/transcribe", "POST"), routes)

    def test_change_pipeline_requires_confirmation_and_uses_actor_scope(self):
        lead = Lead(
            organization_id=self.org.id,
            nome="Javier Edmundo",
            contato="555-1000",
            assigned_to_user_id=self.actor.id,
            pipeline="ATENDIMENTO",
        )
        self.db.add(lead)
        self.db.commit()
        proposal = process_operational_message(self.db, self.actor, "Você pode colocar o Javier Edmundo para diagnóstico?")
        self.assertEqual(proposal["action"], "CHANGE_PIPELINE")
        self.assertEqual(proposal["status"], "PENDING_CONFIRMATION")
        self.db.refresh(lead)
        self.assertEqual(lead.pipeline, "ATENDIMENTO")
        result = confirm_operational_proposal(self.db, self.actor)
        self.assertEqual(result["action"], "CHANGE_PIPELINE")
        self.db.refresh(lead)
        self.assertEqual(lead.pipeline, "VISITA")
        with self.assertRaises(Exception):
            confirm_operational_proposal(self.db, self.actor)

    def test_update_client_proposal_is_scoped_and_confirmed_once(self):
        lead = Lead(
            organization_id=self.org.id,
            nome="Javier Edmundo",
            contato="555-1000",
            assigned_to_user_id=self.actor.id,
            pipeline="ATENDIMENTO",
        )
        self.db.add(lead)
        self.db.commit()
        proposal = process_operational_message(self.db, self.actor, "troque o telefone do Javier Edmundo para +52 555 777 8888")
        self.assertEqual(proposal["action"], "UPDATE_CLIENT")
        self.assertEqual(lead.contato, "555-1000")
        confirm_operational_proposal(self.db, self.actor)
        self.db.refresh(lead)
        self.assertEqual(lead.contato, "+52 555 777 8888")

    def test_update_client_parser_separates_target_connectors_and_new_value(self):
        leads = [
            Lead(organization_id=self.org.id, nome="Javier Edmundo", contato="old-1", assigned_to_user_id=self.actor.id),
            Lead(organization_id=self.org.id, nome="Javier", email="old-2@example.com", assigned_to_user_id=self.actor.id),
            Lead(organization_id=self.org.id, nome="Carlos", cidade="old-city", assigned_to_user_id=self.actor.id),
        ]
        self.db.add_all(leads)
        self.db.commit()

        cases = [
            ("cambia el teléfono de Javier Edmundo a +52 555 777 8888", "contato", "+52 555 777 8888"),
            ("update Javier's email to teste@example.com", "email", "teste@example.com"),
            ("mude a cidade do Carlos para Cancún", "cidade", "Cancún"),
        ]
        for message, field, expected in cases:
            proposal = process_operational_message(self.db, self.actor, message)
            self.assertEqual(proposal["action"], "UPDATE_CLIENT")
            self.assertEqual(proposal["changes"][field], expected)
            self.assertNotIn(" para ", proposal["changes"][field])
            self.assertNotIn(" to ", proposal["changes"][field])
            cancel_operational_proposal(self.actor)

    def test_update_client_conversation_collects_field_target_and_value(self):
        lead = Lead(
            organization_id=self.org.id,
            nome="Javier Edmundo",
            contato="555-1000",
            assigned_to_user_id=self.actor.id,
        )
        self.db.add(lead)
        self.db.commit()

        proposal = process_operational_message(self.db, self.actor, "cadastra telefone")
        self.assertEqual(proposal["action"], "UPDATE_CLIENT")
        self.assertEqual(proposal["missing_fields"], ["client"])

        proposal = process_operational_message(self.db, self.actor, "Javier Edmundo")
        self.assertEqual(proposal["target"]["name"], "Javier Edmundo")
        self.assertEqual(proposal["missing_fields"], ["value"])
        self.assertEqual(proposal["current"]["contato"], "555-1000")

        proposal = process_operational_message(self.db, self.actor, "+52 555 777 8888")
        self.assertEqual(proposal["status"], "PENDING_CONFIRMATION")
        self.assertEqual(proposal["changes"], {"contato": "+52 555 777 8888"})

    def test_update_service_order_proposal_requires_confirmation(self):
        lead = Lead(
            organization_id=self.org.id,
            nome="Javier Edmundo",
            assigned_to_user_id=self.actor.id,
            pipeline="VISITA",
        )
        self.db.add(lead)
        self.db.flush()
        order = ServiceOrder(
            organization_id=self.org.id,
            lead_id=lead.id,
            order_number=f"TS-TEST-{lead.id}",
            status="EM_DIAGNOSTICO",
            responsible_user_id=self.actor.id,
        )
        self.db.add(order)
        self.db.commit()

        proposal = process_operational_message(
            self.db,
            self.actor,
            "atualize a OS do Javier Edmundo status para CONCLUIDA",
        )
        self.assertEqual(proposal["action"], "UPDATE_SERVICE_ORDER")
        self.assertEqual(proposal["status"], "PENDING_CONFIRMATION")
        self.assertEqual(proposal["changes"]["status"], "CONCLUIDA")
        self.assertEqual(order.status, "EM_DIAGNOSTICO")
        confirm_operational_proposal(self.db, self.actor)
        self.db.refresh(order)
        self.assertEqual(order.status, "CONCLUIDA")

    def test_operational_correction_preserves_target_and_returns_to_input(self):
        lead = Lead(
            organization_id=self.org.id,
            nome="Javier Edmundo",
            contato="555-1000",
            assigned_to_user_id=self.actor.id,
        )
        self.db.add(lead)
        self.db.commit()
        proposal = process_operational_message(self.db, self.actor, "troque o telefone do Javier Edmundo para +52 555 777 8888")
        self.assertEqual(proposal["status"], "PENDING_CONFIRMATION")
        corrected = correct_operational_proposal(self.actor)
        self.assertEqual(corrected["status"], "PENDING_INPUT")
        self.assertEqual(corrected["target"]["name"], "Javier Edmundo")
        self.assertEqual(corrected["missing_fields"], ["value"])

    def test_vision_session_is_actor_org_scoped_and_expires(self):
        analysis = {"status": "ANALYZED", "fields": {"document_type": "passport", "name": "Pessoa Sensível", "passport_number": "SECRET"}}
        session = create_vision_session(self.actor, b"image", "image/png", "doc.png", analysis)
        self.assertTrue(session["vision_id"])
        self.assertNotIn("SECRET", str(session))
        self.assertIsNotNone(get_active_vision(self.actor, session["vision_id"]))
        self.assertIsNone(get_active_vision(self.other_user, session["vision_id"]))
        expire_vision_for_test(self.actor)
        self.assertIsNone(get_active_vision(self.actor, session["vision_id"]))

    def test_vision_attach_requires_confirmation_and_persists_after_confirm(self):
        lead = Lead(organization_id=self.org.id, nome="Javier Edmundo", assigned_to_user_id=self.actor.id)
        self.db.add(lead)
        self.db.flush()
        order = ServiceOrder(organization_id=self.org.id, lead_id=lead.id, order_number="TS-2026-000019", status="ABERTA")
        self.db.add(order)
        self.db.commit()
        create_vision_session(self.actor, b"\x89PNG\r\n\x1a\nvision", "image/png", "passport.png", {"status": "ANALYZED", "fields": {"document_type": "passport"}})
        with tempfile.TemporaryDirectory() as folder:
            with patch("app.services.pablo_actions_service.UPLOADS_DIR", __import__("pathlib").Path(folder)):
                proposal = process_operational_message(self.db, self.actor, "anexa isso na OS do Javier Edmundo")
                self.assertEqual(proposal["action"], "ATTACH_EVIDENCE")
                self.assertEqual(proposal["status"], "PENDING_CONFIRMATION")
                self.assertEqual(self.db.query(LeadDocument).count(), 0)
                result = confirm_operational_proposal(self.db, self.actor)
                self.assertEqual(result["action"], "ATTACH_EVIDENCE")
                self.assertEqual(self.db.query(LeadDocument).count(), 1)

    def test_vision_applies_only_allowed_fields_to_client_and_draft(self):
        lead = Lead(organization_id=self.org.id, nome="Javier Edmundo", contato="555-1000", assigned_to_user_id=self.actor.id)
        self.db.add(lead)
        self.db.commit()
        create_vision_session(self.actor, b"image", "image/png", "label.png", {"status": "ANALYZED", "fields": {"phone": "+52 555 777 8888", "city": "Cancún", "passport_number": "SECRET"}})
        proposal = process_operational_message(self.db, self.actor, "usa esses dados no Javier Edmundo")
        self.assertEqual(proposal["action"], "UPDATE_CLIENT")
        self.assertEqual(proposal["changes"], {"contato": "+52 555 777 8888", "cidade": "Cancún"})
        cancel_operational_proposal(self.actor)
        create_vision_session(self.actor, b"image", "image/png", "label.png", {"status": "ANALYZED", "fields": {"phone": "+52 555 777 8888", "city": "Cancún", "passport_number": "SECRET"}})
        draft = process_client_message(self.actor, "quero cadastrar este cliente")
        self.assertEqual(draft["data"]["phone"], "+52 555 777 8888")
        self.assertNotIn("passport_number", draft["data"])

    def test_frontend_vision_uses_safe_display_not_raw_json(self):
        html = __import__("pathlib").Path(__file__).parents[2].joinpath("frontend", "index.html").read_text(encoding="utf-8")
        self.assertIn("renderPabloVisionResult", html)
        self.assertNotIn("JSON.stringify(analysis.fields)", html)

    def test_location_session_is_scoped_and_expires_without_persisting(self):
        session = create_location_session(self.actor, 21.1619, -86.8515, 12.4)
        self.assertTrue(session["location_id"])
        self.assertNotIn("21.1619", str(session))
        self.assertIsNotNone(get_active_location(self.actor, session["location_id"]))
        self.assertIsNone(get_active_location(self.other_user, session["location_id"]))
        expire_location_for_test(self.actor)
        self.assertIsNone(get_active_location(self.actor))

    def test_location_client_requires_confirmation_and_persists_only_after_confirm(self):
        lead = Lead(organization_id=self.org.id, nome="Javier Edmundo", assigned_to_user_id=self.actor.id)
        self.db.add(lead)
        self.db.commit()
        create_location_session(self.actor, 21.1619, -86.8515, 8)
        proposal = process_operational_message(self.db, self.actor, "usa essa localização no Javier Edmundo")
        self.assertEqual(proposal["action"], "UPDATE_CLIENT")
        self.assertEqual(proposal["status"], "PENDING_CONFIRMATION")
        self.assertIsNone(lead.latitude)
        confirm_operational_proposal(self.db, self.actor)
        self.db.refresh(lead)
        self.assertEqual(lead.latitude, "21.1619")
        self.assertEqual(self.db.query(LeadEvent).filter(LeadEvent.event_type == "PABLO_ACTION_UPDATE_LOCATION").count(), 1)

    def test_location_os_is_scoped_confirmed_and_cancel_does_not_persist(self):
        lead = Lead(organization_id=self.org.id, nome="Javier Edmundo", assigned_to_user_id=self.actor.id)
        self.db.add(lead)
        self.db.flush()
        order = ServiceOrder(organization_id=self.org.id, lead_id=lead.id, order_number="TS-LOC-19", status="ABERTA")
        self.db.add(order)
        self.db.commit()
        create_location_session(self.actor, 21.1619, -86.8515, 10)
        proposal = process_operational_message(self.db, self.actor, "registra onde fiz o atendimento na OS TS-LOC-19")
        self.assertEqual(proposal["action"], "UPDATE_SERVICE_ORDER")
        self.assertEqual(proposal["status"], "PENDING_CONFIRMATION")
        cancel_operational_proposal(self.actor)
        self.db.refresh(lead)
        self.assertIsNone(lead.latitude)
        self.assertIsNotNone(get_active_location(self.actor))

    def test_location_pending_target_consumes_full_order_reply(self):
        lead = Lead(organization_id=self.org.id, nome="Teste 03", assigned_to_user_id=self.actor.id)
        self.db.add(lead)
        self.db.flush()
        self.db.add(ServiceOrder(organization_id=self.org.id, lead_id=lead.id, order_number="TS-LOC-000005-A", status="ABERTA"))
        self.db.commit()
        create_location_session(self.actor, 21.1619, -86.8515, 35)
        pending = process_operational_message(self.db, self.actor, "enviar minha localização para base")
        self.assertEqual(pending["status"], "PENDING_INPUT")
        self.assertEqual(pending["missing_fields"], ["target"])
        proposal = process_operational_message(self.db, self.actor, "TS-LOC-000005-A")
        self.assertEqual(proposal["action"], "UPDATE_SERVICE_ORDER")
        self.assertEqual(proposal["target"]["order_number"], "TS-LOC-000005-A")
        self.assertEqual(proposal["target"]["name"], "Teste 03")
        self.assertEqual(proposal["status"], "PENDING_CONFIRMATION")

    def test_location_buttons_map_to_explicit_client_or_order_state(self):
        lead = Lead(organization_id=self.org.id, nome="Javier Edmundo", assigned_to_user_id=self.actor.id)
        self.db.add(lead)
        self.db.flush()
        self.db.add(ServiceOrder(organization_id=self.org.id, lead_id=lead.id, order_number="TS-LOC-000005-B", status="ABERTA"))
        self.db.commit()
        create_location_session(self.actor, 21.1619, -86.8515, 35)
        pending = process_operational_message(self.db, self.actor, "usa essa localização na OS")
        self.assertEqual(pending["target_type"], "SERVICE_ORDER")
        proposal = process_operational_message(self.db, self.actor, "TS-LOC-000005-B")
        self.assertEqual(proposal["action"], "UPDATE_SERVICE_ORDER")
        cancel_operational_proposal(self.actor)
        create_location_session(self.actor, 21.1619, -86.8515, 35)
        pending = process_operational_message(self.db, self.actor, "usa essa localização no cliente")
        self.assertEqual(pending["target_type"], "CLIENT")
        proposal = process_operational_message(self.db, self.actor, "Javier Edmundo")
        self.assertEqual(proposal["action"], "UPDATE_CLIENT")
        self.assertEqual(proposal["target"]["name"], "Javier Edmundo")

    def test_short_number_resolves_os_only_for_pending_order_target(self):
        lead = Lead(organization_id=self.org.id, nome="Teste 03", assigned_to_user_id=self.actor.id)
        self.db.add(lead)
        self.db.flush()
        self.db.add(ServiceOrder(organization_id=self.org.id, lead_id=lead.id, order_number="TS-LOC-000019", status="ABERTA"))
        self.db.commit()
        create_location_session(self.actor, 21.1619, -86.8515, 35)
        pending = process_operational_message(self.db, self.actor, "usa essa localização na OS")
        self.assertEqual(pending["status"], "PENDING_INPUT")
        proposal = process_operational_message(self.db, self.actor, "19")
        self.assertEqual(proposal["target"]["order_number"], "TS-LOC-000019")
        cancel_operational_proposal(self.actor)
        self.assertIsNone(process_operational_message(self.db, self.actor, "19"))

    def test_pending_location_rejects_order_from_other_organization(self):
        lead = Lead(organization_id=self.other_org.id, nome="Fora da organização", assigned_to_user_id=None)
        self.db.add(lead)
        self.db.flush()
        self.db.add(ServiceOrder(organization_id=self.other_org.id, lead_id=lead.id, order_number="TS-OTHER-000005", status="ABERTA"))
        self.db.commit()
        create_location_session(self.actor, 21.1619, -86.8515, 35)
        pending = process_operational_message(self.db, self.actor, "usa essa localização na OS")
        self.assertEqual(process_operational_message(self.db, self.actor, "TS-OTHER-000005")["status"], "PENDING_INPUT")
        self.assertEqual(pending["target_type"], "SERVICE_ORDER")

    def test_pending_location_rejects_order_outside_broker_scope(self):
        lead = Lead(organization_id=self.org.id, nome="Cliente de outro técnico", assigned_to_user_id=self.other_user.id)
        self.db.add(lead)
        self.db.flush()
        self.db.add(ServiceOrder(organization_id=self.org.id, lead_id=lead.id, order_number="TS-HIDDEN-000005", status="ABERTA"))
        self.db.commit()
        create_location_session(self.actor, 21.1619, -86.8515, 35)
        process_operational_message(self.db, self.actor, "usa essa localização na OS")
        unresolved = process_operational_message(self.db, self.actor, "TS-HIDDEN-000005")
        self.assertEqual(unresolved["status"], "PENDING_INPUT")
        self.assertEqual(unresolved["missing_fields"], ["target"])

    def test_location_proposal_card_hides_coordinates_and_names_destination(self):
        html = __import__("pathlib").Path(__file__).parents[2].joinpath("frontend", "index.html").read_text(encoding="utf-8")
        self.assertIn("ATUALIZAÇÃO DE LOCALIZAÇÃO", html)
        self.assertIn("Localização do cliente associado à OS", html)
        self.assertIn("proposal.location_accuracy", html)

    def test_marketplace_feed_is_sanitized_and_available(self):
        opportunity = ServiceOpportunity(
            public_id="MKT-SAFE-001", organization_id=self.org.id, source="MARKETPLACE",
            service_type="AR-CONDICIONADO", segment="HOTEL", city="Cancún", state="Quintana Roo",
            country="México", approx_latitude=21.16, approx_longitude=-86.85, urgency="ALTA",
            estimated_value_min=1800, estimated_value_max=2500, description_public="Equipo no enfría.",
        )
        self.db.add(opportunity)
        self.db.commit()
        feed = list_opportunities(self.db, self.actor)
        self.assertEqual(feed[0]["public_id"], "MKT-SAFE-001")
        self.assertNotIn("latitude", feed[0])
        self.assertNotIn("longitude", feed[0])
        self.assertNotIn("phone", feed[0])
        self.assertNotIn("email", feed[0])

    def test_marketplace_claim_is_single_and_exposes_private_data_only_after_claim(self):
        opportunity = ServiceOpportunity(
            public_id="MKT-CLAIM-001", organization_id=self.org.id, source="MARKETPLACE",
            service_type="HIDRAULICA", city="Cancún", country="México", urgency="EMERGENCIA",
            description_public="Fuga urgente.",
        )
        self.db.add(opportunity)
        self.db.commit()
        first = claim_opportunity(self.db, self.actor, "MKT-CLAIM-001")
        self.assertEqual(first["opportunity"]["status"], "CLAIMED")
        self.assertEqual(first["opportunity"]["client"]["name"], "Oportunidade MKT-CLAIM-001")
        with self.assertRaises(Exception) as conflict:
            claim_opportunity(self.db, self.other_user, "MKT-CLAIM-001")
        self.assertIn("aceita por outro", str(conflict.exception))
        self.assertEqual(self.db.query(ServiceOpportunity).filter(ServiceOpportunity.public_id == "MKT-CLAIM-001").one().claimed_by_user_id, self.actor.id)

    def test_marketplace_claim_isolated_by_organization_and_pablo_requires_confirmation(self):
        other = ServiceOpportunity(public_id="MKT-OTHER-001", organization_id=self.other_org.id, source="MARKETPLACE", service_type="ELETRICA", city="Cancún")
        own = ServiceOpportunity(public_id="MKT-PABLO-001", organization_id=self.org.id, source="MARKETPLACE", service_type="AR-CONDICIONADO", city="Cancún", urgency="ALTA")
        self.db.add_all([other, own])
        self.db.commit()
        self.assertEqual(list_opportunities(self.db, self.actor)[0]["public_id"], "MKT-PABLO-001")
        proposal = process_operational_message(self.db, self.actor, "Pablo, pega o serviço mais urgente")
        self.assertEqual(proposal["action"], "CLAIM_MARKETPLACE_OPPORTUNITY")
        self.assertEqual(proposal["status"], "PENDING_CONFIRMATION")
        self.assertEqual(self.db.query(ServiceOpportunity).filter(ServiceOpportunity.public_id == "MKT-PABLO-001").one().status, "AVAILABLE")
        cancel_operational_proposal(self.actor)

    def test_pablo_marketplace_confirmation_uses_atomic_claim_service(self):
        self.db.add(ServiceOpportunity(public_id="MKT-PABLO-CONFIRM", organization_id=self.org.id, source="MARKETPLACE", service_type="ELETRICA", city="Cancún", urgency="EMERGENCIA"))
        self.db.commit()
        proposal = process_operational_message(self.db, self.actor, "Pablo, pega o serviço mais urgente")
        self.assertEqual(proposal["opportunity_id"], "MKT-PABLO-CONFIRM")
        self.assertEqual(self.db.query(ServiceOpportunity).filter(ServiceOpportunity.public_id == "MKT-PABLO-CONFIRM").one().status, "AVAILABLE")
        result = confirm_operational_proposal(self.db, self.actor)
        self.assertEqual(result["status"], "EXECUTED")
        self.assertEqual(self.db.query(ServiceOpportunity).filter(ServiceOpportunity.public_id == "MKT-PABLO-CONFIRM").one().status, "CLAIMED")
        self.assertEqual(self.db.query(LeadEvent).filter(LeadEvent.event_type == "PABLO_MARKETPLACE_CLAIM").count(), 1)


if __name__ == "__main__":
    unittest.main()
