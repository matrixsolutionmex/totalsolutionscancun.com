import logging
import os
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.auth.jwt_handler import create_access_token, get_current_user, get_db
from app.core.security import hash_password, password_needs_upgrade, verify_password
from app.models.user import User
from app.schemas.auth_schema import AuthLoginRequest, AuthResponse, RegisterRequest, RegisterResponse
from app.schemas.user_schema import UserResponse


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


def normalized_email(value: str) -> str:
    email = value.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise HTTPException(status_code=400, detail="Email invalido")
    return email


@router.post("/register", response_model=RegisterResponse, status_code=201)
def register(payload: RegisterRequest, request: Request, db: Session = Depends(get_db)):
    email = normalized_email(payload.email)
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="A senha deve ter pelo menos 8 caracteres")

    existing = (
        db.query(User)
        .filter(or_(func.lower(User.email) == email, func.lower(User.username) == email))
        .first()
    )
    if existing:
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
    db.commit()

    base_url = os.getenv("PUBLIC_BASE_URL") or str(request.base_url).rstrip("/")
    verification_url = f"{base_url}/auth/verify-email?token={token}"
    logger.info("Verificacao de email Total Solutions para %s: %s", email, verification_url)
    return RegisterResponse(
        message="Cadastro recebido. Confirme o email para seguir para aprovacao.",
        verification_url=verification_url,
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


@router.post("/login", response_model=AuthResponse)
def login(payload: AuthLoginRequest, db: Session = Depends(get_db)):
    identifier = payload.email.strip().lower()
    user = (
        db.query(User)
        .filter(or_(func.lower(User.email) == identifier, func.lower(User.username) == identifier))
        .first()
    )
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Usuario ou senha invalidos")

    if user.status == "PENDING_EMAIL":
        raise HTTPException(status_code=403, detail="Confirme seu email antes de entrar")
    if user.status == "PENDING_APPROVAL":
        raise HTTPException(status_code=403, detail="Cadastro aguardando aprovacao do administrador")
    if user.status == "SUSPENDED" or not user.is_active:
        raise HTTPException(status_code=403, detail="Usuario suspenso ou inativo")

    if password_needs_upgrade(user.password_hash):
        user.password_hash = hash_password(payload.password)
    user.last_seen_at = datetime.utcnow()
    if not user.status:
        user.status = "ACTIVE"
    db.commit()
    db.refresh(user)

    return AuthResponse(
        access_token=create_access_token(user),
        user=UserResponse.model_validate(user),
    )


@router.post("/logout")
def logout(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    user.last_seen_at = None
    db.commit()
    return {"offline": True}


@router.get("/me", response_model=UserResponse)
def get_authenticated_user(user: User = Depends(get_current_user)):
    return user
