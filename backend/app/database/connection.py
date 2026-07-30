import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import declarative_base, sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_DATABASE_URLS = {
    "",
    "postgresql://user@localhost/totalsolutions_crm",
    "postgresql://user:password@localhost:5432/totalsolutions_crm",
}

load_dotenv(ENV_FILE)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

if DATABASE_URL in DEFAULT_DATABASE_URLS:
    env_hint = (
        f"Arquivo esperado: {ENV_FILE}"
        if ENV_FILE.exists()
        else f"Crie {ENV_FILE} a partir de {PROJECT_ROOT / '.env.example'}"
    )
    raise RuntimeError(
        "DATABASE_URL nao configurada corretamente. "
        "O Total Solutions nao vai mais cair para localhost automaticamente. "
        f"{env_hint}."
    )

engine_options = {"pool_pre_ping": True}

database_driver = make_url(DATABASE_URL).get_backend_name()
if database_driver == "postgresql":
    engine_options["connect_args"] = {"connect_timeout": 10}

engine = create_engine(DATABASE_URL, **engine_options)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

Base = declarative_base()
