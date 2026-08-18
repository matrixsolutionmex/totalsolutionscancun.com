import logging
import html
import json
import os
import secrets
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.auth.jwt_handler import create_access_token, get_current_user, get_db, user_access_block_reason
from app.core.auth_security import (
    audit_auth_event,
    apply_public_rate_limits,
    clear_rate_limit,
    clear_session_cookies,
    consume_recovery_code,
    create_mfa_challenge_token,
    create_user_session,
    generate_recovery_codes,
    generate_totp_secret,
    hash_value,
    mfa_required_for_user,
    otpauth_url,
    public_turnstile_site_key,
    rate_limit_or_429,
    request_ip,
    revoke_sessions_for_user,
    turnstile_configured,
    validate_google_id_token_or_401,
    verify_mfa_challenge_token,
    verify_totp_code,
    verify_turnstile_or_403,
    decrypt_secret,
    encrypt_secret,
    now_utc,
)
from app.core.organization import get_or_create_default_organization
from app.core.security import hash_password, password_needs_upgrade, verify_password
from app.models.auth_security import PasswordResetToken, UserIdentity
from app.models.user import User
from app.models.user_lifecycle import UserReactivationRequest
from app.schemas.auth_schema import (
    AuthLoginRequest,
    AuthResponse,
    EmailVerificationChangeRequest,
    EmailVerificationResendRequest,
    GoogleLoginRequest,
    MfaSetupConfirmRequest,
    MfaSetupConfirmResponse,
    MfaSetupChallengeConfirmRequest,
    MfaSetupChallengeStartRequest,
    MfaSetupStartResponse,
    MfaVerifyRequest,
    PasswordRecoveryRequest,
    PasswordResetRequest,
    PublicAuthConfig,
    ReactivationRequestCreate,
    ReactivationRequestResponse,
    RegisterRequest,
    RegisterResponse,
)
from app.schemas.user_schema import UserResponse
from app.services.notification_service import create_notification


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


PUBLIC_AUTH_MESSAGE = "Si la información es válida, recibirás las próximas instrucciones."
PUBLIC_REGISTER_MESSAGE = "Enviamos un enlace de confirmación a tu correo electrónico."
PUBLIC_EMAIL_DELIVERY_UNAVAILABLE_MESSAGE = "No fue posible enviar el correo ahora. Inténtalo nuevamente en unos minutos."
EMAIL_VERIFICATION_TTL_MINUTES = 60
EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS = 60
EMAIL_CONFIRMED_REDIRECT_PATH = "/?email_confirmed=1"
EMAIL_VERIFICATION_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}


class _LocalRequest:
    method = "POST"
    headers = {}
    cookies = {}
    client = None


class _LocalResponse:
    def set_cookie(self, *_args, **_kwargs):
        return None

    def delete_cookie(self, *_args, **_kwargs):
        return None


def safe_request(request: Request | None) -> Request:
    return request or _LocalRequest()


def safe_response(response: Response | None) -> Response:
    return response or _LocalResponse()


@router.get("/public-config", response_model=PublicAuthConfig)
def public_config():
    return PublicAuthConfig(
        turnstile_site_key=public_turnstile_site_key() or None,
        turnstile_required=turnstile_configured(),
        google_client_id=os.getenv("GOOGLE_CLIENT_ID", "").strip() or None,
        public_signup_enabled=os.getenv("PUBLIC_SIGNUP_ENABLED", "true").strip().lower() != "false",
    )


def normalized_email(value: str) -> str:
    email = value.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise HTTPException(status_code=400, detail="Correo inválido")
    return email


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if not domain:
        return "c***@***"
    prefix = local[:2] if len(local) > 2 else local[:1]
    return f"{prefix}{'*' * max(3, len(local) - len(prefix))}@{domain}"


def email_delivery_missing_variables() -> list[str]:
    missing = []
    if not os.getenv("PUBLIC_BASE_URL", "").strip():
        missing.append("PUBLIC_BASE_URL")
    if not os.getenv("SMTP_HOST", "").strip():
        missing.append("SMTP_HOST")
    if not os.getenv("SMTP_PORT", "").strip():
        missing.append("SMTP_PORT")
    if not os.getenv("SMTP_USERNAME", "").strip():
        missing.append("SMTP_USERNAME")
    if not os.getenv("SMTP_PASSWORD", "").strip():
        missing.append("SMTP_PASSWORD")
    if not os.getenv("SMTP_FROM", "").strip():
        missing.append("SMTP_FROM")
    use_tls = os.getenv("SMTP_USE_TLS", "true").strip().lower() == "true"
    use_ssl = os.getenv("SMTP_USE_SSL", "false").strip().lower() == "true"
    if not use_tls and not use_ssl:
        missing.append("SMTP_USE_TLS_OR_SSL")
    if os.getenv("PUBLIC_BASE_URL", "").strip() and os.getenv("SMTP_FROM", "").strip() and not smtp_from_domain_is_authorized():
        missing.append("SMTP_FROM_AUTHORIZED_DOMAIN")
    return missing


def smtp_from_domain_is_authorized() -> bool:
    base_url = os.getenv("PUBLIC_BASE_URL", "").strip()
    smtp_from = os.getenv("SMTP_FROM", "").strip()
    if "@" not in smtp_from:
        return False
    app_host = urlparse(base_url).hostname or ""
    sender_domain = smtp_from.rsplit("@", 1)[1].lower()
    app_host = app_host.lower()
    return bool(app_host and sender_domain and (app_host == sender_domain or app_host.endswith(f".{sender_domain}")))


