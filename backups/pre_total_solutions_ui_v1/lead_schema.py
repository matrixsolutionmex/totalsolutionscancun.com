from datetime import datetime

from pydantic import BaseModel, Field, model_validator


class LeadResponse(BaseModel):
    id: int
    nome: str | None = None
    contato: str | None = None
    email: str | None = None
    site: str | None = None
    endereco: str | None = None
    instagram: str | None = None
    linkedin: str | None = None
    facebook: str | None = None
    redes_sociais: str | None = None
    observacoes: str | None = None
    nicho: str | None = None
    pais: str | None = None
    estado: str | None = None
    cidade: str | None = None
    score: int | None = None
    valor_negocio: float | None = None
    pipeline: str | None = None
    pipeline_updated_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    assigned_to_user_id: int | None = None

    model_config = {
        "from_attributes": True,
    }


class LeadPipelineUpdate(BaseModel):
    pipeline: str


class LeadAssignUpdate(BaseModel):
    assigned_to_user_id: int | None = None


class LeadUpdate(BaseModel):
    nome: str | None = None
    contato: str | None = None
    email: str | None = None
    site: str | None = None
    endereco: str | None = None
    instagram: str | None = None
    linkedin: str | None = None
    facebook: str | None = None
    redes_sociais: str | None = None
    observacoes: str | None = None
    nicho: str | None = None
    pais: str | None = None
    estado: str | None = None
    cidade: str | None = None
    score: int | None = None
    valor_negocio: float | None = None
    pipeline: str | None = None
    assigned_to_user_id: int | None = None


class LeadEventCreate(BaseModel):
    message: str


class LeadEventResponse(BaseModel):
    id: int
    lead_id: int
    actor_id: int | None = None
    actor_name: str | None = None
    event_type: str
    message: str
    created_at: datetime

    model_config = {
        "from_attributes": True,
    }


class LeadEnrichBatchFilter(BaseModel):
    nicho: str | None = None
    pais: str | None = None

    @model_validator(mode="after")
    def validate_filter(self):
        if not any(value and value.strip() for value in (self.nicho, self.pais)):
            raise ValueError("Informe nicho ou pais para o filtro")
        return self


class LeadEnrichBatchRequest(BaseModel):
    ids: list[int] | None = Field(default=None, min_length=1, max_length=500)
    filter: LeadEnrichBatchFilter | None = None
    limit: int = Field(default=100, ge=1, le=500)

    @model_validator(mode="after")
    def validate_selection(self):
        if (self.ids is None) == (self.filter is None):
            raise ValueError("Informe ids ou filter, mas nao os dois")
        return self


class LeadEnrichBatchResponse(BaseModel):
    status: str
    scheduled: int
