from datetime import datetime

from pydantic import BaseModel, Field


class MatrixLeadImportRecord(BaseModel):
    nome: str | None = None
    contato: str | None = None
    email: str | None = None
    site: str | None = None
    endereco: str | None = None
    nicho: str | None = None
    pais: str | None = None
    score: int | None = None
    instagram: str | None = None
    linkedin: str | None = None
    facebook: str | None = None
    redes_sociais: str | None = None
    observacoes: str | None = None
    valor_negocio: float | None = None


class MatrixLeadImportRequest(BaseModel):
    source: str = Field(default="TotalSolutions_Import", min_length=1)
    batch_id: str = Field(min_length=1, max_length=120)
    sent_at: datetime | None = None
    records: list[MatrixLeadImportRecord] = Field(min_length=1, max_length=20000)


class ImportJobResponse(BaseModel):
    id: int
    source: str
    batch_id: str
    status: str
    total_received: int
    inserted: int
    skipped_duplicates: int
    invalid: int
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    model_config = {
        "from_attributes": True,
    }
