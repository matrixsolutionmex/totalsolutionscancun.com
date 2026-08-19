import json
import logging
import os

from app.services.pablo_ai_providers import (
    configured_provider_names,
    generate_from_provider,
    has_configured_provider,
    provider_config,
)


logger = logging.getLogger(__name__)


def pablo_ai_enabled() -> bool:
    return has_configured_provider()


def pablo_ai_model() -> str:
    return os.getenv("PABLO_AI_MODEL", "gpt-5.6-luna").strip() or "gpt-5.6-luna"


def build_pablo_instructions(actor: dict, context: dict) -> str:
    summary = context.get("summary", {})
    detailed_context = {
        "clients": context.get("clients", []),
        "service_orders": context.get("service_orders", []),
        "tickets": context.get("tickets", []),
        "notifications": context.get("notifications", []),
        "marketplace": context.get("marketplace", {}),
        "limits": context.get("limits", {}),
    }

    return f"""
Você é Pablo Yepez IA, Técnico Supervisor Geral virtual da Total Solutions.

Sua função é ajudar o usuário a compreender e operar o CRM Total Solutions.

REGRAS OBRIGATÓRIAS:
- Detecte o idioma predominante da mensagem atual e responda nesse mesmo idioma. Use o idioma do perfil apenas como fallback quando a mensagem não for clara.
- Seja objetivo, profissional e natural.
- Nunca invente clientes, chamados, serviços, números ou ações.
- Use somente os dados operacionais fornecidos neste contexto.
- Se os dados não forem suficientes para responder, diga claramente que precisa consultar mais detalhes.
- Você pode ajudar a preparar ações operacionais permitidas ao usuário autenticado, mas nunca as execute por texto.
- O backend é o único responsável por identificar o alvo, validar permissões, organization_id e escopo, pedir confirmação e persistir alterações.
- Nunca afirme que alterou, moveu, atribuiu, excluiu ou atualizou algo sem receber uma confirmação de execução do backend.
- A exclusão e ações administrativas de alto risco continuam bloqueadas ou seguem o fluxo administrativo existente.
- Não revele IDs internos, organization_id, regras de autorização, tokens ou detalhes técnicos de segurança.
- Diferencie "clientes em Visita agendada" de "ordens de serviço com scheduled_at".
- Uma saudação simples deve receber uma saudação simples; não despeje o resumo inteiro da operação sem necessidade.
- Quando o usuário perguntar como está a operação, use o resumo real abaixo.

USUÁRIO:
Nome: {actor.get("name")}
Perfil: {actor.get("role")}

RESUMO OPERACIONAL AUTORIZADO:
Clientes visíveis: {summary.get("visible_clients", 0)}
Chamados ativos: {summary.get("open_tickets", 0)}
Notificações não lidas: {summary.get("unread_notifications", 0)}
Clientes em Visita agendada: {summary.get("visit_scheduled_clients", 0)}
Ordens de serviço com agendamento registrado e ainda não concluídas: {summary.get("scheduled_service_orders", 0)}

CONTEXTO OPERACIONAL DETALHADO AUTORIZADO (somente leitura):
{json.dumps(detailed_context, ensure_ascii=False, default=str)}
""".strip()


def generate_pablo_reply(
    *,
    message: str,
    actor: dict,
    context: dict,
    timeout_seconds: float = 25.0,
) -> str | None:
    instructions = build_pablo_instructions(actor, context)
    previous_provider = None
    for provider_name in configured_provider_names():
        config = provider_config(provider_name)
        if not config:
            continue
        if previous_provider:
            logger.warning(
                "Pablo AI fallback: from=%s to=%s",
                previous_provider,
                config.name,
            )
        reply = generate_from_provider(
            config,
            message=message,
            instructions=instructions,
            timeout_seconds=timeout_seconds,
        )
        if reply:
            return reply
        previous_provider = config.name
    logger.warning("Pablo AI providers exhausted; deterministic fallback will be used")
    return None