def email_verification_url(raw_token: str) -> str | None:
    base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        return None
    return f"{base_url}/auth/verify-email?token={quote(raw_token, safe='')}"


def resend_api_key() -> str:
    explicit_key = os.getenv("RESEND_API_KEY", "").strip()
    if explicit_key:
        return explicit_key
    if os.getenv("SMTP_HOST", "").strip().lower() == "smtp.resend.com":
        return os.getenv("SMTP_PASSWORD", "").strip()
    return ""


def verification_email_text(*, full_name: str | None, verification_url: str) -> str:
    greeting = (full_name or "Hola").strip()
    return "\n".join(
        [
            f"{greeting},",
            "",
            "Confirma tu correo para continuar con la solicitud de acceso a Total Solutions.",
            "El enlace vence en 60 minutos y solo puede usarse una vez.",
            "",
            verification_url,
            "",
            "Si no solicitaste este acceso, ignora este mensaje.",
        ]
    )


def verification_email_html(*, full_name: str | None, verification_url: str) -> str:
    base_url = os.getenv("PUBLIC_BASE_URL", "https://totalsolutionscancun.com").strip().rstrip("/")
    logo_url = f"{base_url}/assets/total-solutions-logo.svg"
    escaped_name = html.escape((full_name or "Hola").strip())
    escaped_url = html.escape(verification_url, quote=True)
    escaped_logo_url = html.escape(logo_url, quote=True)
    return f"""<!doctype html>
<html lang="es-MX">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Confirma tu correo - Total Solutions</title>
  </head>
  <body style="margin:0;background:#e8f9f1;font-family:Inter,Arial,Helvetica,sans-serif;color:#1f2937;">
    <div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;">
      Confirma tu correo para continuar con tu acceso a Total Solutions.
    </div>
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#e8f9f1;margin:0;padding:32px 12px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px;background:#07111e;border-radius:24px;overflow:hidden;box-shadow:0 24px 70px rgba(15,23,42,.24);">
            <tr>
              <td style="padding:34px 34px 20px;background:linear-gradient(135deg,#07111e 0%,#0f172a 52%,#0b2a22 100%);">
                <img src="{escaped_logo_url}" alt="Total Solutions" width="260" style="display:block;max-width:260px;width:100%;height:auto;margin:0 0 28px;">
                <div style="display:inline-block;padding:8px 14px;border:1px solid rgba(34,197,94,.45);border-radius:999px;color:#7cff55;background:rgba(34,197,94,.12);font-size:13px;font-weight:800;letter-spacing:.02em;">
                  CORREO SEGURO
                </div>
                <h1 style="margin:22px 0 10px;color:#ffffff;font-size:34px;line-height:1.08;font-weight:900;letter-spacing:0;">
                  Confirma tu correo
                </h1>
                <p style="margin:0;color:#cbd5e1;font-size:17px;line-height:1.55;font-weight:600;">
                  Hola {escaped_name}, completa este paso para continuar con tu solicitud de acceso a Total Solutions.
                </p>
              </td>
            </tr>
            <tr>
              <td style="padding:0 34px 34px;background:#07111e;">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#0b1624;border:1px solid rgba(34,197,94,.24);border-radius:18px;">
                  <tr>
                    <td style="padding:26px;">
                      <p style="margin:0 0 18px;color:#dbeafe;font-size:16px;line-height:1.6;">
                        El enlace vence en <strong style="color:#ffffff;">60 minutos</strong> y solo puede usarse una vez.
                      </p>
                      <a href="{escaped_url}" style="display:block;text-align:center;text-decoration:none;background:linear-gradient(90deg,#4cff32,#22c55e);color:#06110d;font-size:17px;font-weight:900;border-radius:14px;padding:17px 22px;box-shadow:0 12px 30px rgba(34,197,94,.32);">
                        Confirmar correo
                      </a>
                      <p style="margin:22px 0 0;color:#94a3b8;font-size:13px;line-height:1.6;">
                        Si el botón no funciona, copia y pega este enlace en tu navegador:
                      </p>
                      <p style="margin:8px 0 0;word-break:break-all;color:#86efac;font-size:12px;line-height:1.55;">
                        {escaped_url}
                      </p>
                    </td>
                  </tr>
                </table>
                <p style="margin:22px 0 0;color:#94a3b8;font-size:13px;line-height:1.6;text-align:center;">
                  Si no solicitaste este acceso, ignora este mensaje. Tu cuenta no será activada sin aprobación administrativa.
                </p>
              </td>
            </tr>
          </table>
          <p style="margin:18px 0 0;color:#64748b;font-size:12px;line-height:1.5;">
            Total Solutions Cancún | Mantenimiento profesional
          </p>
        </td>
      </tr>
    </table>
  </body>
</html>"""


