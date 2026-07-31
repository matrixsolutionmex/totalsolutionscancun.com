import logging
import os
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.auth.jwt_handler import create_access_token, get_current_user, get_db
from app.core.auth_security import (
    audit_auth_event,
    apply_public_rate_limits,
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
    revoke_sessions_for_user,
    turnstile_configured,
    validate_google_id_token_or_401,
    verify_mfa_challenge_token,
    verify_totp_code,
    verify_turnstile_or_403,
    encrypt_secret,
    now_utc,
)
from app.core.security import hash_password, password_needs_upgrade, verify_password
from app.models.auth_security import PasswordResetToken, UserIdentity
from app.models.user import User
from app.models.user_lifecycle import UserReactivationRequest
from app.schemas.auth_schema import (
    AuthLoginRequest,
    AuthResponse,
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


PUBLIC_AUTH_MESSAGE = "Se as informacoes forem validas, voce recebera as proximas instrucoes."


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
    )


def normalized_email(value: str) -> str:
    email = value.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise HTTPException(status_code=400, detail="Email invalido")
    return email


def public_status_label(status: str | None) -> str:
    value = (status or "").upper()
    if value in {"PENDING_EMAIL", "PENDING_APPROVAL", "PENDING"}:
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
    email = normalized_email(payload.email)
    apply_public_rate_limits(request, email, "register")
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
        if status == "ACTIVE":
            raise HTTPException(status_code=409, detail="Conta ja existe. Use recuperacao de acesso ou entre em contato com o administrador.")
        if status == "PENDING":
            raise HTTPException(status_code=409, detail="Sua solicitacao ja esta aguardando analise.")
        if status in {"SUSPENDED", "ARCHIVED"}:
            request_row = create_reactivation_request_for_user(
                db,
                user=existing,
                email=email,
                reason="Solicitacao criada a partir de novo cadastro com email existente.",
            )
            db.commit()
            return RegisterResponse(
                message="Conta existente bloqueada. Solicitação de reativação enviada ao administrador.",
                verification_url=f"reactivation-request:{request_row.id}",
            )
        raise HTTPException(status_code=409, detail="Email ja cadastrado")

    token = secrets.token_urlsafe(32)
    user = User(
        username=email,
        email=email,
        full_name=payload.full_name.strip(),
        company=(payload.company or "").strip() or None,
        telefone=(payload.phone or "").strip() or None,
        password_hash=hash_password(payload.password),
        role="BROKER",
        plan=(payload.plan or "STARTER").upper(),
        email_verified=False,
        email_verification_token=token,
        status="PENDING_EMAIL",
        is_active=False,
        registered_at=datetime.utcnow(),
    )
    db.add(user)
    audit_auth_event(db, request=request, event_type="REGISTER_REQUESTED", outcome="PENDING", user=user)
    db.commit()

    base_url = os.getenv("PUBLIC_BASE_URL") or str(request.base_url).rstrip("/")
    verification_url = f"{base_url}/auth/verify-email?token={token}"
    logger.info("Verificacao de email Total Solutions para %s: %s", email, verification_url)
    return RegisterResponse(
        message="Cadastro recebido. Confirme o email para seguir para aprovacao.",
        verification_url=verification_url,
    )


@router.post("/reactivation-request", response_model=ReactivationRequestResponse)
def request_reactivation(payload: ReactivationRequestCreate, request: Request, db: Session = Depends(get_db)):
    email = normalized_email(payload.email)
    apply_public_rate_limits(request, email, "reactivation")
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


@router.get("/verify-email", response_class=HTMLResponse)
def verify_email(token: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email_verification_token == token).first()
    if not user:
        raise HTTPException(status_code=404, detail="Link de verificacao invalido")

    user.email_verified = True
    user.email_verification_token = None
    user.status = "PENDING_APPROVAL"
    db.commit()
    return HTMLResponse(
        "<h1>Email confirmado</h1><p>Seu cadastro agora aguarda aprovacao do administrador Total Solutions.</p>"
    )


