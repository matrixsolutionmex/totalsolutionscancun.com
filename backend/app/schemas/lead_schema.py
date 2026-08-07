from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator


ORIGENS_CLIENTE = {
    "WHATSAPP",
    "GOOGLE_BUSINESS",
    "GOOGLE_ADS",
    "META_ADS",
    "INSTAGRAM",
    "FACEBOOK",
    "LANDING_PAGE",
    "LLAMADA",
    "INDICACION",
    "CLIENTE_ANTIGUO",
    "PROSPECCION",
    "LEADVAULT",
    "SOCIO_COMERCIAL",
    "OTRO",
}

URGENCIAS_CLIENTE = {"BAJA", "NORMAL", "ALTA", "EMERGENCIA"}

TIPOS_IMOVEL = {
    "CASA",
    "APARTAMENTO",
    "HOTEL",
    "AIRBNB",
    "LOJA",
    "INDUSTRIA",
    "ESCOLA",
    "CLINICA",
    "ESCRITORIO",
    "CONDOMINIO",
    "OBRA",
    "OUTRO",
}

TIPOS_SERVICO = {
    "HIDRAULICA",
    "ELETRICA",
    "AR_CONDICIONADO",
    "PINTURA",
    "IMPERMEABILIZACAO",
    "CISTERNA",
    "MARCENARIA",
    "ALVENARIA",
    "LIMPEZA_TECNICA",
    "OUTRO",
}


def normalize_enum(value: str | None, allowed: set[str], default: str | None = None) -> str | None:
    if value is None or str(value).strip() == "":
        return default

    normalized = str(value).strip().upper()
    if normalized not in allowed:
        raise ValueError(f"Valor invalido: {value}")
    return normalized


class ServiceOrderResponse(BaseModel):
    id: int
    order_number: str | None = None
    lead_id: int
    property_id: str | None = None
    property_record_id: int | None = None
    service_request_id: int | None = None
    status: str
    warranty_days: int
    opened_at: datetime | None = None
    scheduled_at: datetime | None = None
    completed_at: datetime | None = None
    responsible_user_id: int | None = None
    supervisor_user_id: int | None = None
    signature_status: str
    qr_token: str | None = None
    warranty_seal_status: str
    checklist_status: str
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {
        "from_attributes": True,
    }


class LeadResponse(BaseModel):
    id: int
    nome: str | None = None
    contato: str | None = None
    whatsapp: str | None = None
    email: str | None = None
    site: str | None = None
    endereco: str | None = None
    colonia: str | None = None
    codigo_postal: str | None = None
    google_maps_url: str | None = None
    instagram: str | None = None
    linkedin: str | None = None
    facebook: str | None = None
    redes_sociais: str | None = None
    observacoes: str | None = None
    property_id: str | None = None
    tipo_imovel: str | None = None
    tipo_servico: str | None = None
    empresa: str | None = None
    pessoa_contato: str | None = None
    latitude: str | None = None
    longitude: str | None = None
    foto_fachada_url: str | None = None
    property_extra_json: str | None = None
    nicho: str | None = None
    descripcion_problema: str | None = None
    urgencia: str | None = None
    origen: str | None = None
    origen_detalle: str | None = None
    external_id: str | None = None
    external_source: str | None = None
    received_at: datetime | None = None
    proximo_contacto: datetime | None = None
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
    service_order: ServiceOrderResponse | None = None

    model_config = {
        "from_attributes": True,
    }


class LeadPipelineUpdate(BaseModel):
    pipeline: str


class LeadAssignUpdate(BaseModel):
    assigned_to_user_id: int | None = None


