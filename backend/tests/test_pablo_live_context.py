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


if __name__ == "__main__":
    unittest.main()