def send_verification_email_with_resend_api(*, to_email: str, full_name: str | None, verification_url: str) -> bool:
    api_key = resend_api_key()
    smtp_from = os.getenv("SMTP_FROM", "").strip()
    if not api_key or not smtp_from:
        logger.warning(
            "Email de verificacao via Resend API ignorado por configuracao incompleta: api_key=%s,smtp_from=%s",
            bool(api_key),
            bool(smtp_from),
        )
        return False

    payload = {
        "from": smtp_from,
        "to": [to_email],
        "subject": "Confirma tu correo - Total Solutions",
        "text": verification_email_text(full_name=full_name, verification_url=verification_url),
        "html": verification_email_html(full_name=full_name, verification_url=verification_url),
    }
    request = urlrequest.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "TotalSolutionsCRM/1.0 (+https://totalsolutionscancun.com)",
        },
        method="POST",
    )
    try:
        with urlrequest.urlopen(request, timeout=15) as response:
            response_body = response.read().decode("utf-8", errors="replace")
            response_id = ""
            if response_body:
                try:
                    response_id = str(json.loads(response_body).get("id", ""))[:80]
                except json.JSONDecodeError:
                    response_id = ""
            delivered = 200 <= response.status < 300
            if delivered:
                logger.info(
                    "Email de verificacao aceito pela Resend API: status=%s,response_id=%s,user_email_hash=%s",
                    response.status,
                    response_id or "sem-id",
                    hash_value(to_email),
                )
            else:
                logger.warning(
                    "Resend API recusou email de verificacao: status=%s,response_id=%s,user_email_hash=%s",
                    response.status,
                    response_id or "sem-id",
                    hash_value(to_email),
                )
            return delivered
    except urlerror.HTTPError as exc:
        error_status = getattr(exc, "code", "unknown")
        logger.warning(
            "Falha HTTP ao enviar email de verificacao via Resend API: status=%s,user_email_hash=%s",
            error_status,
            hash_value(to_email),
        )
        return False
    except (urlerror.URLError, TimeoutError) as exc:
        logger.warning(
            "Falha ao enviar email de verificacao via Resend API para user_email_hash=%s: %s",
            hash_value(to_email),
            exc.__class__.__name__,
        )
        return False


