import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import struct
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
from fastapi import HTTPException, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import hash_password, verify_password
from app.models.auth_security import AuthAuditEvent, AuthRateLimit, MfaRecoveryCode, UserSession
from app.models.user import User

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover
    Fernet = None
    InvalidToken = Exception

try:
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token
except ImportError:  # pragma: no cover
    google_requests = None
    google_id_token = None


AUTH_COOKIE_NAME = "ts_session"
CSRF_COOKIE_NAME = "ts_csrf"
MFA_CHALLENGE_TTL_SECONDS = 300
RATE_LIMIT_CLEANUP_AFTER_SECONDS = 86400


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def hash_value(value: str | None) -> str | None:
    if not value:
        return None
    pepper = os.getenv("AUTH_AUDIT_PEPPER", os.getenv("JWT_SECRET_KEY", "dev-pepper"))
    return hashlib.sha256(f"{pepper}:{value}".encode("utf-8")).hexdigest()


def scoped_hmac_value(purpose: str, value: str | None) -> str | None:
    if not value:
        return None
    pepper = os.getenv("AUTH_AUDIT_PEPPER", os.getenv("JWT_SECRET_KEY", "dev-pepper"))
    message = f"{purpose}:{value}".encode("utf-8")
    return hmac.new(pepper.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _trusted_proxy_networks() -> list[ipaddress._BaseNetwork]:
    configured = os.getenv("TRUSTED_PROXY_CIDRS", "").strip()
    networks: list[ipaddress._BaseNetwork] = []
    for raw_network in configured.split(","):
        raw_network = raw_network.strip()
        if not raw_network:
            continue
        try:
            networks.append(ipaddress.ip_network(raw_network, strict=False))
        except ValueError:
            continue
    return networks


def _is_trusted_proxy_host(host: str) -> bool:
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    return any(ip in network for network in _trusted_proxy_networks())


def _first_valid_forwarded_ip(header_value: str) -> str:
    for candidate in header_value.split(","):
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            continue
        return candidate
    return ""


def request_ip(request: Request | None) -> str:
    if not request:
        return ""
    connection_host = request.client.host if request.client else ""
    trust_proxy_headers = os.getenv("TRUST_PROXY_HEADERS", "false").strip().lower() == "true"
    if trust_proxy_headers and _is_trusted_proxy_host(connection_host):
        cf_ip = _first_valid_forwarded_ip(request.headers.get("cf-connecting-ip", ""))
        if cf_ip:
            return cf_ip
        forwarded_ip = _first_valid_forwarded_ip(request.headers.get("x-forwarded-for", ""))
        if forwarded_ip:
            return forwarded_ip
    return connection_host


def request_user_agent(request: Request | None) -> str:
    return (request.headers.get("user-agent", "") if request else "")[:1000]


def audit_auth_event(
    db: Session,
    *,
    request: Request | None,
    event_type: str,
    outcome: str,
    user: User | None = None,
    actor: User | None = None,
    detail: dict | str | None = None,
    correlation_id: str | None = None,
) -> AuthAuditEvent:
    if isinstance(detail, dict):
        safe_detail = json.dumps(detail, ensure_ascii=False)
    else:
        safe_detail = detail
    event = AuthAuditEvent(
        organization_id=(user.organization_id if user else None) or (actor.organization_id if actor else None),
        user_id=user.id if user else None,
        actor_user_id=actor.id if actor else None,
        event_type=event_type[:80],
        outcome=outcome[:40],
        ip_hash=hash_value(request_ip(request)),
        user_agent=request_user_agent(request),
        correlation_id=correlation_id or uuid4().hex,
        detail=(safe_detail or "")[:2000] or None,
    )
    db.add(event)
    return event


def _rate_limit_message() -> str:
    return "Muitas tentativas. Aguarde alguns minutos e tente novamente."


def _rate_limit_key_hash(scope: str, key: str) -> str:
    normalized_key = (key or "anonymous").strip().lower()
    return scoped_hmac_value(f"auth-rate-limit:{scope}", normalized_key) or "unknown"


def cleanup_expired_rate_limits(db: Session) -> int:
    cutoff = now_utc() - timedelta(seconds=RATE_LIMIT_CLEANUP_AFTER_SECONDS)
    deleted = db.query(AuthRateLimit).filter(AuthRateLimit.updated_at < cutoff).delete(synchronize_session=False)
    return int(deleted or 0)


def clear_rate_limit(db: Session, scope: str, key: str) -> int:
    key_hash = _rate_limit_key_hash(scope, key)
    deleted = (
        db.query(AuthRateLimit)
        .filter(AuthRateLimit.scope == scope, AuthRateLimit.key_hash == key_hash)
        .delete(synchronize_session=False)
    )
    return int(deleted or 0)


def rate_limit_or_429(
    db: Session,
    scope: str,
    key: str,
    *,
    limit: int,
    window_seconds: int,
    progressive: bool = True,
) -> None:
    now = now_utc()
    key_hash = _rate_limit_key_hash(scope, key)

    if secrets.randbelow(100) == 0:
        cleanup_expired_rate_limits(db)

    row = (
        db.query(AuthRateLimit)
        .filter(AuthRateLimit.scope == scope, AuthRateLimit.key_hash == key_hash)
        .with_for_update()
        .first()
    )
    if row is None:
        row = AuthRateLimit(scope=scope, key_hash=key_hash, window_start=now, attempts=0, updated_at=now)
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            row = (
                db.query(AuthRateLimit)
                .filter(AuthRateLimit.scope == scope, AuthRateLimit.key_hash == key_hash)
                .with_for_update()
                .one()
            )

    if row.blocked_until and row.blocked_until > now:
        retry_after = max(1, int((row.blocked_until - now).total_seconds()) + 1)
        raise HTTPException(status_code=429, detail=_rate_limit_message(), headers={"Retry-After": str(retry_after)})

    if (now - row.window_start).total_seconds() >= window_seconds:
        row.window_start = now
        row.attempts = 0
        row.blocked_until = None

    next_attempts = int(row.attempts or 0) + 1
    row.attempts = next_attempts
    row.updated_at = now

    if next_attempts > limit:
        retry_after = max(1, int(window_seconds - (now - row.window_start).total_seconds()) + 1)
        if progressive:
            retry_after = min(window_seconds * 2, retry_after + (next_attempts - limit) * 5)
        row.blocked_until = now + timedelta(seconds=retry_after)
        raise HTTPException(status_code=429, detail=_rate_limit_message(), headers={"Retry-After": str(retry_after)})


def apply_public_rate_limits(db: Session, request: Request, identity: str, action: str) -> None:
    ip_value = request_ip(request) or "unknown"
    identity_value = identity.strip().lower() or "anonymous"
    rate_limit_or_429(db, f"ip:{action}", ip_value, limit=20, window_seconds=300)
    rate_limit_or_429(db, f"id:{action}", identity_value, limit=8, window_seconds=900)


def turnstile_configured() -> bool:
    return bool(os.getenv("TURNSTILE_SECRET_KEY", "").strip())


def public_turnstile_site_key() -> str:
    return os.getenv("TURNSTILE_SITE_KEY", "").strip()


def verify_turnstile_or_403(
    db: Session,
    request: Request,
    *,
    token: str | None,
    expected_action: str,
) -> None:
    secret = os.getenv("TURNSTILE_SECRET_KEY", "").strip()
    if not secret:
        if os.getenv("AUTH_SECURITY_TEST_MODE", "").lower() == "true" or os.getenv("ENVIRONMENT", "").lower() != "production":
            return
        audit_auth_event(db, request=request, event_type="TURNSTILE_CONFIG_MISSING", outcome="DENIED")
        db.commit()
        raise HTTPException(status_code=503, detail="Validacao de seguranca indisponivel")
    if not token:
        audit_auth_event(db, request=request, event_type="TURNSTILE_MISSING", outcome="DENIED", detail={"action": expected_action})
        db.commit()
        raise HTTPException(status_code=403, detail="Validacao humana obrigatoria")
    if os.getenv("AUTH_SECURITY_TEST_MODE", "").lower() == "true":
        if token != f"test:{expected_action}":
            audit_auth_event(db, request=request, event_type="TURNSTILE_TEST_DENIED", outcome="DENIED", detail={"action": expected_action})
            db.commit()
            raise HTTPException(status_code=403, detail="Validacao humana invalida")
        return

    try:
        with httpx.Client(timeout=8) as client:
            response = client.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={"secret": secret, "response": token, "remoteip": request_ip(request)},
            )
        data = response.json()
    except Exception:
        audit_auth_event(db, request=request, event_type="TURNSTILE_UNAVAILABLE", outcome="DENIED", detail={"action": expected_action})
        db.commit()
        raise HTTPException(status_code=503, detail="Validacao de seguranca indisponivel")

    expected_hostname = os.getenv("TURNSTILE_EXPECTED_HOSTNAME", "").strip()
    hostname_ok = not expected_hostname or data.get("hostname") == expected_hostname
    action_ok = data.get("action") == expected_action
    if not data.get("success") or not hostname_ok or not action_ok:
        audit_auth_event(
            db,
            request=request,
            event_type="TURNSTILE_DENIED",
            outcome="DENIED",
            detail={"action": expected_action, "hostname_ok": hostname_ok, "action_ok": action_ok},
        )
        db.commit()
        raise HTTPException(status_code=403, detail="Validacao humana invalida")


