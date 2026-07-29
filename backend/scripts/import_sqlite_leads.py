import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from app.database.connection import Base, engine, SessionLocal
from app.models.lead import Lead
from app.models.user import User
from app.services.import_service import import_lead_records

BATCH_SIZE = 1000


def iter_source_leads(source_db):
    base_query = """
        SELECT
            nome,
            contato,
            email,
            site,
            endereco,
            nicho,
            pais,
            {estado},
            {cidade},
            score
        FROM dados_segmentados
        WHERE COALESCE(TRIM(nome), '') <> ''
           OR COALESCE(TRIM(email), '') <> ''
           OR COALESCE(TRIM(site), '') <> ''
    """

    with sqlite3.connect(source_db) as conn:
        conn.row_factory = sqlite3.Row
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(dados_segmentados)")
        }
        query = base_query.format(
            estado="estado" if "estado" in columns else "NULL AS estado",
            cidade="cidade" if "cidade" in columns else "NULL AS cidade",
        )
        for row in conn.execute(query):
            now = datetime.utcnow()
            yield {
                "nome": row["nome"],
                "contato": row["contato"],
                "email": row["email"],
                "site": row["site"],
                "endereco": row["endereco"],
                "nicho": row["nicho"],
                "pais": row["pais"],
                "estado": row["estado"],
                "cidade": row["cidade"],
                "score": row["score"],
                "pipeline": "NOVO LEAD",
                "pipeline_updated_at": now,
                "created_at": now,
                "updated_at": now,
            }


def import_leads(source_db):
    Base.metadata.create_all(bind=engine)

    records = list(iter_source_leads(source_db))
    total_read = len(records)

    with SessionLocal() as session:
        stats = import_lead_records(session, records, batch_size=BATCH_SIZE)

    return total_read, stats.inserted


def main():
    parser = argparse.ArgumentParser(
        description="Importa leads de um SQLite informado explicitamente para o PostgreSQL do Total Solutions CRM."
    )
    parser.add_argument(
        "--source-db",
        required=True,
        type=Path,
        help="Caminho para o SQLite de origem. Nao ha origem padrao para evitar compartilhamento acidental.",
    )
    args = parser.parse_args()

    if not args.source_db.exists():
        raise FileNotFoundError(f"Banco de origem nao encontrado: {args.source_db}")

    total_read, total_inserted = import_leads(args.source_db)
    print(f"Lidos do Matrix: {total_read}")
    print(f"Inseridos no CRM: {total_inserted}")


if __name__ == "__main__":
    main()
