from pydantic import BaseModel

from app.schemas.user_schema import UserResponse


class RegisterRequest(BaseModel):
    full_name: str
    email: str
    password: str
    company: str | None = None
    phone: str | None = None
    plan: str = "STARTER"


class AuthLoginRequest(BaseModel):
    email: str
    password: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class RegisterResponse(BaseModel):
    message: str
    verification_url: str


class UserApprovalRequest(BaseModel):
    role: str = "BROKER"
    plan: str | None = None
    plan_max_brokers: int | None = None
    plan_max_leads: int | None = None
    manager_id: int | None = None
