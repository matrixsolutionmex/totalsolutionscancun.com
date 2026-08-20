"""Billing abstraction. Real gateways are intentionally not implemented in V1."""

from dataclasses import dataclass

from app.models.user import User
from app.services.entitlement_service import record_plan_change


@dataclass
class MockBillingProvider:
    name: str = "MOCK"

    def change_plan(self, db, actor: User, plan: str, reason: str | None = None, *, organization_id: int | None = None):
        return record_plan_change(db, actor, plan, reason, organization_id=organization_id)

    def create_checkout(self, *args, **kwargs):
        raise NotImplementedError("Checkout real não está habilitado")

    def create_subscription(self, *args, **kwargs):
        raise NotImplementedError("Assinatura real não está habilitada")

    def cancel_subscription(self, *args, **kwargs):
        raise NotImplementedError("Billing real não está habilitado")

    def handle_webhook(self, *args, **kwargs):
        raise NotImplementedError("Webhooks reais não estão habilitados")

    def get_subscription_status(self, *args, **kwargs):
        raise NotImplementedError("Billing real não está habilitado")
