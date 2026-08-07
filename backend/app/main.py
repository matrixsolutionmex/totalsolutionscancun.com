import logging
import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.security import hash_password
from app.core.organization import get_or_create_default_organization
from app.core.storage import UPLOADS_DIR
from app.auth.routes import router as auth_router
from app.database.connection import Base, SessionLocal, engine
from app.models import import_job, lead, lead_event, support_ticket, user, contract, contract_event, lead_document, service_order, deletion_request, notification, user_lifecycle, auth_security, organization, service_property, service_request
from app.models.lead import Lead
from app.models.service_order import ServiceOrder
from app.models.user import User
from app.routes.import_routes import router as import_router
from app.routes.integration_routes import router as integration_router
from app.routes.admin_routes import router as admin_router
from app.routes.lead_routes import router as lead_router
from app.routes.lead_document_routes import router as lead_document_router
from app.routes.notification_routes import router as notification_router
from app.routes.public_service_request_routes import router as public_service_request_router
from app.routes.service_request_routes import router as service_request_router
from app.routes.support_routes import router as support_router
from app.routes.user_routes import router as user_router
from app.routes.contract_routes import router as contract_router
from app.services.service_order_service import ensure_service_order
from app.services.notification_service import process_email_outbox

app = FastAPI(
    title="Total Solutions CRM",
    version="1.0.0"
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
email_worker_started = False


def startup_log(message: str):
    logger.info(message)
    print(f"[startup] {message}", flush=True)


def cors_origins():
    configured = os.getenv("CORS_ORIGINS", "").strip()
    if not configured:
        public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
        if public_base_url:
            return [public_base_url]
        if os.getenv("ENVIRONMENT", "").lower() == "production":
            return ["https://totalsolutionscancun.com"]
        return ["http://127.0.0.1:8010", "http://localhost:8010"]
    return [origin.strip() for origin in configured.split(",") if origin.strip()]


app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(lead_router)
app.include_router(lead_document_router)
app.include_router(notification_router)
app.include_router(user_router)
app.include_router(support_router)
app.include_router(import_router)
app.include_router(integration_router)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(contract_router)
app.include_router(public_service_request_router)
app.include_router(service_request_router)

frontend_dir = Path(__file__).resolve().parents[2] / "frontend"
if frontend_dir.exists():
    app.mount("/assets", StaticFiles(directory=frontend_dir), name="assets")
    static_dir = frontend_dir / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    no_store_paths = {"/", "/sw.js", "/solicitar-servico"}
    if request.url.path in no_store_paths or request.url.path.startswith("/acompanhar/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["CDN-Cache-Control"] = "no-store"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(self), geolocation=(self)")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com https://accounts.google.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: blob: https://lh3.googleusercontent.com; "
        "connect-src 'self' https://challenges.cloudflare.com https://accounts.google.com; "
        "frame-src https://challenges.cloudflare.com https://accounts.google.com; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'",
    )
    if request.url.scheme == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


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


def add_column_if_missing(db, table_name: str, column_name: str, column_definition: str):
    cache_key = (id(db.bind), table_name)
    if not hasattr(add_column_if_missing, "_column_cache"):
        add_column_if_missing._column_cache = {}

    column_cache = add_column_if_missing._column_cache
    if cache_key not in column_cache:
        column_cache[cache_key] = {
            column["name"]
            for column in inspect(db.bind).get_columns(table_name)
        }

    existing_columns = {
        column_name
        for column_name in column_cache[cache_key]
    }
    if column_name in existing_columns:
        return

    db.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}"))
    column_cache[cache_key].add(column_name)


def start_email_outbox_worker():
    global email_worker_started
    if email_worker_started or os.getenv("EMAIL_OUTBOX_WORKER_ENABLED", "true").lower() != "true":
        return

    email_worker_started = True

    def worker():
        while True:
            db = SessionLocal()
            try:
                process_email_outbox(db)
            except Exception as exc:  # noqa: BLE001 - background worker must not crash the app.
                logger.warning("Falha ao processar email_outbox: %s", exc.__class__.__name__)
            finally:
                db.close()
            time.sleep(int(os.getenv("EMAIL_OUTBOX_INTERVAL_SECONDS", "30")))

    threading.Thread(target=worker, name="email-outbox-worker", daemon=True).start()


