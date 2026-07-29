from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String
from app.database.connection import Base

class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, index=True)

    nome = Column(String)
    contato = Column(String)
    email = Column(String)
    site = Column(String)
    endereco = Column(String)
    instagram = Column(String)
    linkedin = Column(String)
    facebook = Column(String)
    redes_sociais = Column(String)
    observacoes = Column(String)

    nicho = Column(String)
    pais = Column(String)
    estado = Column(String)
    cidade = Column(String)
    score = Column(Integer)
    valor_negocio = Column(Numeric(12, 2), default=0)

    pipeline = Column(String, default="NOVO LEAD")
    pipeline_updated_at = Column(DateTime, default=datetime.utcnow, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=True)
    assigned_to_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