def send_verification_email(*, to_email: str, full_name: str | None, verification_url: str) -> bool:
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_from = os.getenv("SMTP_FROM", "").strip()
    missing = email_delivery_missing_variables()
    if missing:
        logger.warning("Email de verificacao pendente por configuracao SMTP incompleta: %s", ",".join(sorted(set(missing))))
        return False

    if resend_api_key():
        return send_verification_email_with_resend_api(
            to_email=to_email,
            full_name=full_name,
            verification_url=verification_url,
        )

    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_username = os.getenv("SMTP_USERNAME", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
    use_tls = os.getenv("SMTP_USE_TLS", "true").strip().lower() == "true"
    use_ssl = os.getenv("SMTP_USE_SSL", "false").strip().lower() == "true"

    message = EmailMessage()
    message["From"] = smtp_from
    message["To"] = to_email
    message["Subject"] = "Confirma tu correo - Total Solutions"
    message.set_content(verification_email_text(full_name=full_name, verification_url=verification_url))
    message.add_alternative(verification_email_html(full_name=full_name, verification_url=verification_url), subtype="html")

    try:
        smtp_factory = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        with smtp_factory(smtp_host, smtp_port, timeout=15) as smtp:
            if use_tls:
                smtp.starttls()
            smtp.login(smtp_username, smtp_password)
            smtp.send_message(message)
        return True
    except Exception as exc:  # noqa: BLE001 - cadastro nao deve ser revertido por falha SMTP.
        logger.warning("Falha ao enviar email de verificacao para user_email_hash=%s: %s", hash_value(to_email), exc.__class__.__name__)
        return False


def create_email_verification_link(user: User) -> str | None:
    raw_token = secrets.token_urlsafe(48)
    verification_url = email_verification_url(raw_token)
    if not verification_url:
        return None

    user.email_verification_token = None
    user.email_verification_token_hash = hash_value(raw_token)
    user.email_verification_expires_at = now_utc() + timedelta(minutes=EMAIL_VERIFICATION_TTL_MINUTES)
    user.email_verification_sent_at = now_utc()
    user.email_verification_used_at = None
    return verification_url


def issue_email_verification(db: Session, user: User) -> bool:
    verification_url = create_email_verification_link(user)
    if not verification_url:
        return False
    return send_verification_email(
        to_email=user.email,
        full_name=user.full_name,
        verification_url=verification_url,
    )


def generic_register_response(
    email: str,
    *,
    message: str = PUBLIC_REGISTER_MESSAGE,
    email_delivery_status: str = "accepted",
    resend_after_seconds: int = EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS,
) -> RegisterResponse:
    return RegisterResponse(
        message=message,
        masked_email=mask_email(email),
        resend_after_seconds=resend_after_seconds,
        email_delivery_status=email_delivery_status,
    )


def public_status_label(status: str | None) -> str:
    value = (status or "").upper()
    if value in {"PENDING_EMAIL", "PENDING_APPROVAL", "PENDING_ADMIN", "PENDING"}:
        return "PENDING"
    return value or "ACTIVE"


def create_reactivation_request_for_user(
    db: Session,
    *,
    user: User,
    email: str,
    reason: str,
) -> UserReactivationRequest:
    clean_reason = reason.strip()
    if len(clean_reason) < 5:
        raise HTTPException(status_code=400, detail="Informe uma justificativa com pelo menos 5 caracteres")

    existing = (
        db.query(UserReactivationRequest)
        .filter(UserReactivationRequest.user_id == user.id, UserReactivationRequest.status == "PENDING")
        .first()
    )
    if existing:
        return existing

    request = UserReactivationRequest(
        organization_id=user.organization_id,
        user_id=user.id,
        email=email,
        requested_name=user.full_name,
        current_status=public_status_label(user.status),
        reason=clean_reason[:2000],
    )
    db.add(request)
    db.flush()

    roots = db.query(User).filter(User.role == "ROOT", User.status == "ACTIVE", User.is_active.is_(True)).all()
    for root in roots:
        create_notification(
            db,
            recipient=root,
            actor=None,
            type_="user_reactivation_requested",
            title="Solicitud de reactivación",
            message=f"{user.full_name or email} solicita reactivar una cuenta en estado {public_status_label(user.status)}.",
            priority="NORMAL",
            action_url="/?admin=users&tab=reactivation",
            idempotency_key=f"user_reactivation_request:{request.id}:root:{root.id}",
            metadata={"request_id": request.id, "user_id": user.id, "status": public_status_label(user.status)},
        )

    return request


@router.post("/register", response_model=RegisterResponse, status_code=201)
def register(payload: RegisterRequest, request: Request, db: Session = Depends(get_db)):
    if os.getenv("PUBLIC_SIGNUP_ENABLED", "true").strip().lower() == "false":
        raise HTTPException(status_code=403, detail="Registro temporalmente no disponible.")
    email = normalized_email(payload.email)
    organization = get_or_create_default_organization(db)
    apply_public_rate_limits(db, request, email, "register")
    verify_turnstile_or_403(db, request, token=payload.turnstile_token, expected_action="register")
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="A senha deve ter pelo menos 8 caracteres")

    existing = (
        db.query(User)
        .filter(or_(func.lower(User.email) == email, func.lower(User.username) == email))
        .first()
    )
    if existing:
        status = public_status_label(existing.status)
        if status in {"SUSPENDED", "ARCHIVED"}:
            create_reactivation_request_for_user(
                db,
                user=existing,
                email=email,
                reason="Solicitacao criada a partir de novo cadastro com email existente.",
            )
        if existing.status == "PENDING_EMAIL" and not existing.email_verified:
            rate_limit_or_429(db, "email-verification-resend", str(existing.id), limit=3, window_seconds=900)
            now = now_utc()
            sent_at = existing.email_verification_sent_at
            if sent_at and (now - sent_at).total_seconds() < EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS:
                remaining = EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS - int((now - sent_at).total_seconds())
                audit_auth_event(db, request=request, event_type="REGISTER_REQUESTED", outcome="COOLDOWN_EXISTING_PENDING_EMAIL", user=existing)
                db.commit()
                return generic_register_response(
                    email,
                    email_delivery_status="cooldown",
                    resend_after_seconds=max(remaining, 1),
                )
            email_sent = issue_email_verification(db, existing)
            audit_auth_event(
                db,
                request=request,
                event_type="REGISTER_REQUESTED",
                outcome="RESENT_EXISTING_PENDING_EMAIL" if email_sent else "PENDING_CONFIG_EXISTING_PENDING_EMAIL",
                user=existing,
                detail={"email_sent": email_sent, "missing_email_variables": email_delivery_missing_variables()},
            )
            db.commit()
            if not email_sent:
                logger.warning("Cadastro existente pendente sem reenvio para user_id=%s; configuracao SMTP/PUBLIC_BASE_URL incompleta ou indisponivel.", existing.id)
            return generic_register_response(
                email,
                message=PUBLIC_REGISTER_MESSAGE if email_sent else PUBLIC_EMAIL_DELIVERY_UNAVAILABLE_MESSAGE,
                email_delivery_status="accepted" if email_sent else "unavailable",
            )
        logger.info(
            "Cadastro existente aceito sem reenvio: user_id=%s,status=%s,email_verified=%s",
            existing.id,
            public_status_label(existing.status),
            bool(existing.email_verified),
        )
        audit_auth_event(db, request=request, event_type="REGISTER_REQUESTED", outcome="ACCEPTED_EXISTING", user=existing)
        db.commit()
        return generic_register_response(email)

    user = User(
        organization_id=organization.id,
        username=email,
        email=email,
        full_name=payload.full_name.strip(),
        company=(payload.company or "").strip() or None,
        telefone=(payload.phone or "").strip() or None,
        password_hash=hash_password(payload.password),
        role="BROKER",
        plan=(payload.plan or "STARTER").upper(),
        email_verified=False,
        email_verification_token=None,
        status="PENDING_EMAIL",
        is_active=False,
        registered_at=datetime.utcnow(),
    )
    db.add(user)
    db.flush()
    email_sent = issue_email_verification(db, user)
    audit_auth_event(
        db,
        request=request,
        event_type="REGISTER_REQUESTED",
        outcome="PENDING_EMAIL",
        user=user,
        detail={"email_sent": email_sent, "missing_email_variables": email_delivery_missing_variables()},
    )
    db.commit()
    if not email_sent:
        logger.warning("Email de verificacao nao enviado para user_id=%s; configuracao SMTP/PUBLIC_BASE_URL incompleta ou indisponivel.", user.id)
    return generic_register_response(
        email,
        message=PUBLIC_REGISTER_MESSAGE if email_sent else PUBLIC_EMAIL_DELIVERY_UNAVAILABLE_MESSAGE,
        email_delivery_status="accepted" if email_sent else "unavailable",
    )