def ensure_root_user(db, default_organization_id):
    root_username = os.getenv("ROOT_USERNAME", "root").strip() or "root"
    root_password = os.getenv(
        "ROOT_PASSWORD",
        "" if os.getenv("ENVIRONMENT", "").lower() == "production" else "12345m*",
    ).strip()
    root_full_name = os.getenv("ROOT_FULL_NAME", "Administrador Total Solutions").strip()
    root_email = os.getenv("ROOT_EMAIL", "").strip().lower()
    if not root_email and "@" in root_username:
        root_email = root_username.lower()

    root_user = db.query(User).filter(User.username == root_username).first()
    if not root_user and root_email:
        root_user = db.query(User).filter(User.email == root_email).first()

    if not root_user:
        if not root_password:
            startup_log("Usuario ROOT nao encontrado e ROOT_PASSWORD nao configurado.")
            return
        startup_log("Criando usuario ROOT a partir de variaveis seguras.")
        root_user = User(
            organization_id=default_organization_id,
            username=root_username,
            email=root_email or None,
            password_hash=hash_password(root_password),
            role="ROOT",
            full_name=root_full_name or "Administrador Total Solutions",
            is_active=True,
            email_verified=True,
            status="ACTIVE",
            plan="ENTERPRISE",
        )
        db.add(root_user)
        db.commit()
        return

    changed = False
    if root_user.username != root_username:
        root_user.username = root_username
        changed = True
    if root_email and root_user.email != root_email:
        root_user.email = root_email
        changed = True
    if root_password:
        root_user.password_hash = hash_password(root_password)
        changed = True
    if root_full_name and root_user.full_name != root_full_name:
        root_user.full_name = root_full_name
        changed = True
    if root_user.organization_id is None:
        root_user.organization_id = default_organization_id
        changed = True

    root_defaults = {
        "role": "ROOT",
        "is_active": True,
        "email_verified": True,
        "status": "ACTIVE",
        "plan": "ENTERPRISE",
    }
    for attr, expected in root_defaults.items():
        if getattr(root_user, attr) != expected:
            setattr(root_user, attr, expected)
            changed = True

    if changed:
        startup_log("Sincronizando usuario ROOT a partir de variaveis seguras.")
        db.commit()


