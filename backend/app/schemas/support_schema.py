from datetime import datetime

from pydantic import BaseModel


class SupportTicketCreate(BaseModel):
    module: str
    priority: str = "Media"
    message: str


class SupportTicketUpdate(BaseModel):
    status: str | None = None


class SupportTicketResponse(BaseModel):
    id: int
    protocol: str | None = None
    module: str
    priority: str
    message: str
    status: str
    created_by_user_id: int | None = None
    created_by_username: str | None = None
    created_by_full_name: str | None = None
    created_by_role: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {
        "from_attributes": True,
    }