@router.post("/resend-verification", response_model=RegisterResponse)
def resend_verification(payload: EmailVerificationResendRequest, request: Request, db: Session = Depends(get_db)):
    email = normalized_email(payload.email)
    logger.info("Solicitacao de reenvio de verificacao recebida: user_email_hash=%s", hash_value(email))
    apply_public_rate_limits(db, request, email, "email_verification_resend")
    verify_turnstile_or_403(db, request, token=payload.turnstile_token, expected_action="email_verification_resend")
    user = (
        db.query(User)
        .filter(or_(func.lower(User.email) == email, func.lower(User.username) == email))
        .first()
    )
    if not user or user.email_verified or user.status != "PENDING_EMAIL":
        logger.info(
            "Reenvio de verificacao aceito sem envio: user_found=%s,status=%s,email_verified=%s,user_email_hash=%s",
            bool(user),
            public_status_label(user.status) if user else "NONE",
            bool(user.email_verified) if user else False,
            hash_value(email),
        )
        audit_auth_event(db, request=request, event_type="EMAIL_VERIFICATION_RESEND", outcome="ACCEPTED")
        db.commit()
        return generic_register_response(email)

    rate_limit_or_429(db, "email-verification-resend", str(user.id), limit=3, window_seconds=900)
    now = now_utc()
    sent_at = user.email_verification_sent_at
    if sent_at and (now - sent_at).total_seconds() < EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS:
        remaining = EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS - int((now - sent_at).total_seconds())
        audit_auth_event(db, request=request, event_type="EMAIL_VERIFICATION_RESEND", outcome="COOLDOWN", user=user)
        db.commit()
        return generic_register_response(
            email,
            email_delivery_status="cooldown",
            resend_after_seconds=max(remaining, 1),
        )

    email_sent = issue_email_verification(db, user)
    audit_auth_event(
        db,
        request=request,
        event_type="EMAIL_VERIFICATION_RESEND",
        outcome="SENT" if email_sent else "PENDING_CONFIG",
        user=user,
        detail={"email_sent": email_sent, "missing_email_variables": email_delivery_missing_variables()},
    )
    db.commit()
    if not email_sent:
        logger.warning("Reenvio de verificacao nao enviado para user_id=%s; configuracao SMTP/PUBLIC_BASE_URL incompleta ou indisponivel.", user.id)
    return generic_register_response(
        email,
        message=PUBLIC_REGISTER_MESSAGE if email_sent else PUBLIC_EMAIL_DELIVERY_UNAVAILABLE_MESSAGE,
        email_delivery_status="accepted" if email_sent else "unavailable",
    )


@router.post("/change-verification-email", response_model=RegisterResponse)
def change_verification_email(payload: EmailVerificationChangeRequest, request: Request, db: Session = Depends(get_db)):
    old_email = normalized_email(payload.old_email)
    new_email = normalized_email(payload.new_email)
    apply_public_rate_limits(db, request, old_email, "email_verification_change")
    apply_public_rate_limits(db, request, new_email, "email_verification_change")
    verify_turnstile_or_403(db, request, token=payload.turnstile_token, expected_action="email_verification_change")

    user = (
        db.query(User)
        .filter(or_(func.lower(User.email) == old_email, func.lower(User.username) == old_email))
        .first()
    )
    if not user or user.email_verified or user.status != "PENDING_EMAIL":
        audit_auth_event(db, request=request, event_type="EMAIL_VERIFICATION_EMAIL_CHANGED", outcome="ACCEPTED")
        db.commit()
        return generic_register_response(new_email)

    existing_new_email = (
        db.query(User)
        .filter(or_(func.lower(User.email) == new_email, func.lower(User.username) == new_email), User.id != user.id)
        .first()
    )
    if existing_new_email:
        audit_auth_event(
            db,
            request=request,
            event_type="EMAIL_VERIFICATION_EMAIL_CHANGED",
            outcome="ACCEPTED_EXISTING",
            user=user,
        )
        db.commit()
        return generic_register_response(new_email)

    user.email = new_email
    user.username = new_email
    user.email_verified = False
    user.email_verification_token = None
    email_sent = issue_email_verification(db, user)
    audit_auth_event(
        db,
        request=request,
        event_type="EMAIL_VERIFICATION_EMAIL_CHANGED",
        outcome="SENT" if email_sent else "PENDING_CONFIG",
        user=user,
        detail={"email_sent": email_sent, "missing_email_variables": email_delivery_missing_variables()},
    )
    db.commit()
    if not email_sent:
        logger.warning("Alteracao de email pendente para user_id=%s; configuracao SMTP/PUBLIC_BASE_URL incompleta ou indisponivel.", user.id)
    return generic_register_response(
        new_email,
        message=PUBLIC_REGISTER_MESSAGE if email_sent else PUBLIC_EMAIL_DELIVERY_UNAVAILABLE_MESSAGE,
        email_delivery_status="accepted" if email_sent else "unavailable",
    )


@router.post("/reactivation-request", response_model=ReactivationRequestResponse)
def request_reactivation(payload: ReactivationRequestCreate, request: Request, db: Session = Depends(get_db)):
    email = normalized_email(payload.email)
    apply_public_rate_limits(db, request, email, "reactivation")
    verify_turnstile_or_403(db, request, token=payload.turnstile_token, expected_action="reactivation")
    user = (
        db.query(User)
        .filter(or_(func.lower(User.email) == email, func.lower(User.username) == email))
        .first()
    )
    if not user:
        audit_auth_event(db, request=request, event_type="REACTIVATION_REQUEST", outcome="UNKNOWN")
        db.commit()
        return ReactivationRequestResponse(message="Se a conta existir e puder ser reativada, o administrador recebera a solicitacao.")

    status = public_status_label(user.status)
    if status == "ACTIVE":
        raise HTTPException(status_code=409, detail="Conta ativa. Use a recuperacao de acesso ou fale com o administrador.")
    if status == "PENDING":
        raise HTTPException(status_code=409, detail="Cadastro ja esta aguardando analise.")
    if status not in {"SUSPENDED", "ARCHIVED"}:
        return ReactivationRequestResponse(message="Se a conta existir e puder ser reativada, o administrador recebera a solicitacao.")

    request_row = create_reactivation_request_for_user(db, user=user, email=email, reason=payload.reason)
    audit_auth_event(db, request=request, event_type="REACTIVATION_REQUEST", outcome="CREATED", user=user)
    db.commit()
    return ReactivationRequestResponse(
        message="Solicitud de reactivación registrada para aprobación.",
        request_id=request_row.id,
    )


