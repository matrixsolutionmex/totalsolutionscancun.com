from datetime import datetime

from pydantic import BaseModel


class LoginRequest(BaseModel):
    username: str
    password: str


class UserCreate(BaseModel):
    username: str
    password: str
    manager_id: int | None = None
    full_name: str | None = None
    role: str = "BROKER"
    creci: str | None = None
    data_nascimento: str | None = None
    telefone: str | None = None
    email_pessoal: str | None = None
    documento: str | None = None
    observacoes: str | None = None
    pais_operacao: str | None = "BR"
    estado_operacao: str | None = None
    cidade_operacao: str | None = None
    idioma: str | None = "pt"
    email: str | None = None
    company: str | None = None
    plan: str | None = "FREE"
    plan_max_brokers: int | None = 1
    plan_max_leads: int | None = 100
    organization_mode: str | None = None
    organization_id: int | None = None


class UserUpdate(BaseModel):
    username: str | None = None
    password: str | None = None
    manager_id: int | None = None
    full_name: str | None = None
    role: str | None = None
    creci: str | None = None
    data_nascimento: str | None = None
    telefone: str | None = None
    email_pessoal: str | None = None
    documento: str | None = None
    observacoes: str | None = None
    pais_operacao: str | None = None
    estado_operacao: str | None = None
    cidade_operacao: str | None = None
    idioma: str | None = None
    is_active: bool | None = None
    email: str | None = None
    company: str | None = None
    plan: str | None = None
    plan_max_brokers: int | None = None
    plan_max_leads: int | None = None


class UserResponse(BaseModel):
    id: int
    organization_id: int | None = None
    manager_id: int | None = None
    username: str
    email: str | None = None
    company: str | None = None
    role: str
    full_name: str | None = None
    creci: str | None = None
    data_nascimento: str | None = None
    telefone: str | None = None
    email_pessoal: str | None = None
    documento: str | None = None
    observacoes: str | None = None
    pais_operacao: str | None = None
    estado_operacao: str | None = None
    cidade_operacao: str | None = None
    idioma: str | None = None
    profile_photo_url: str | None = None
    last_seen_at: datetime | None = None
    is_online: bool = False
    is_active: bool
    email_verified: bool = False
    status: str = "ACTIVE"
    status_reason: str | None = None
    status_changed_at: datetime | None = None
    status_changed_by: int | None = None
    archived_at: datetime | None = None
    anonymized_at: datetime | None = None
    plan: str = "FREE"
    plan_max_brokers: int = 1
    plan_max_leads: int = 100
    onboarding_source: str = "INDEPENDENT"
    registered_at: datetime | None = None

    model_config = {
        "from_attributes": True,
    }


class AssignLeadsRequest(BaseModel):
    broker_id: int
    limit: int = 100
    nicho: str | None = None
    pais: str | None = None
    estado: str | None = None
    cidade: str | None = None


class ReturnLeadsRequest(BaseModel):
    broker_id: int | None = None
    limit: int | None = None
    all: bool = False
    nicho: str | None = None
    pais: str | None = None
    estado: str | None = None
    cidade: str | None = None


class UserLifecycleRequest(BaseModel):
    reason: str


class UserReactivateRequest(BaseModel):
    reason: str
    role: str = "BROKER"
    manager_id: int | None = None
    plan: str | None = None
    plan_max_brokers: int | None = None
    plan_max_leads: int | None = None
    reset_password: bool = False


class UserArchiveRequest(BaseModel):
    reason: str
    client_action: str = "UNASSIGN"


class UserAnonymizeRequest(BaseModel):
    reason: str
    confirmation: str
    client_action: str = "UNASSIGN"


class UserLifecycleEventResponse(BaseModel):
    id: int
    user_id: int
    actor_user_id: int | None = None
    event_type: str
    from_status: str | None = None
    to_status: str
    reason: str | None = None
    created_at: datetime

    model_config = {
        "from_attributes": True,
    }


class UserReactivationRequestResponse(BaseModel):
    id: int
    user_id: int
    email: str
    requested_name: str | None = None
    current_status: str
    reason: str
    status: str
    reviewed_by: int | None = None
    reviewed_at: datetime | None = None
    review_reason: str | None = None
    created_at: datetime

    model_config = {
        "from_attributes": True,
    }
