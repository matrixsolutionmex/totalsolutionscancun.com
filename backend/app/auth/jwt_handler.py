import logging
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.database.connection import SessionLocal
from app.models.user import User
from app.core.auth_security import validate_session_cookie


logger = logging.getLogger(__name__)
bearer_scheme = HTTPBearer(auto_error=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def jwt_secret_key() -> str:
    secret = (
        os.getenv("JWT_SECRET_KEY")
        or os.getenv("ROOT_KEY")
        or os.getenv("ROOT_PASSWORD")
    )
    if secret:
        return secret

    logger.warning("JWT_SECRET_KEY ausente; usando chave somente para desenvolvimento")
    return "totalsolutions-development-key-change-me"


def create_access_token(user: User) -> str:
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=int(os.getenv("JWT_EXPIRE_HOURS", "12")))
    payload = {
        "sub": str(user.id),
        "organization_id": user.organization_id,
        "role": user.role,
        "manager_id": user.manager_id,
        "session_version": user.session_version or 0,
        "iat": now,
        "exp": expires,
        "jti": uuid4().hex,
    }
    return jwt.encode(
        payload,
        jwt_secret_key(),
        algorithm=os.getenv("JWT_ALGORITHM", "HS256"),
    )


def verify_access_token(token: str) -> dict:
    try:
        return jwt.decode(
            token,
            jwt_secret_key(),
            algorithms=[os.getenv("JWT_ALGORITHM", "HS256")],
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessao invalida ou expirada",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def user_can_access(user: User) -> bool:
    return user.is_active and (not user.status or user.status == "ACTIVE")


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    x_actor_id: int | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    user_id = None
    payload = None

    if credentials:
        if credentials.scheme.lower() != "bearer":
            raise HTTPException(status_code=401, detail="Tipo de autenticacao invalido")
        payload = verify_access_token(credentials.credentials)
        try:
            user_id = int(payload.get("sub", ""))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=401, detail="Sessao invalida") from exc
    elif (
        x_actor_id is not None
        and os.getenv("ALLOW_LEGACY_ACTOR_HEADER", "false").lower() == "true"
    ):
        user_id = x_actor_id
    else:
        cookie_user = validate_session_cookie(db, request)
        if cookie_user:
            user_id = cookie_user.id

    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Autenticacao obrigatoria",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user_can_access(user):
        raise HTTPException(status_code=401, detail="Usuario inativo ou nao autorizado")
    if payload and int(payload.get("session_version", -1)) != int(user.session_version or 0):
        raise HTTPException(status_code=401, detail="Sessao revogada")
    if payload and payload.get("organization_id") and int(payload["organization_id"]) != int(user.organization_id or 0):
        raise HTTPException(status_code=401, detail="Sessao fora da organizacao")

    return user


def require_admin_user(user: User = Depends(get_current_user)) -> User:
    if user.role not in {"ROOT", "GERENTE"}:
        raise HTTPException(status_code=403, detail="Somente root ou gerente pode executar esta acao")
    return user


def require_root_user(user: User = Depends(get_current_user)) -> User:
    if user.role != "ROOT":
        raise HTTPException(status_code=403, detail="Somente root pode executar esta acao")
    return user
