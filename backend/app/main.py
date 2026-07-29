import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.security import hash_password
from app.core.storage import UPLOADS_DIR
from app.auth.routes import router as auth_router
from app.database.connection import Base, SessionLocal, engine
from app.models import import_job, lead, lead_event, support_ticket, user, contract, contract_event, lead_document, service_order
from app.models.lead import Lead
from app.models.service_order import ServiceOrder
from app.models.user import User
from app.routes.import_routes import router as import_router
from app.routes.integration_routes import router as integration_router
from app.routes.admin_routes import router as admin_router
from app.routes.lead_routes import router as lead_router
from app.routes.lead_document_routes import router as lead_document_router
from app.routes.support_routes import router as support_router
from app.routes.user_routes import router as user_router
from app.routes.contract_routes import router as contract_router
from app.services.service_order_service import ensure_service_order

app = FastAPI(
    title="Total Solutions CRM",
    version="1.0.0"
)

logger = logging.getLogger(__name__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(lead_router)
app.include_router(lead_document_router)
app.include_router(user_router)
app.include_router(support_router)
app.include_router(import_router)
app.include_router(integration_router)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(contract_router)

frontend_dir = Path(__file__).resolve().parents[2] / "frontend"
if frontend_dir.exists():
    app.mount("/assets", StaticFiles(directory=frontend_dir), name="assets")

app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")


def ensure_index(db, primary_sql: str, *, fallback_sql: str | None = None, label: str = ""):
    try:
        db.execute(text(primary_sql))
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        if not fallback_sql:
            raise

        logger.warning("Falha ao criar indice %s. Aplicando fallback seguro. Motivo: %s", label or primary_sql, exc)
        db.execute(text(fallback_sql))
        db.commit()


@app.on_event("startup")
def create_database_tables():
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS valor_negocio NUMERIC(12, 2) DEFAULT 0"))
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS pipeline_updated_at TIMESTAMP"))
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS created_at TIMESTAMP"))
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP"))
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS estado VARCHAR"))
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS cidade VARCHAR"))
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS whatsapp VARCHAR"))
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS colonia VARCHAR"))
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS codigo_postal VARCHAR"))
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS google_maps_url VARCHAR"))
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS descripcion_problema VARCHAR"))
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS urgencia VARCHAR DEFAULT 'NORMAL'"))
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS origen VARCHAR"))
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS origen_detalle VARCHAR"))
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS external_id VARCHAR"))
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS external_source VARCHAR"))
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS received_at TIMESTAMP"))
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS proximo_contacto TIMESTAMP"))
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS property_id VARCHAR"))
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS tipo_imovel VARCHAR"))
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS tipo_servico VARCHAR"))
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS empresa VARCHAR"))
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS pessoa_contato VARCHAR"))
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS latitude VARCHAR"))
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS longitude VARCHAR"))
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS foto_fachada_url VARCHAR"))
        db.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS property_extra_json VARCHAR"))
        db.execute(text("UPDATE leads SET urgencia = 'NORMAL' WHERE urgencia IS NULL OR urgencia = ''"))
        db.execute(text("UPDATE leads SET tipo_imovel = 'OUTRO' WHERE tipo_imovel IS NULL OR tipo_imovel = ''"))
        db.execute(text("UPDATE leads SET tipo_servico = COALESCE(NULLIF(tipo_servico, ''), NULLIF(nicho, ''), 'OUTRO') WHERE tipo_servico IS NULL OR tipo_servico = ''"))
        db.execute(text("UPDATE leads SET pipeline_updated_at = COALESCE(pipeline_updated_at, updated_at, CURRENT_TIMESTAMP) WHERE pipeline_updated_at IS NULL"))
        db.execute(text("UPDATE leads SET created_at = COALESCE(created_at, pipeline_updated_at, CURRENT_TIMESTAMP) WHERE created_at IS NULL"))
        db.execute(text("UPDATE leads SET updated_at = COALESCE(updated_at, pipeline_updated_at, CURRENT_TIMESTAMP) WHERE updated_at IS NULL"))
        db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS manager_id INTEGER"))
        db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS pais_operacao VARCHAR DEFAULT 'BR'"))
        db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS estado_operacao VARCHAR DEFAULT ''"))
        db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS cidade_operacao VARCHAR DEFAULT ''"))
        db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS idioma VARCHAR DEFAULT 'pt'"))
        db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_photo_url VARCHAR"))
        db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMP"))
        db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR"))
        db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS company VARCHAR"))
        db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN DEFAULT TRUE"))
        db.execute(text("ALTER TABLE users ALTER COLUMN email_verified SET DEFAULT FALSE"))
        db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verification_token VARCHAR"))
        db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT 'ACTIVE'"))
        db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS plan VARCHAR DEFAULT 'STARTER'"))
        db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS plan_max_brokers INTEGER DEFAULT 1"))
        db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS plan_max_leads INTEGER DEFAULT 100"))
        db.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"))
        db.execute(text("UPDATE users SET email_verified = TRUE WHERE email_verified IS NULL"))
        db.execute(text("UPDATE users SET status = 'ACTIVE' WHERE status IS NULL OR status = ''"))
        db.execute(text("UPDATE users SET plan = 'STARTER' WHERE plan IS NULL OR plan = ''"))
        db.execute(text("UPDATE users SET plan_max_brokers = 1 WHERE plan_max_brokers IS NULL"))
        db.execute(text("UPDATE users SET plan_max_leads = 100 WHERE plan_max_leads IS NULL"))
        db.execute(text("UPDATE users SET registered_at = CURRENT_TIMESTAMP WHERE registered_at IS NULL"))
        db.commit()

        for existing_lead in db.query(Lead).filter((Lead.property_id.is_(None)) | (Lead.property_id == "")).all():
            existing_lead.property_id = f"TS-{existing_lead.id:06d}"
        db.commit()

        for existing_lead in db.query(Lead).outerjoin(ServiceOrder, ServiceOrder.lead_id == Lead.id).filter(ServiceOrder.id.is_(None)).all():
            ensure_service_order(db, existing_lead)
        db.commit()

        ensure_index(
            db,
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_lower ON users (LOWER(email)) WHERE email IS NOT NULL AND email <> ''",
            label="uq_users_email_lower",
        )
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_users_status ON users (status)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_users_verification_token ON users (email_verification_token)")

        ensure_index(
            db,
            "CREATE INDEX IF NOT EXISTS idx_leads_email_lower ON leads (LOWER(email))",
            fallback_sql="CREATE INDEX IF NOT EXISTS idx_leads_email_lower_hash ON leads (md5(lower(coalesce(email, ''))))",
            label="idx_leads_email_lower",
        )
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_leads_contato ON leads (contato)")
        ensure_index(
            db,
            "CREATE INDEX IF NOT EXISTS idx_leads_site_lower ON leads (LOWER(site))",
            fallback_sql="CREATE INDEX IF NOT EXISTS idx_leads_site_lower_hash ON leads (md5(lower(coalesce(site, ''))))",
            label="idx_leads_site_lower",
        )
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_leads_nicho ON leads (nicho)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_leads_pais ON leads (pais)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_leads_estado ON leads (estado)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_leads_cidade ON leads (cidade)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_leads_pais_estado_cidade ON leads (pais, estado, cidade)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_leads_assigned_pipeline ON leads (assigned_to_user_id, pipeline)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_leads_origen ON leads (origen)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_leads_urgencia ON leads (urgencia)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_leads_proximo_contacto ON leads (proximo_contacto)")
        ensure_index(db, "CREATE UNIQUE INDEX IF NOT EXISTS uq_leads_property_id ON leads (property_id)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_leads_tipo_imovel ON leads (tipo_imovel)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_leads_tipo_servico ON leads (tipo_servico)")
        ensure_index(db, "CREATE UNIQUE INDEX IF NOT EXISTS uq_service_orders_lead_id ON service_orders (lead_id)")
        ensure_index(db, "CREATE UNIQUE INDEX IF NOT EXISTS uq_service_orders_order_number ON service_orders (order_number)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_service_orders_status ON service_orders (status)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_service_orders_property_id ON service_orders (property_id)")
        ensure_index(
            db,
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_leads_external_source_id ON leads (external_source, external_id) WHERE external_source IS NOT NULL AND external_source <> '' AND external_id IS NOT NULL AND external_id <> ''",
            label="uq_leads_external_source_id",
        )

        if db.query(User).count() == 0:
            root_user = User(
                username=os.getenv("ROOT_USERNAME", "root"),
                password_hash=hash_password(os.getenv("ROOT_PASSWORD", "12345m*")),
                role="ROOT",
                full_name=os.getenv("ROOT_FULL_NAME", "Administrador Total Solutions"),
                is_active=True,
                email_verified=True,
                status="ACTIVE",
                plan="ENTERPRISE",
            )
            db.add(root_user)
            db.commit()
    finally:
        db.close()


@app.get("/")
def home():
    frontend_index = frontend_dir / "index.html"
    if frontend_index.exists():
        return FileResponse(frontend_index)

    return {
        "status": "online",
        "sistema": "Total Solutions CRM",
        "mensagem": "CRM iniciado com sucesso"
    }


@app.get("/health")
def health():
    return {"status": "online"}