@app.on_event("startup")
def create_database_tables():
    startup_log("Iniciando preparacao do banco de dados.")
    Base.metadata.create_all(bind=engine)
    startup_log("Tabelas verificadas/criadas com sucesso.")

    db = SessionLocal()
    try:
        startup_log("Verificando colunas e defaults de producao.")
        default_organization = get_or_create_default_organization(db)
        default_organization_id = default_organization.id
        db.commit()

        for table_name in (
            "users",
            "leads",
            "service_orders",
            "lead_documents",
            "lead_events",
            "deletion_requests",
            "notifications",
            "notification_preferences",
            "email_outbox",
            "web_push_subscriptions",
            "support_tickets",
            "contracts",
            "contract_events",
            "import_jobs",
            "user_lifecycle_events",
            "user_reactivation_requests",
            "user_identities",
            "user_sessions",
            "password_reset_tokens",
            "mfa_recovery_codes",
            "auth_audit_events",
            "auth_rate_limits",
            "properties",
            "service_requests",
            "service_request_media",
        ):
            add_column_if_missing(db, table_name, "organization_id", "INTEGER")

        add_column_if_missing(db, "leads", "valor_negocio", "NUMERIC(12, 2) DEFAULT 0")
        add_column_if_missing(db, "leads", "pipeline_updated_at", "TIMESTAMP")
        add_column_if_missing(db, "leads", "created_at", "TIMESTAMP")
        add_column_if_missing(db, "leads", "updated_at", "TIMESTAMP")
        add_column_if_missing(db, "leads", "estado", "VARCHAR")
        add_column_if_missing(db, "leads", "cidade", "VARCHAR")
        add_column_if_missing(db, "leads", "whatsapp", "VARCHAR")
        add_column_if_missing(db, "leads", "colonia", "VARCHAR")
        add_column_if_missing(db, "leads", "codigo_postal", "VARCHAR")
        add_column_if_missing(db, "leads", "google_maps_url", "VARCHAR")
        add_column_if_missing(db, "leads", "descripcion_problema", "VARCHAR")
        add_column_if_missing(db, "leads", "urgencia", "VARCHAR DEFAULT 'NORMAL'")
        add_column_if_missing(db, "leads", "origen", "VARCHAR")
        add_column_if_missing(db, "leads", "origen_detalle", "VARCHAR")
        add_column_if_missing(db, "leads", "external_id", "VARCHAR")
        add_column_if_missing(db, "leads", "external_source", "VARCHAR")
        add_column_if_missing(db, "leads", "received_at", "TIMESTAMP")
        add_column_if_missing(db, "leads", "proximo_contacto", "TIMESTAMP")
        add_column_if_missing(db, "leads", "property_id", "VARCHAR")
        add_column_if_missing(db, "leads", "tipo_imovel", "VARCHAR")
        add_column_if_missing(db, "leads", "tipo_servico", "VARCHAR")
        add_column_if_missing(db, "leads", "empresa", "VARCHAR")
        add_column_if_missing(db, "leads", "pessoa_contato", "VARCHAR")
        add_column_if_missing(db, "leads", "latitude", "VARCHAR")
        add_column_if_missing(db, "leads", "longitude", "VARCHAR")
        add_column_if_missing(db, "leads", "foto_fachada_url", "VARCHAR")
        add_column_if_missing(db, "leads", "property_extra_json", "VARCHAR")
        db.execute(text("UPDATE leads SET urgencia = 'NORMAL' WHERE urgencia IS NULL OR urgencia = ''"))
        db.execute(text("UPDATE leads SET tipo_imovel = 'OUTRO' WHERE tipo_imovel IS NULL OR tipo_imovel = ''"))
        db.execute(text("UPDATE leads SET tipo_servico = COALESCE(NULLIF(tipo_servico, ''), NULLIF(nicho, ''), 'OUTRO') WHERE tipo_servico IS NULL OR tipo_servico = ''"))
        db.execute(text("UPDATE leads SET pipeline_updated_at = COALESCE(pipeline_updated_at, updated_at, CURRENT_TIMESTAMP) WHERE pipeline_updated_at IS NULL"))
        db.execute(text("UPDATE leads SET created_at = COALESCE(created_at, pipeline_updated_at, CURRENT_TIMESTAMP) WHERE created_at IS NULL"))
        db.execute(text("UPDATE leads SET updated_at = COALESCE(updated_at, pipeline_updated_at, CURRENT_TIMESTAMP) WHERE updated_at IS NULL"))
        add_column_if_missing(db, "users", "manager_id", "INTEGER")
        add_column_if_missing(db, "users", "pais_operacao", "VARCHAR DEFAULT 'BR'")
        add_column_if_missing(db, "users", "estado_operacao", "VARCHAR DEFAULT ''")
        add_column_if_missing(db, "users", "cidade_operacao", "VARCHAR DEFAULT ''")
        add_column_if_missing(db, "users", "idioma", "VARCHAR DEFAULT 'pt'")
        add_column_if_missing(db, "users", "profile_photo_url", "VARCHAR")
        add_column_if_missing(db, "users", "last_seen_at", "TIMESTAMP")
        add_column_if_missing(db, "users", "email", "VARCHAR")
        add_column_if_missing(db, "users", "company", "VARCHAR")
        add_column_if_missing(db, "users", "email_verified", "BOOLEAN DEFAULT TRUE")
        if engine.dialect.name == "postgresql":
            db.execute(text("ALTER TABLE users ALTER COLUMN email_verified SET DEFAULT FALSE"))
        add_column_if_missing(db, "users", "email_verification_token", "VARCHAR")
        add_column_if_missing(db, "users", "email_verification_token_hash", "VARCHAR")
        add_column_if_missing(db, "users", "email_verification_expires_at", "TIMESTAMP")
        add_column_if_missing(db, "users", "email_verification_sent_at", "TIMESTAMP")
        add_column_if_missing(db, "users", "email_verification_used_at", "TIMESTAMP")
        add_column_if_missing(db, "users", "status", "VARCHAR DEFAULT 'ACTIVE'")
        add_column_if_missing(db, "users", "status_reason", "VARCHAR")
        add_column_if_missing(db, "users", "status_changed_at", "TIMESTAMP")
        add_column_if_missing(db, "users", "status_changed_by", "INTEGER")
        add_column_if_missing(db, "users", "archived_at", "TIMESTAMP")
        add_column_if_missing(db, "users", "anonymized_at", "TIMESTAMP")
        add_column_if_missing(db, "users", "session_version", "INTEGER DEFAULT 0")
        add_column_if_missing(db, "users", "mfa_enabled", "BOOLEAN DEFAULT FALSE")
        add_column_if_missing(db, "users", "mfa_secret_encrypted", "VARCHAR")
        add_column_if_missing(db, "users", "mfa_confirmed_at", "TIMESTAMP")
        add_column_if_missing(db, "users", "mfa_last_counter", "INTEGER")
        add_column_if_missing(db, "users", "plan", "VARCHAR DEFAULT 'STARTER'")
        add_column_if_missing(db, "users", "plan_max_brokers", "INTEGER DEFAULT 1")
        add_column_if_missing(db, "users", "plan_max_leads", "INTEGER DEFAULT 100")
        add_column_if_missing(db, "users", "registered_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        add_column_if_missing(db, "service_orders", "property_record_id", "INTEGER")
        add_column_if_missing(db, "service_orders", "service_request_id", "INTEGER")
        db.execute(text("UPDATE users SET organization_id = :organization_id WHERE organization_id IS NULL"), {"organization_id": default_organization_id})
        db.execute(text("UPDATE leads SET organization_id = :organization_id WHERE organization_id IS NULL"), {"organization_id": default_organization_id})
        db.execute(text("UPDATE service_orders SET organization_id = :organization_id WHERE organization_id IS NULL"), {"organization_id": default_organization_id})
        db.execute(text("UPDATE properties SET organization_id = :organization_id WHERE organization_id IS NULL"), {"organization_id": default_organization_id})
        db.execute(text("UPDATE service_requests SET organization_id = :organization_id WHERE organization_id IS NULL"), {"organization_id": default_organization_id})
        db.execute(text("UPDATE service_request_media SET organization_id = :organization_id WHERE organization_id IS NULL"), {"organization_id": default_organization_id})
        db.execute(text("UPDATE lead_documents SET organization_id = :organization_id WHERE organization_id IS NULL"), {"organization_id": default_organization_id})
        db.execute(text("UPDATE lead_events SET organization_id = :organization_id WHERE organization_id IS NULL"), {"organization_id": default_organization_id})
        db.execute(text("UPDATE deletion_requests SET organization_id = :organization_id WHERE organization_id IS NULL"), {"organization_id": default_organization_id})
        db.execute(text("UPDATE notifications SET organization_id = :organization_id WHERE organization_id IS NULL"), {"organization_id": default_organization_id})
        db.execute(text("UPDATE notification_preferences SET organization_id = :organization_id WHERE organization_id IS NULL"), {"organization_id": default_organization_id})
        db.execute(text("UPDATE email_outbox SET organization_id = :organization_id WHERE organization_id IS NULL"), {"organization_id": default_organization_id})
        db.execute(text("UPDATE web_push_subscriptions SET organization_id = :organization_id WHERE organization_id IS NULL"), {"organization_id": default_organization_id})
        db.execute(text("UPDATE support_tickets SET organization_id = :organization_id WHERE organization_id IS NULL"), {"organization_id": default_organization_id})
        db.execute(text("UPDATE contracts SET organization_id = :organization_id WHERE organization_id IS NULL"), {"organization_id": default_organization_id})
        db.execute(text("UPDATE contract_events SET organization_id = :organization_id WHERE organization_id IS NULL"), {"organization_id": default_organization_id})
        db.execute(text("UPDATE import_jobs SET organization_id = :organization_id WHERE organization_id IS NULL"), {"organization_id": default_organization_id})
        db.execute(text("UPDATE user_lifecycle_events SET organization_id = :organization_id WHERE organization_id IS NULL"), {"organization_id": default_organization_id})
        db.execute(text("UPDATE user_reactivation_requests SET organization_id = :organization_id WHERE organization_id IS NULL"), {"organization_id": default_organization_id})
        db.execute(text("UPDATE user_identities SET organization_id = :organization_id WHERE organization_id IS NULL"), {"organization_id": default_organization_id})
        db.execute(text("UPDATE user_sessions SET organization_id = :organization_id WHERE organization_id IS NULL"), {"organization_id": default_organization_id})
        db.execute(text("UPDATE password_reset_tokens SET organization_id = :organization_id WHERE organization_id IS NULL"), {"organization_id": default_organization_id})
        db.execute(text("UPDATE mfa_recovery_codes SET organization_id = :organization_id WHERE organization_id IS NULL"), {"organization_id": default_organization_id})
        db.execute(text("UPDATE auth_audit_events SET organization_id = :organization_id WHERE organization_id IS NULL"), {"organization_id": default_organization_id})
        db.execute(text("UPDATE auth_rate_limits SET organization_id = :organization_id WHERE organization_id IS NULL"), {"organization_id": default_organization_id})
        migrate_legacy_email_verification(db)
        db.execute(text("UPDATE users SET status = 'ACTIVE' WHERE status IS NULL OR status = ''"))
        db.execute(text("UPDATE users SET session_version = 0 WHERE session_version IS NULL"))
        db.execute(text("UPDATE users SET mfa_enabled = FALSE WHERE mfa_enabled IS NULL"))
        db.execute(text("UPDATE users SET plan = 'STARTER' WHERE plan IS NULL OR plan = ''"))
        db.execute(text("UPDATE users SET plan_max_brokers = 1 WHERE plan_max_brokers IS NULL"))
        db.execute(text("UPDATE users SET plan_max_leads = 100 WHERE plan_max_leads IS NULL"))
        db.execute(text("UPDATE users SET registered_at = CURRENT_TIMESTAMP WHERE registered_at IS NULL"))
        db.commit()

        startup_log("Sincronizando propriedades e ordens de servico.")
        for existing_lead in db.query(Lead).filter((Lead.property_id.is_(None)) | (Lead.property_id == "")).all():
            existing_lead.property_id = f"TS-{existing_lead.id:06d}"
        db.commit()

        for existing_lead in db.query(Lead).outerjoin(ServiceOrder, ServiceOrder.lead_id == Lead.id).filter(ServiceOrder.id.is_(None)).all():
            ensure_service_order(db, existing_lead)
        db.commit()

        startup_log("Criando indices operacionais.")
        try:
            if engine.dialect.name == "postgresql":
                db.execute(text("ALTER TABLE service_orders DROP CONSTRAINT IF EXISTS service_orders_lead_id_key"))
            db.execute(text("DROP INDEX IF EXISTS uq_service_orders_lead_id"))
            db.commit()
        except SQLAlchemyError as exc:
            db.rollback()
            logger.warning("Nao foi possivel remover indice unico legado de service_orders.lead_id: %s", exc)

        ensure_index(
            db,
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_lower ON users (LOWER(email)) WHERE email IS NOT NULL AND email <> ''",
            label="uq_users_email_lower",
        )
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_users_status ON users (status)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_users_status_changed_at ON users (status_changed_at)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_users_verification_token ON users (email_verification_token)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_users_verification_token_hash ON users (email_verification_token_hash)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_user_lifecycle_events_user ON user_lifecycle_events (user_id, created_at)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_user_reactivation_requests_status ON user_reactivation_requests (status, created_at)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_user_identities_user ON user_identities (user_id)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_user_sessions_user_active ON user_sessions (user_id, revoked_at, expires_at)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_auth_audit_events_user ON auth_audit_events (user_id, created_at)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_auth_rate_limits_scope_key ON auth_rate_limits (scope, key_hash)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_auth_rate_limits_expiry ON auth_rate_limits (blocked_until, updated_at)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_user ON password_reset_tokens (user_id, expires_at)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_mfa_recovery_codes_user ON mfa_recovery_codes (user_id, used_at)")

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
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_service_orders_lead_id ON service_orders (lead_id)")
        ensure_index(db, "CREATE UNIQUE INDEX IF NOT EXISTS uq_service_orders_order_number ON service_orders (order_number)")
        ensure_index(db, "CREATE UNIQUE INDEX IF NOT EXISTS uq_service_orders_service_request_id ON service_orders (service_request_id) WHERE service_request_id IS NOT NULL")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_service_orders_status ON service_orders (status)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_service_orders_property_id ON service_orders (property_id)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_service_orders_property_record_id ON service_orders (property_record_id)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_service_requests_status ON service_requests (status)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_service_requests_idempotency ON service_requests (organization_id, source, idempotency_key)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_service_request_media_request ON service_request_media (service_request_id)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_properties_lead ON properties (lead_id)")
        ensure_index(db, "CREATE INDEX IF NOT EXISTS idx_web_push_subscriptions_user_active ON web_push_subscriptions (user_id, active)")
        ensure_index(
            db,
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_leads_external_source_id ON leads (external_source, external_id) WHERE external_source IS NOT NULL AND external_source <> '' AND external_id IS NOT NULL AND external_id <> ''",
            label="uq_leads_external_source_id",
        )

        ensure_root_user(db, default_organization_id)
        startup_log("Preparacao do banco de dados concluida.")
        start_email_outbox_worker()
    finally:
        db.close()


def migrate_legacy_email_verification(db):
    legacy_active_verified = db.execute(
        text(
            """
            UPDATE users
            SET email_verified = TRUE
            WHERE email_verified IS NULL
              AND is_active IS TRUE
              AND COALESCE(NULLIF(status, ''), 'ACTIVE') = 'ACTIVE'
            """
        )
    ).rowcount
    legacy_non_active_pending = db.execute(
        text(
            """
            UPDATE users
            SET email_verified = FALSE
            WHERE email_verified IS NULL
              AND NOT (
                is_active IS TRUE
                AND COALESCE(NULLIF(status, ''), 'ACTIVE') = 'ACTIVE'
              )
            """
        )
    ).rowcount
    raw_tokens_cleared = db.execute(
        text("UPDATE users SET email_verification_token = NULL WHERE email_verification_token IS NOT NULL")
    ).rowcount
    startup_log(
        "Migracao de verificacao de email: "
        f"legacy_active_verified={legacy_active_verified or 0}; "
        f"legacy_pending_preserved={legacy_non_active_pending or 0}; "
        f"raw_tokens_cleared={raw_tokens_cleared or 0}."
    )


@app.get("/")
def home():
    frontend_index = frontend_dir / "index.html"
    if frontend_index.exists():
        return FileResponse(
            frontend_index,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
                "CDN-Cache-Control": "no-store",
            },
        )

    return {
        "status": "online",
        "sistema": "Total Solutions CRM",
        "mensagem": "CRM iniciado com sucesso"
    }


@app.get("/solicitar-servico", include_in_schema=False)
def service_portal():
    frontend_index = frontend_dir / "index.html"
    if frontend_index.exists():
        return FileResponse(
            frontend_index,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
                "CDN-Cache-Control": "no-store",
            },
        )
    return {"status": "portal-not-found"}


@app.get("/acompanhar/{tracking_token}", include_in_schema=False)
def service_request_tracking(tracking_token: str):
    frontend_index = frontend_dir / "index.html"
    if frontend_index.exists():
        return FileResponse(
            frontend_index,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
                "CDN-Cache-Control": "no-store",
            },
        )
    return {"status": "tracking-not-found", "tracking_token": tracking_token}


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    service_worker_file = frontend_dir / "sw.js"
    if service_worker_file.exists():
        return FileResponse(
            service_worker_file,
            media_type="application/javascript",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
                "CDN-Cache-Control": "no-store",
                "Service-Worker-Allowed": "/",
            },
        )

    return {"status": "service-worker-not-found"}


@app.get("/health")
def health():
    return {"status": "online"}
