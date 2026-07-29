from pydantic import BaseModel
from typing import Optional


class ContractCreate(BaseModel):
    lead_id: int
    contract_type: str

    client_name: Optional[str] = None
    client_email: Optional[str] = None
    client_phone: Optional[str] = None

    property_address: Optional[str] = None
    business_value: Optional[str] = None
    commission_value: Optional[str] = None

    notes: Optional[str] = None


class ContractResponse(ContractCreate):
    id: int
    status: str

    class Config:
        from_attributes = True