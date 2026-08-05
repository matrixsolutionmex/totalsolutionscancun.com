from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import relationship

from app.database.connection import Base

class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)

    nome = Column(String)
    contato = Column(String)
    whatsapp = Column(String)
    email = Column(String)
    site = Column(String)
    endereco = Column(String)
    colonia = Column(String)
    codigo_postal = Column(String)
    google_maps_url = Column(String)
    instagram = Column(String)
    linkedin = Column(String)
    facebook = Column(String)
    redes_sociais = Column(String)
    observacoes = Column(String)

    property_id = Column(String)
    tipo_imovel = Column(String)
    tipo_servico = Column(String)
    empresa = Column(String)
    pessoa_contato = Column(String)
    latitude = Column(String)
    longitude = Column(String)
    foto_fachada_url = Column(String)
    property_extra_json = Column(String)

    nicho = Column(String)
    descripcion_problema = Column(String)
    urgencia = Column(String, default="NORMAL")
    origen = Column(String)
    origen_detalle = Column(String)
    external_id = Column(String)
    external_source = Column(String)
    received_at = Column(DateTime, nullable=True)
    proximo_contacto = Column(DateTime, nullable=True)
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

    service_order = relationship("ServiceOrder", back_populates="lead", uselist=False)