def issue_authenticated_response(db: Session, request: Request | None, response: Response | None, user: User, *, event_type: str) -> AuthResponse:
    user.last_seen_at = datetime.utcnow()
    if not user.status:
        user.status = "ACTIVE"
    create_user_session(db, safe_request(request), safe_response(response), user)
    audit_auth_event(db, request=safe_request(request), event_type=event_type, outcome="SUCCESS", user=user)
    db.commit()
    db.refresh(user)
    return AuthResponse(access_token=create_access_token(user), user=UserResponse.model_validate(user))


def auth_status_gate(user: User) -> str | None:
    if user.status == "PENDING_EMAIL":
        return "Confirme seu email antes de entrar"
    if user.status == "PENDING_APPROVAL" or user.status == "PENDING":
        return "Cadastro recebido e aguardando aprovação"
    if user.status == "SUSPENDED":
        return "Usuario suspenso ou inativo"
    if user.status in {"ARCHIVED", "ANONYMIZED"}:
        return "Usuario nao autorizado"
    if not user.is_active:
        return "Usuario suspenso ou inativo"
    return None


def mfa_gate_response(db: Session, request: Request | None, user: User, event_type: str) -> AuthResponse | None:
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
    apply_public_rate_limits(request, identifier, "login")
    verify_turnstile_or_403(db, request, token=payload.turnstile_token, expected_action="login")
    user = (
        db.query(User)
        .filter(or_(func.lower(User.email) == identifier, func.lower(User.username) == identifier))
        .first()
    )
    if not user or not verify_password(payload.password, user.password_hash):
        audit_auth_event(db, request=request, event_type="PASSWORD_LOGIN", outcome="DENIED", user=user)
        db.commit()
        raise HTTPException(status_code=401, detail="Usuario ou senha invalidos")

    blocked_reason = auth_status_gate(user)
    if blocked_reason:
        audit_auth_event(db, request=request, event_type="PASSWORD_LOGIN", outcome="BLOCKED", user=user, detail={"status": user.status})
        db.commit()
        raise HTTPException(status_code=403, detail=blocked_reason)

    if password_needs_upgrade(user.password_hash):
        user.password_hash = hash_password(payload.password)

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
    rate_limit_or_429(f"mfa:{user.id}", limit=6, window_seconds=300)
    if not (verify_totp_code(user, payload.code) or consume_recovery_code(db, user, payload.code)):
        audit_auth_event(db, request=request, event_type="MFA_VERIFY", outcome="DENIED", user=user)
        db.commit()
        raise HTTPException(status_code=401, detail="Codigo MFA invalido")
    return issue_authenticated_response(db, request, response, user, event_type="MFA_VERIFY")


@router.post("/mfa/setup/start", response_model=MfaSetupStartResponse)
def start_mfa_setup(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
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
    secret = generate_totp_secret()
    user.mfa_secret_encrypted = encrypt_secret(secret)
    user.mfa_enabled = False
    user.mfa_confirmed_at = None
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
    apply_public_rate_limits(request, email, "password_recovery")
    verify_turnstile_or_403(db, request, token=payload.turnstile_token, expected_action="password_recovery")
    user = db.query(User).filter(or_(func.lower(User.email) == email, func.lower(User.username) == email)).first()
    if user and user.status == "ACTIVE":
        raw_token = secrets.token_urlsafe(48)
        db.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=hash_value(raw_token),
                expires_at=datetime.utcnow() + timedelta(minutes=30),
                requested_ip_hash=hash_value(request.client.host if request.client else ""),
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
            username=provider_email,
            email=provider_email,
            full_name=claims.get("name") or provider_email,
            profile_photo_url=claims.get("picture"),
            password_hash=hash_password(secrets.token_urlsafe(48)),
            role="BROKER",
            email_verified=True,
            status="PENDING_APPROVAL",
            is_active=False,
            registered_at=datetime.utcnow(),
        )
        db.add(user)
        db.flush()
        identity = UserIdentity(
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
        return AuthResponse(message="Cadastro recebido e aguardando aprovação", user=UserResponse.model_validate(user))

    user = db.query(User).filter(User.id == identity.user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="Identidade Google invalida")
    blocked_reason = auth_status_gate(user)
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