def verification_html_response(content: str, *, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(content, status_code=status_code, headers=EMAIL_VERIFICATION_SECURITY_HEADERS)


@router.get("/verify-email")
def verify_email(token: str, db: Session = Depends(get_db)):
    token_hash = hash_value(token)
    user = (
        db.query(User)
        .filter(User.email_verification_token_hash == token_hash, User.email_verification_used_at.is_(None))
        .first()
    )
    now = now_utc()
    if not user or not user.email_verification_expires_at or user.email_verification_expires_at < now:
        return verification_html_response(
            "<h1>El enlace no es válido o ha expirado.</h1><p>Enviar un nuevo enlace desde la pantalla de acceso.</p>",
            status_code=400,
        )

    updated = (
        db.query(User)
        .filter(
            User.id == user.id,
            User.email_verification_token_hash == token_hash,
            User.email_verification_used_at.is_(None),
            User.email_verification_expires_at >= now,
        )
        .update(
            {
                User.email_verified: True,
                User.email_verification_token: None,
                User.email_verification_token_hash: None,
                User.email_verification_used_at: now,
                User.status: "PENDING_ADMIN" if user.status == "PENDING_EMAIL" else user.status,
            },
            synchronize_session=False,
        )
    )
    if updated != 1:
        db.rollback()
        return verification_html_response(
            "<h1>El enlace no es válido o ha expirado.</h1><p>Enviar un nuevo enlace desde la pantalla de acceso.</p>",
            status_code=400,
        )
    db.commit()
    response = RedirectResponse(url=EMAIL_CONFIRMED_REDIRECT_PATH, status_code=303)
    for key, value in EMAIL_VERIFICATION_SECURITY_HEADERS.items():
        response.headers[key] = value
    return response


def issue_authenticated_response(db: Session, request: Request | None, response: Response | None, user: User, *, event_type: str) -> AuthResponse:
    blocked_reason = user_access_block_reason(db, user)
    if blocked_reason:
        audit_auth_event(
            db,
            request=safe_request(request),
            event_type=event_type,
            outcome="BLOCKED",
            user=user,
            detail={"status": user.status},
        )
        db.commit()
        raise HTTPException(status_code=403, detail=blocked_reason)
    user.last_seen_at = datetime.utcnow()
    if not user.status:
        user.status = "ACTIVE"
    create_user_session(db, safe_request(request), safe_response(response), user)
    audit_auth_event(db, request=safe_request(request), event_type=event_type, outcome="SUCCESS", user=user)
    db.commit()
    db.refresh(user)
    return AuthResponse(access_token=create_access_token(user), user=UserResponse.model_validate(user))


def auth_status_gate(db: Session, user: User) -> str | None:
    return user_access_block_reason(db, user)


def mfa_gate_response(db: Session, request: Request | None, user: User, event_type: str) -> AuthResponse | None:
    blocked_reason = user_access_block_reason(db, user)
    if blocked_reason:
        audit_auth_event(db, request=safe_request(request), event_type=event_type, outcome="BLOCKED", user=user, detail={"status": user.status})
        db.commit()
        raise HTTPException(status_code=403, detail=blocked_reason)
    if not mfa_required_for_user(user) and not user.mfa_enabled:
        return None
    if not user.mfa_enabled:
        audit_auth_event(db, request=safe_request(request), event_type=event_type, outcome="MFA_SETUP_REQUIRED", user=user)
        db.commit()
        return AuthResponse(
            mfa_required=True,
            mfa_setup_required=True,
            mfa_challenge_token=create_mfa_challenge_token(user),
            message="Configure MFA para concluir o acesso.",
        )
    audit_auth_event(db, request=safe_request(request), event_type=event_type, outcome="MFA_REQUIRED", user=user)
    db.commit()
    return AuthResponse(
        mfa_required=True,
        mfa_challenge_token=create_mfa_challenge_token(user),
        message="Informe o codigo do autenticador.",
    )


@router.post("/login", response_model=AuthResponse)
def login(
    payload: AuthLoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    identifier = payload.email.strip().lower()
    request = safe_request(request)
    response = safe_response(response)
    apply_public_rate_limits(db, request, identifier, "login")
    verify_turnstile_or_403(db, request, token=payload.turnstile_token, expected_action="login")
    user = (
        db.query(User)
        .filter(or_(func.lower(User.email) == identifier, func.lower(User.username) == identifier))
        .first()
    )
    if not user or not verify_password(payload.password, user.password_hash):
        audit_auth_event(db, request=request, event_type="PASSWORD_LOGIN", outcome="DENIED", user=user)
        db.commit()
        raise HTTPException(status_code=401, detail="Usuario o contraseña inválidos")

    blocked_reason = auth_status_gate(db, user)
    if blocked_reason:
        audit_auth_event(db, request=request, event_type="PASSWORD_LOGIN", outcome="BLOCKED", user=user, detail={"status": user.status})
        db.commit()
        raise HTTPException(status_code=403, detail=blocked_reason)

    if password_needs_upgrade(user.password_hash):
        user.password_hash = hash_password(payload.password)

    clear_rate_limit(db, "id:login", identifier)

    mfa_response = mfa_gate_response(db, request, user, "PASSWORD_LOGIN")
    if mfa_response:
        return mfa_response

    return issue_authenticated_response(db, request, response, user, event_type="PASSWORD_LOGIN")


@router.post("/mfa/verify", response_model=AuthResponse)
def verify_mfa(payload: MfaVerifyRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    user_id = verify_mfa_challenge_token(payload.challenge_token)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="Desafio MFA invalido")
    blocked_reason = auth_status_gate(db, user)
    if blocked_reason:
        audit_auth_event(db, request=request, event_type="MFA_VERIFY", outcome="BLOCKED", user=user, detail={"status": user.status})
        db.commit()
        raise HTTPException(status_code=403, detail=blocked_reason)
    rate_limit_or_429(db, "mfa", str(user.id), limit=6, window_seconds=300)
    if not (verify_totp_code(user, payload.code) or consume_recovery_code(db, user, payload.code)):
        audit_auth_event(db, request=request, event_type="MFA_VERIFY", outcome="DENIED", user=user)
        db.commit()
        raise HTTPException(status_code=401, detail="Codigo MFA invalido")
    return issue_authenticated_response(db, request, response, user, event_type="MFA_VERIFY")


@router.post("/mfa/setup/start", response_model=MfaSetupStartResponse)
def start_mfa_setup(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.mfa_secret_encrypted and not user.mfa_enabled:
        secret = decrypt_secret(user.mfa_secret_encrypted)
    else:
        secret = generate_totp_secret()
        user.mfa_secret_encrypted = encrypt_secret(secret)
        user.mfa_enabled = False
        user.mfa_confirmed_at = None
        db.commit()
    return MfaSetupStartResponse(secret=secret, otpauth_url=otpauth_url(user, secret))


@router.post("/mfa/setup/start-challenge", response_model=MfaSetupStartResponse)
def start_mfa_setup_from_challenge(payload: MfaSetupChallengeStartRequest, db: Session = Depends(get_db)):
    user_id = verify_mfa_challenge_token(payload.challenge_token)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="Desafio MFA invalido")
    blocked_reason = auth_status_gate(db, user)
    if blocked_reason:
        audit_auth_event(db, request=safe_request(None), event_type="MFA_SETUP_START", outcome="BLOCKED", user=user, detail={"status": user.status})
        db.commit()
        raise HTTPException(status_code=403, detail=blocked_reason)
    if user.mfa_secret_encrypted and not user.mfa_enabled and not payload.reset_secret:
        secret = decrypt_secret(user.mfa_secret_encrypted)
    else:
        secret = generate_totp_secret()
        user.mfa_secret_encrypted = encrypt_secret(secret)
        user.mfa_enabled = False
        user.mfa_confirmed_at = None
        user.mfa_last_counter = None
        db.commit()
    return MfaSetupStartResponse(secret=secret, otpauth_url=otpauth_url(user, secret))


@router.post("/mfa/setup/confirm", response_model=MfaSetupConfirmResponse)
def confirm_mfa_setup(payload: MfaSetupConfirmRequest, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not verify_totp_code(user, payload.code):
        audit_auth_event(db, request=request, event_type="MFA_ENABLED", outcome="DENIED", user=user)
        db.commit()
        raise HTTPException(status_code=401, detail="Codigo MFA invalido")
    user.mfa_enabled = True
    user.mfa_confirmed_at = datetime.utcnow()
    codes = generate_recovery_codes(db, user)
    audit_auth_event(db, request=request, event_type="MFA_ENABLED", outcome="SUCCESS", user=user)
    db.commit()
    return MfaSetupConfirmResponse(recovery_codes=codes)


@router.post("/mfa/setup/confirm-challenge", response_model=AuthResponse)
def confirm_mfa_setup_from_challenge(
    payload: MfaSetupChallengeConfirmRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    user_id = verify_mfa_challenge_token(payload.challenge_token)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="Desafio MFA invalido")
    blocked_reason = auth_status_gate(db, user)
    if blocked_reason:
        audit_auth_event(db, request=request, event_type="MFA_ENABLED", outcome="BLOCKED", user=user, detail={"status": user.status})
        db.commit()
        raise HTTPException(status_code=403, detail=blocked_reason)
    if not verify_totp_code(user, payload.code):
        audit_auth_event(db, request=request, event_type="MFA_ENABLED", outcome="DENIED", user=user)
        db.commit()
        raise HTTPException(status_code=401, detail="Codigo MFA invalido")
    user.mfa_enabled = True
    user.mfa_confirmed_at = datetime.utcnow()
    generate_recovery_codes(db, user)
    audit_auth_event(db, request=request, event_type="MFA_ENABLED", outcome="SUCCESS", user=user)
    return issue_authenticated_response(db, request, response, user, event_type="MFA_SETUP_LOGIN")


@router.post("/password-recovery/request", response_model=ReactivationRequestResponse)
def request_password_recovery(payload: PasswordRecoveryRequest, request: Request, db: Session = Depends(get_db)):
    email = normalized_email(payload.email)
    apply_public_rate_limits(db, request, email, "password_recovery")
    verify_turnstile_or_403(db, request, token=payload.turnstile_token, expected_action="password_recovery")
    user = db.query(User).filter(or_(func.lower(User.email) == email, func.lower(User.username) == email)).first()
    if user and not user_access_block_reason(db, user):
        raw_token = secrets.token_urlsafe(48)
        db.add(
            PasswordResetToken(
                organization_id=user.organization_id,
                user_id=user.id,
                token_hash=hash_value(raw_token),
                expires_at=datetime.utcnow() + timedelta(minutes=30),
                requested_ip_hash=hash_value(request_ip(request)),
                requested_user_agent=request.headers.get("user-agent", "")[:1000],
            )
        )
        audit_auth_event(db, request=request, event_type="PASSWORD_RECOVERY_REQUESTED", outcome="CREATED", user=user)
        logger.info("Password reset token generated for user_id=%s; token is intentionally not logged.", user.id)
    else:
        audit_auth_event(db, request=request, event_type="PASSWORD_RECOVERY_REQUESTED", outcome="ACCEPTED")
    db.commit()
    return ReactivationRequestResponse(message=PUBLIC_AUTH_MESSAGE)


@router.post("/password-recovery/reset", response_model=ReactivationRequestResponse)
def reset_password(payload: PasswordResetRequest, request: Request, db: Session = Depends(get_db)):
    verify_turnstile_or_403(db, request, token=payload.turnstile_token, expected_action="password_reset")
    if len(payload.password) < 10:
        raise HTTPException(status_code=400, detail="A senha deve ter pelo menos 10 caracteres")
    token_hash = hash_value(payload.token)
    row = (
        db.query(PasswordResetToken)
        .filter(PasswordResetToken.token_hash == token_hash, PasswordResetToken.used_at.is_(None))
        .first()
    )
    if not row or row.expires_at < datetime.utcnow():
        audit_auth_event(db, request=request, event_type="PASSWORD_RESET", outcome="DENIED")
        db.commit()
        raise HTTPException(status_code=400, detail="Token invalido ou expirado")
    user = db.query(User).filter(User.id == row.user_id).first()
    if not user:
        raise HTTPException(status_code=400, detail="Token invalido ou expirado")
    user.password_hash = hash_password(payload.password)
    user.session_version = int(user.session_version or 0) + 1
    row.used_at = datetime.utcnow()
    revoke_sessions_for_user(db, user.id, "password_reset")
    audit_auth_event(db, request=request, event_type="PASSWORD_RESET", outcome="SUCCESS", user=user)
    db.commit()
    return ReactivationRequestResponse(message="Senha atualizada com sucesso.")


@router.post("/google", response_model=AuthResponse)
def google_login(payload: GoogleLoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    verify_turnstile_or_403(db, request, token=payload.turnstile_token, expected_action="google_login")
    claims = validate_google_id_token_or_401(payload.id_token)
    provider_subject = claims["sub"]
    provider_email = str(claims.get("email", "")).strip().lower()
    identity = (
        db.query(UserIdentity)
        .filter(UserIdentity.provider == "google", UserIdentity.provider_subject == provider_subject)
        .first()
    )
    if not identity:
        existing_email_user = db.query(User).filter(func.lower(User.email) == provider_email).first()
        if existing_email_user and existing_email_user.status != "ANONYMIZED":
            audit_auth_event(db, request=request, event_type="GOOGLE_LOGIN", outcome="LINK_REQUIRED", user=existing_email_user)
            db.commit()
            raise HTTPException(status_code=409, detail="Conta Google precisa ser vinculada após login seguro na conta atual.")
        user = User(
            organization_id=get_or_create_default_organization(db).id,
            username=provider_email,
            email=provider_email,
            full_name=claims.get("name") or provider_email,
            profile_photo_url=claims.get("picture"),
            password_hash=hash_password(secrets.token_urlsafe(48)),
            role="BROKER",
            email_verified=True,
            status="PENDING_ADMIN",
            is_active=False,
            registered_at=datetime.utcnow(),
        )
        db.add(user)
        db.flush()
        identity = UserIdentity(
            organization_id=user.organization_id,
            user_id=user.id,
            provider="google",
            provider_subject=provider_subject,
            provider_email=provider_email,
            email_verified=True,
            linked_at=datetime.utcnow(),
        )
        db.add(identity)
        audit_auth_event(db, request=request, event_type="GOOGLE_REGISTER", outcome="PENDING", user=user)
        db.commit()
        return AuthResponse(message="Registro recibido y pendiente de aprobación.", user=UserResponse.model_validate(user))

    user = db.query(User).filter(User.id == identity.user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="Identidade Google invalida")
    blocked_reason = auth_status_gate(db, user)
    if blocked_reason:
        audit_auth_event(db, request=request, event_type="GOOGLE_LOGIN", outcome="BLOCKED", user=user, detail={"status": user.status})
        db.commit()
        raise HTTPException(status_code=403, detail=blocked_reason)
    identity.last_login_at = datetime.utcnow()
    mfa_response = mfa_gate_response(db, request, user, "GOOGLE_LOGIN")
    if mfa_response:
        return mfa_response
    return issue_authenticated_response(db, request, response, user, event_type="GOOGLE_LOGIN")


@router.post("/logout")
def logout(response: Response, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    user.last_seen_at = None
    revoke_sessions_for_user(db, user.id, "logout")
    db.commit()
    clear_session_cookies(response)
    return {"offline": True}


@router.get("/me", response_model=UserResponse)
def get_authenticated_user(user: User = Depends(get_current_user)):
    return user
