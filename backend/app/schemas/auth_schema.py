from pydantic import BaseModel

from app.schemas.user_schema import UserResponse


class RegisterRequest(BaseModel):
    full_name: str
    email: str
    password: str
    company: str | None = None
    phone: str | None = None
    plan: str = "FREE"
    turnstile_token: str | None = None


class AuthLoginRequest(BaseModel):
    email: str
    password: str
    turnstile_token: str | None = None


class AuthResponse(BaseModel):
    access_token: str | None = None
    token_type: str = "bearer"
    user: UserResponse | None = None
    mfa_required: bool = False
    mfa_setup_required: bool = False
    mfa_challenge_token: str | None = None
    message: str | None = None


class RegisterResponse(BaseModel):
    message: str
    masked_email: str | None = None
    resend_after_seconds: int = 60
    email_delivery_status: str = "accepted"


class EmailVerificationResendRequest(BaseModel):
    email: str
    turnstile_token: str | None = None


class EmailVerificationChangeRequest(BaseModel):
    old_email: str
    new_email: str
    turnstile_token: str | None = None


class ReactivationRequestCreate(BaseModel):
    email: str
    reason: str
    turnstile_token: str | None = None


class ReactivationRequestResponse(BaseModel):
    message: str
    request_id: int | None = None


class UserApprovalRequest(BaseModel):
    role: str = "BROKER"
    plan: str | None = None
    plan_max_brokers: int | None = None
    plan_max_leads: int | None = None
    manager_id: int | None = None


class PublicAuthConfig(BaseModel):
    turnstile_site_key: str | None = None
    turnstile_required: bool = False
    google_client_id: str | None = None
    public_signup_enabled: bool = True


class MfaVerifyRequest(BaseModel):
    challenge_token: str
    code: str


class MfaSetupStartResponse(BaseModel):
    secret: str
    otpauth_url: str


class MfaSetupConfirmRequest(BaseModel):
    code: str


class MfaSetupConfirmResponse(BaseModel):
    recovery_codes: list[str]


class MfaSetupChallengeStartRequest(BaseModel):
    challenge_token: str
    reset_secret: bool = False


class MfaSetupChallengeConfirmRequest(BaseModel):
    challenge_token: str
    code: str


class PasswordRecoveryRequest(BaseModel):
    email: str
    turnstile_token: str | None = None


class PasswordResetRequest(BaseModel):
    token: str
    password: str
    turnstile_token: str | None = None


class GoogleLoginRequest(BaseModel):
    id_token: str
    turnstile_token: str | None = None
