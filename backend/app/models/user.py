from datetime import datetime, timedelta

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String

from app.database.connection import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    manager_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    username = Column(String, unique=True, index=True, nullable=False)
    email = Column(String, nullable=True, index=True)
    company = Column(String, nullable=True)
    password_hash = Column(String, nullable=False)
    role = Column(String, nullable=False, default="BROKER")
    full_name = Column(String)
    creci = Column(String)
    data_nascimento = Column(String)
    telefone = Column(String)
    email_pessoal = Column(String)
    documento = Column(String)
    observacoes = Column(String)
    pais_operacao = Column(String, default="BR")
    estado_operacao = Column(String, default="")
    cidade_operacao = Column(String, default="")
    idioma = Column(String, default="pt")
    profile_photo_url = Column(String, nullable=True)
    last_seen_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)
    email_verified = Column(Boolean, default=False, nullable=False)
    email_verification_token = Column(String, nullable=True, index=True)
    status = Column(String, default="PENDING_EMAIL", nullable=False)
    plan = Column(String, default="STARTER", nullable=False)
    plan_max_brokers = Column(Integer, default=1, nullable=False)
    plan_max_leads = Column(Integer, default=100, nullable=False)
    registered_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    @property
    def is_online(self):
        if not self.last_seen_at:
            return False

        return datetime.utcnow() - self.last_seen_at <= timedelta(minutes=2)