class LeadCreate(BaseModel):
    nombre: str | None = None
    nome: str | None = None
    telefono: str | None = None
    contato: str | None = None
    whatsapp: str | None = None
    email: str | None = None
    direccion: str | None = None
    endereco: str | None = None
    colonia: str | None = None
    ciudad: str | None = None
    cidade: str | None = None
    estado: str | None = None
    codigo_postal: str | None = None
    google_maps_url: str | None = None
    servicio_solicitado: str | None = None
    nicho: str | None = None
    tipo_imovel: str = "OUTRO"
    tipo_servico: str = "OUTRO"
    empresa: str | None = None
    pessoa_contato: str | None = None
    latitude: str | None = None
    longitude: str | None = None
    foto_fachada_url: str | None = None
    property_extra: dict[str, Any] | None = None
    property_extra_json: str | None = None
    descripcion_problema: str | None = None
    urgencia: str = "NORMAL"
    origen: str = "OTRO"
    origen_detalle: str | None = None
    responsable: int | None = None
    assigned_to_user_id: int | None = None
    proximo_contacto: datetime | None = None
    observaciones: str | None = None
    observacoes: str | None = None
    pais: str | None = "MX"

    @model_validator(mode="after")
    def validate_payload(self):
        self.urgencia = normalize_enum(self.urgencia, URGENCIAS_CLIENTE, "NORMAL") or "NORMAL"
        self.origen = normalize_enum(self.origen, ORIGENS_CLIENTE, "OTRO") or "OTRO"
        self.tipo_imovel = normalize_enum(self.tipo_imovel, TIPOS_IMOVEL, "OUTRO") or "OUTRO"
        self.tipo_servico = normalize_enum(self.tipo_servico, TIPOS_SERVICO, "OUTRO") or "OUTRO"
        if not (self.nombre or self.nome or self.telefono or self.contato or self.whatsapp or self.email):
            raise ValueError("Informe nombre, telefono, whatsapp ou email")
        return self


class IntegrationLeadCreate(BaseModel):
    nombre: str | None = None
    telefono: str | None = None
    whatsapp: str | None = None
    email: str | None = None
    direccion: str | None = None
    ciudad: str | None = None
    servicio_solicitado: str | None = None
    tipo_imovel: str = "OUTRO"
    tipo_servico: str = "OUTRO"
    property_extra: dict[str, Any] | None = None
    property_extra_json: str | None = None
    descripcion_problema: str | None = None
    origen: str = "OTRO"
    origen_detalle: str | None = None
    external_id: str | None = None

    @model_validator(mode="after")
    def validate_payload(self):
        self.origen = normalize_enum(self.origen, ORIGENS_CLIENTE, "OTRO") or "OTRO"
        self.tipo_imovel = normalize_enum(self.tipo_imovel, TIPOS_IMOVEL, "OUTRO") or "OUTRO"
        self.tipo_servico = normalize_enum(self.tipo_servico, TIPOS_SERVICO, "OUTRO") or "OUTRO"
        if not (self.nombre or self.telefono or self.whatsapp or self.email):
            raise ValueError("Informe nombre, telefono, whatsapp ou email")
        return self


class LeadUpdate(BaseModel):
    nome: str | None = None
    contato: str | None = None
    whatsapp: str | None = None
    email: str | None = None
    site: str | None = None
    endereco: str | None = None
    colonia: str | None = None
    codigo_postal: str | None = None
    google_maps_url: str | None = None
    instagram: str | None = None
    linkedin: str | None = None
    facebook: str | None = None
    redes_sociais: str | None = None
    observacoes: str | None = None
    property_id: str | None = None
    tipo_imovel: str | None = None
    tipo_servico: str | None = None
    empresa: str | None = None
    pessoa_contato: str | None = None
    latitude: str | None = None
    longitude: str | None = None
    foto_fachada_url: str | None = None
    property_extra: dict[str, Any] | None = None
    property_extra_json: str | None = None
    nicho: str | None = None
    descripcion_problema: str | None = None
    urgencia: str | None = None
    origen: str | None = None
    origen_detalle: str | None = None
    proximo_contacto: datetime | None = None
    pais: str | None = None
    estado: str | None = None
    cidade: str | None = None
    score: int | None = None
    valor_negocio: float | None = None
    pipeline: str | None = None
    assigned_to_user_id: int | None = None

    @model_validator(mode="after")
    def validate_payload(self):
        if self.urgencia is not None:
            self.urgencia = normalize_enum(self.urgencia, URGENCIAS_CLIENTE)
        if self.origen is not None:
            self.origen = normalize_enum(self.origen, ORIGENS_CLIENTE)
        if self.tipo_imovel is not None:
            self.tipo_imovel = normalize_enum(self.tipo_imovel, TIPOS_IMOVEL)
        if self.tipo_servico is not None:
            self.tipo_servico = normalize_enum(self.tipo_servico, TIPOS_SERVICO)
        return self


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