def session_ttl_hours() -> int:
    return int(os.getenv("SESSION_EXPIRE_HOURS", os.getenv("JWT_EXPIRE_HOURS", "12")))


def create_user_session(db: Session, request: Request, response: Response, user: User) -> UserSession:
    session_id = secrets.token_urlsafe(48)
    csrf_token = secrets.token_urlsafe(32)
    expires = now_utc() + timedelta(hours=session_ttl_hours())
    row = UserSession(
        organization_id=user.organization_id,
        user_id=user.id,
        session_id_hash=hash_value(session_id),
        csrf_token_hash=hash_value(csrf_token),
        user_agent=request_user_agent(request),
        ip_hash=hash_value(request_ip(request)),
        expires_at=expires,
    )
    db.add(row)
    secure = os.getenv("COOKIE_SECURE", "true").lower() != "false"
    response.set_cookie(
        AUTH_COOKIE_NAME,
        session_id,
        httponly=True,
        secure=secure,
        samesite=os.getenv("COOKIE_SAMESITE", "lax").lower(),
        max_age=session_ttl_hours() * 3600,
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        httponly=False,
        secure=secure,
        samesite=os.getenv("COOKIE_SAMESITE", "lax").lower(),
        max_age=session_ttl_hours() * 3600,
        path="/",
    )
    return row


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")


