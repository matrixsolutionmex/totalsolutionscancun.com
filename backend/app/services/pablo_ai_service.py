import json
import logging
import os

import httpx


logger = logging.getLogger(__name__)

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"


def pablo_ai_enabled() -> bool:
    return bool(os.getenv("OPENAI_API_KEY", "").strip())


def pablo_ai_model() -> str:
    return os.getenv("PABLO_AI_MODEL", "gpt-5.6-luna").strip() or "gpt-5.6-luna"


def extract_output_text(payload: dict) -> str | None:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    chunks = []

    for item in payload.get("output", []) or []:
        if item.get("type") != "message":
            continue

        for content in item.get("content", []) or []:
            if content.get("type") == "output_text":
                value = content.get("text")
                if isinstance(value, str) and value.strip():
                    chunks.append(value.strip())

    return "\n".join(chunks).strip() or None


def build_pablo_instructions(actor: dict, context: dict) -> str:
    summary = context.get("summary", {})

    return f"""
Você é Pablo Yepez IA, Técnico Supervisor Geral virtual da Total Solutions.

Sua função é ajudar o usuário a compreender e operar o CRM Total Solutions.

REGRAS OBRIGATÓRIAS:
- Responda no idioma usado pelo usuário.
- Seja objetivo, profissional e natural.
- Nunca invente clientes, chamados, serviços, números ou ações.
- Use somente os dados operacionais fornecidos neste contexto.
- Se os dados não forem suficientes para responder, diga claramente que precisa consultar mais detalhes.
- Você está em modo SOMENTE LEITURA.
- Nunca afirme que alterou, moveu, atribuiu, excluiu ou atualizou algo.
- Se o usuário pedir uma alteração, explique o que entendeu e diga que a ação exigirá confirmação quando essa capacidade estiver habilitada.
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
""".strip()


def generate_pablo_reply(
    *,
    message: str,
    actor: dict,
    context: dict,
    timeout_seconds: float = 25.0,
) -> str | None:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()

    if not api_key:
        return None

    payload = {
        "model": pablo_ai_model(),
        "instructions": build_pablo_instructions(actor, context),
        "input": message,
        "max_output_tokens": 500,
    }

    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.post(
                OPENAI_RESPONSES_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

        if response.status_code >= 400:
            logger.warning(
                "Pablo AI request failed: status=%s body=%s",
                response.status_code,
                response.text[:800],
            )
            return None

        data = response.json()
        return extract_output_text(data)

    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("Pablo AI unavailable: %s", exc)
        return None
