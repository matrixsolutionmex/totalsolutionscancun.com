import argparse
import sys
from collections import Counter
from pathlib import Path

from psycopg2.extras import execute_values
from sqlalchemy import or_

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from app.database.connection import SessionLocal
from app.models.lead import Lead
from app.services.geography_service import infer_geography


CONFIDENCE_RANK = {"none": 0, "medium": 1, "high": 2}


def main():
    parser = argparse.ArgumentParser(
        description="Preenche estado/cidade a partir do endereco. O padrao e apenas simulacao."
    )
    parser.add_argument("--apply", action="store_true", help="Confirma a gravacao no banco.")
    parser.add_argument("--limit", type=int, help="Limita a quantidade analisada.")
    parser.add_argument("--country", help="Analisa somente um pais, por exemplo BR ou MX.")
    parser.add_argument(
        "--minimum-confidence",
        choices=("medium", "high"),
        default="high",
        help="Confianca minima aceita. O padrao conservador e high.",
    )
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()

    analyzed = 0
    eligible = 0
    updated = 0
    by_country = Counter()
    by_state = Counter()
    by_city = Counter()
    by_strategy = Counter()
    last_id = 0

    with SessionLocal() as session:
        while True:
            remaining = None if args.limit is None else args.limit - analyzed
            if remaining is not None and remaining <= 0:
                break

            batch_limit = min(args.batch_size, remaining) if remaining else args.batch_size
            query = session.query(
                Lead.id,
                Lead.endereco,
                Lead.pais,
                Lead.estado,
                Lead.cidade,
            ).filter(
                Lead.id > last_id,
                or_(
                    Lead.estado.is_(None),
                    Lead.estado == "",
                    Lead.cidade.is_(None),
                    Lead.cidade == "",
                ),
            )
            if args.country:
                query = query.filter(Lead.pais == args.country)

            rows = query.order_by(Lead.id).limit(batch_limit).all()
            if not rows:
                break

            pending = []
            for lead in rows:
                analyzed += 1
                last_id = lead.id
                match = infer_geography(lead.endereco, lead.pais)
                if CONFIDENCE_RANK[match.confidence] < CONFIDENCE_RANK[args.minimum_confidence]:
                    continue
                if not match.estado and not match.cidade:
                    continue

                eligible += 1
                by_country[lead.pais or "SEM_PAIS"] += 1
                by_strategy[match.strategy] += 1
                if match.estado:
                    by_state[match.estado] += 1
                if match.cidade:
                    by_city[match.cidade] += 1

                if args.apply:
                    pending.append(
                        {
                            "lead_id": lead.id,
                            "estado": lead.estado or match.estado,
                            "cidade": lead.cidade or match.cidade,
                        }
                    )

            if args.apply and pending:
                dbapi_connection = session.connection().connection.driver_connection
                with dbapi_connection.cursor() as cursor:
                    execute_values(
                        cursor,
                        """
                        UPDATE leads AS target
                        SET
                            estado = COALESCE(NULLIF(target.estado, ''), source.estado),
                            cidade = COALESCE(NULLIF(target.cidade, ''), source.cidade)
                        FROM (VALUES %s) AS source(id, estado, cidade)
                        WHERE target.id = source.id
                        """,
                        [
                            (item["lead_id"], item["estado"], item["cidade"])
                            for item in pending
                        ],
                        page_size=len(pending),
                    )
                session.commit()
                updated += len(pending)

    mode = "APLICACAO" if args.apply else "SIMULACAO"
    print(f"Modo: {mode}")
    print(f"Leads analisados: {analyzed}")
    print(f"Leads com geografia identificada: {eligible}")
    print(f"Leads atualizados: {updated}")
    print(f"Confianca minima: {args.minimum_confidence}")
    print(f"Por pais: {dict(by_country.most_common())}")
    print(f"Estrategias: {dict(by_strategy.most_common())}")
    print(f"Top estados: {by_state.most_common(15)}")
    print(f"Top cidades: {by_city.most_common(20)}")


if __name__ == "__main__":
    main()