def validate_session_cookie(db: Session, request: Request) -> User | None:
    session_id = request.cookies.get(AUTH_COOKIE_NAME)
    if not session_id:
        return None
    row = db.query(UserSession).filter(UserSession.session_id_hash == hash_value(session_id)).first()
    if not row or row.revoked_at or row.expires_at <= now_utc():
        return None
    unsafe = request.method.upper() not in {"GET", "HEAD", "OPTIONS"}
    if unsafe:
        csrf_header = request.headers.get("x-csrf-token", "")
        if not csrf_header or hash_value(csrf_header) != row.csrf_token_hash:
            raise HTTPException(status_code=403, detail="CSRF invalido")
    user = db.query(User).filter(User.id == row.user_id).first()
    if not user:
        return None
    row.last_seen_at = now_utc()
    return user


def revoke_sessions_for_user(db: Session, user_id: int, reason: str) -> int:
    now = now_utc()
    rows = (
        db.query(UserSession)
        .filter(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .all()
    )
    for row in rows:
        row.revoked_at = now
        row.revoke_reason = reason[:255]
    return len(rows)


def encryption_key() -> bytes:
    raw = os.getenv("TOTP_ENCRYPTION_KEY", "").strip()
    if not raw:
        if os.getenv("ENVIRONMENT", "").lower() == "production":
            raise RuntimeError("TOTP_ENCRYPTION_KEY ausente")
        raw = base64.urlsafe_b64encode(hashlib.sha256(b"dev-totp-key").digest()).decode("ascii")
    try:
        return raw.encode("ascii")
    except UnicodeEncodeError as exc:
        raise RuntimeError("TOTP_ENCRYPTION_KEY invalida") from exc


def encrypt_secret(value: str) -> str:
    if Fernet is None:
        raise RuntimeError("cryptography nao instalada")
    return Fernet(encryption_key()).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str) -> str:
    if Fernet is None:
        raise RuntimeError("cryptography nao instalada")
    try:
        return Fernet(encryption_key()).decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise HTTPException(status_code=500, detail="Configuracao MFA invalida") from exc


def generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def hotp(secret: str, counter: int, digits: int = 6) -> str:
    padding = "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode((secret + padding).upper())
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10**digits)).zfill(digits)


def totp_counter(timestamp: int | None = None, step: int = 30) -> int:
    return int((timestamp if timestamp is not None else time.time()) // step)


def verify_totp_code(user: User, code: str, *, window: int = 1) -> bool:
    if not user.mfa_secret_encrypted:
        return False
    clean = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(clean) != 6:
        return False
    secret = decrypt_secret(user.mfa_secret_encrypted)
    current = totp_counter()
    for counter in range(current - window, current + window + 1):
        if user.mfa_last_counter is not None and counter <= user.mfa_last_counter:
            continue
        if hmac.compare_digest(hotp(secret, counter), clean):
            user.mfa_last_counter = counter
            return True
    return False


def mfa_required_for_user(user: User) -> bool:
    if os.getenv("MFA_REQUIRED_FOR_ALL", "false").lower() == "true":
        return True
    return user.role in {"ROOT", "GERENTE"}


def create_mfa_challenge_token(user: User) -> str:
    payload = {
        "sub": str(user.id),
        "purpose": "mfa",
        "exp": int(time.time()) + MFA_CHALLENGE_TTL_SECONDS,
        "nonce": secrets.token_urlsafe(16),
    }
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(os.getenv("JWT_SECRET_KEY", "dev").encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def verify_mfa_challenge_token(token: str) -> int:
    try:
        body, signature = token.split(".", 1)
        expected = hmac.new(os.getenv("JWT_SECRET_KEY", "dev").encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * ((4 - len(body) % 4) % 4)))
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Desafio MFA invalido") from exc
    if payload.get("purpose") != "mfa" or int(payload.get("exp", 0)) < int(time.time()):
        raise HTTPException(status_code=401, detail="Desafio MFA expirado")
    return int(payload["sub"])


def generate_recovery_codes(db: Session, user: User, count: int = 10) -> list[str]:
    db.query(MfaRecoveryCode).filter(MfaRecoveryCode.user_id == user.id, MfaRecoveryCode.used_at.is_(None)).delete()
    codes = []
    for _ in range(count):
        raw = f"TS-{secrets.token_urlsafe(9).replace('-', '').replace('_', '')[:12].upper()}"
        codes.append(raw)
        db.add(MfaRecoveryCode(organization_id=user.organization_id, user_id=user.id, code_hash=hash_value(raw)))
    return codes


def consume_recovery_code(db: Session, user: User, code: str) -> bool:
    row = (
        db.query(MfaRecoveryCode)
        .filter(MfaRecoveryCode.user_id == user.id, MfaRecoveryCode.code_hash == hash_value(code), MfaRecoveryCode.used_at.is_(None))
        .first()
    )
    if not row:
        return False
    row.used_at = now_utc()
    return True


def otpauth_url(user: User, secret: str) -> str:
    issuer = "Total Solutions"
    label = f"{issuer}:{user.email or user.username}"
    from urllib.parse import quote

    return f"otpauth://totp/{quote(label)}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"


def validate_google_id_token_or_401(token: str) -> dict:
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    if not client_id:
        raise HTTPException(status_code=503, detail="Google Sign-In nao configurado")
    if os.getenv("AUTH_SECURITY_TEST_MODE", "").lower() == "true":
        try:
            payload = json.loads(token)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=401, detail="Token Google invalido") from exc
    else:
        if google_id_token is None or google_requests is None:
            raise HTTPException(status_code=503, detail="Biblioteca Google nao instalada")
        try:
            payload = google_id_token.verify_oauth2_token(token, google_requests.Request(), client_id)
        except Exception as exc:
            raise HTTPException(status_code=401, detail="Token Google invalido") from exc
    if payload.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}:
        raise HTTPException(status_code=401, detail="Emissor Google invalido")
    if payload.get("aud") != client_id:
        raise HTTPException(status_code=401, detail="Audience Google invalida")
    if not payload.get("email_verified"):
        raise HTTPException(status_code=401, detail="Email Google nao verificado")
    if not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Identidade Google invalida")
    exp = int(payload.get("exp", 0))
    if exp and exp < int(time.time()):
        raise HTTPException(status_code=401, detail="Token Google expirado")
    return payload
