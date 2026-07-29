import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT_DIR.parent
sys.path.insert(0, str(ROOT_DIR))

from app.database.connection import SessionLocal
from app.models.lead import Lead
from app.services.import_service import (
    clean_text,
    normalize_domain,
    normalize_email,
    normalize_name,
    normalize_phone,
    normalize_score,
    prepare_lead_mapping,
)

DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "exports"


def normalize_text(value):
    text = clean_text(value)
    return text.casefold() if text else None


def preferred_identity_key(record):
    email = normalize_email(record.get("email"))
    phone = normalize_phone(record.get("contato"))
    domain = normalize_domain(record.get("site"))
    name = normalize_name(record.get("nome"))

    if email:
        return ("email", email)
    if phone:
        return ("phone", phone)
    if domain and name:
        return ("domain_name", domain, name)
    if domain:
        return ("domain", domain)
    return None


def exact_signature(record):
    return (
        normalize_name(record.get("nome")),
        normalize_email(record.get("email")),
        normalize_phone(record.get("contato")),
        normalize_domain(record.get("site")),
        normalize_text(record.get("endereco")),
    )


def build_matrix_records(source_db: Path):
    source_db = Path(source_db)
    matrix_uri = f"file:{source_db.as_posix()}?mode=ro&immutable=1"
    with sqlite3.connect(matrix_uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        segment_index = {}
        for row in conn.execute(
            """
            SELECT
                nome,
                contato,
                email,
                site,
                endereco,
                nicho,
                pais,
                score
            FROM dados_segmentados
            """
        ):
            key = (
                row["nome"] or "",
                row["contato"] or "",
                row["email"] or "",
                row["site"] or "",
                row["endereco"] or "",
            )
            segment_index[key] = {
                "nicho": row["nicho"],
                "pais": row["pais"],
                "score": row["score"],
            }

        records = []
        for row in conn.execute(
            """
            SELECT
                id AS matrix_id,
                nome,
                contato,
                email,
                site,
                endereco
            FROM dados_full
            """
        ):
            key = (
                row["nome"] or "",
                row["contato"] or "",
                row["email"] or "",
                row["site"] or "",
                row["endereco"] or "",
            )
            segment = segment_index.get(key, {})
            records.append(
                {
                    "matrix_id": row["matrix_id"],
                    "nome": row["nome"],
                    "contato": row["contato"],
                    "email": row["email"],
                    "site": row["site"],
                    "endereco": row["endereco"],
                    "nicho": segment.get("nicho"),
                    "pais": segment.get("pais"),
                    "score": segment.get("score"),
                }
            )

        return records


def load_crm_snapshot():
    with SessionLocal() as session:
        leads = (
            session.query(
                Lead.id,
                Lead.nome,
                Lead.contato,
                Lead.email,
                Lead.site,
                Lead.endereco,
                Lead.nicho,
                Lead.pais,
                Lead.score,
            )
            .all()
        )

    leads_by_id = {}
    email_index = defaultdict(set)
    phone_index = defaultdict(set)
    domain_name_index = defaultdict(set)
    domain_index = defaultdict(set)
    signature_index = defaultdict(set)

    for lead in leads:
        payload = {
            "id": lead.id,
            "nome": lead.nome,
            "contato": lead.contato,
            "email": lead.email,
            "site": lead.site,
            "endereco": lead.endereco,
            "nicho": lead.nicho,
            "pais": lead.pais,
            "score": lead.score,
        }
        leads_by_id[lead.id] = payload

        email = normalize_email(lead.email)
        phone = normalize_phone(lead.contato)
        domain = normalize_domain(lead.site)
        name = normalize_name(lead.nome)
        signature_index[exact_signature(payload)].add(lead.id)

        if email:
            email_index[email].add(lead.id)
        if phone:
            phone_index[phone].add(lead.id)
        if domain and name:
            domain_name_index[(domain, name)].add(lead.id)
        if domain:
            domain_index[domain].add(lead.id)

    return {
        "leads_by_id": leads_by_id,
        "email_index": email_index,
        "phone_index": phone_index,
        "domain_name_index": domain_name_index,
        "domain_index": domain_index,
        "signature_index": signature_index,
    }


def find_crm_match(record, snapshot):
    email = normalize_email(record.get("email"))
    phone = normalize_phone(record.get("contato"))
    domain = normalize_domain(record.get("site"))
    name = normalize_name(record.get("nome"))
    signature = exact_signature(record)

    candidates = []
    if email:
        candidates.append(("email", email, snapshot["email_index"].get(email, set())))
    if phone:
        candidates.append(("phone", phone, snapshot["phone_index"].get(phone, set())))
    if domain and name:
        key = (domain, name)
        candidates.append(("domain_name", key, snapshot["domain_name_index"].get(key, set())))
    if domain:
        candidates.append(("domain", domain, snapshot["domain_index"].get(domain, set())))

    for key_type, key_value, ids in candidates:
        if len(ids) == 1:
            return {"status": "matched", "lead_id": next(iter(ids)), "match_key": key_type}
        if len(ids) > 1:
            return {
                "status": "ambiguous",
                "match_key": key_type,
                "matched_ids": sorted(ids),
            }

    exact_ids = snapshot["signature_index"].get(signature, set())
    if len(exact_ids) == 1:
        return {"status": "matched", "lead_id": next(iter(exact_ids)), "match_key": "exact_signature"}
    if len(exact_ids) > 1:
        return {
            "status": "ambiguous",
            "match_key": "exact_signature",
            "matched_ids": sorted(exact_ids),
        }

    return {"status": "new"}


def compare_value(field, crm_value, matrix_value):
    if field == "contato":
        return normalize_phone(crm_value), normalize_phone(matrix_value)
    if field == "email":
        return normalize_email(crm_value), normalize_email(matrix_value)
    if field == "site":
        return normalize_domain(crm_value), normalize_domain(matrix_value)
    if field == "nome":
        return normalize_name(crm_value), normalize_name(matrix_value)
    if field in {"endereco", "nicho", "pais"}:
        return normalize_text(crm_value), normalize_text(matrix_value)
    if field == "score":
        return normalize_score(crm_value), normalize_score(matrix_value)
    return clean_text(crm_value), clean_text(matrix_value)


def build_update_payload(crm_lead, matrix_record):
    tracked_fields = ("nome", "contato", "email", "site", "endereco", "nicho", "pais", "score")
    changes = {}
    payload = {}

    for field in tracked_fields:
        crm_raw = crm_lead.get(field)
        matrix_raw = matrix_record.get(field)
        crm_norm, matrix_norm = compare_value(field, crm_raw, matrix_raw)

        if matrix_norm in (None, ""):
            continue
        if crm_norm in (None, ""):
            mode = "fill"
        elif crm_norm != matrix_norm:
            mode = "replace"
        else:
            continue

        payload[field] = clean_text(matrix_raw) if field != "score" else normalize_score(matrix_raw)
        changes[field] = {
            "mode": mode,
            "from": crm_raw,
            "to": payload[field],
        }

    return payload, changes


def write_json(path: Path, data):
    def default_serializer(value):
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, datetime):
            return value.isoformat()
        raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")

    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=default_serializer),
        encoding="utf-8",
    )


def analyze_incremental(source_db: Path, output_root: Path):
    matrix_records = build_matrix_records(source_db)
    snapshot = load_crm_snapshot()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = output_root / f"incremental_matrix_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    seen_matrix_keys = {}
    seen_matrix_signatures = {}

    inserts = []
    updates = []
    duplicates = []
    existing_unchanged = []
    invalid = []
    match_key_counter = Counter()
    update_field_counter = Counter()

    for record in matrix_records:
        display_record = {
            "matrix_id": record.get("matrix_id"),
            "nome": clean_text(record.get("nome")),
            "contato": clean_text(record.get("contato")),
            "email": normalize_email(record.get("email")),
            "site": clean_text(record.get("site")),
            "endereco": clean_text(record.get("endereco")),
            "nicho": clean_text(record.get("nicho")),
            "pais": clean_text(record.get("pais")),
            "score": normalize_score(record.get("score")),
        }

        if not any(display_record.get(field) for field in ("nome", "contato", "email", "site", "endereco")):
            invalid.append(display_record)
            continue

        identity_key = preferred_identity_key(display_record)
        signature = exact_signature(display_record)

        if identity_key:
            if identity_key in seen_matrix_keys:
                duplicates.append(
                    {
                        "reason": "duplicate_in_matrix",
                        "key": list(identity_key),
                        "first_matrix_id": seen_matrix_keys[identity_key],
                        "record": display_record,
                    }
                )
                continue
            seen_matrix_keys[identity_key] = display_record["matrix_id"]
        elif signature in seen_matrix_signatures:
            duplicates.append(
                {
                    "reason": "duplicate_in_matrix_signature",
                    "first_matrix_id": seen_matrix_signatures[signature],
                    "record": display_record,
                }
            )
            continue

        seen_matrix_signatures[signature] = display_record["matrix_id"]
        match = find_crm_match(display_record, snapshot)

        if match["status"] == "ambiguous":
            duplicates.append(
                {
                    "reason": "ambiguous_crm_match",
                    "match_key": match["match_key"],
                    "matched_ids": match["matched_ids"],
                    "record": display_record,
                }
            )
            continue

        if match["status"] == "new":
            inserts.append(prepare_lead_mapping(display_record))
            continue

        crm_lead = snapshot["leads_by_id"][match["lead_id"]]
        match_key_counter[match["match_key"]] += 1
        update_payload, changes = build_update_payload(crm_lead, display_record)
        if changes:
            for field in changes:
                update_field_counter[field] += 1
            updates.append(
                {
                    "crm_lead_id": crm_lead["id"],
                    "match_key": match["match_key"],
                    "matrix_record": display_record,
                    "crm_snapshot": crm_lead,
                    "update_payload": update_payload,
                    "changed_fields": changes,
                }
            )
        else:
            existing_unchanged.append(
                {
                    "crm_lead_id": crm_lead["id"],
                    "match_key": match["match_key"],
                    "record": display_record,
                }
            )

    summary = {
        "generated_at": datetime.now().isoformat(),
        "source_db": str(source_db),
        "matrix_total": len(matrix_records),
        "crm_total": len(snapshot["leads_by_id"]),
        "already_in_crm": len(existing_unchanged) + len(updates),
        "already_in_crm_unchanged": len(existing_unchanged),
        "needs_update": len(updates),
        "new_records": len(inserts),
        "duplicates": len(duplicates),
        "invalid_records": len(invalid),
        "match_keys": dict(match_key_counter),
        "update_fields": dict(update_field_counter),
    }

    batch = {
        "generated_at": summary["generated_at"],
        "source_db": summary["source_db"],
        "summary": summary,
        "inserts": inserts,
        "updates": updates,
    }

    write_json(output_dir / "summary.json", summary)
    write_json(output_dir / "incremental_batch.json", batch)
    write_json(output_dir / "new_records.json", inserts)
    write_json(output_dir / "update_candidates.json", updates)
    write_json(output_dir / "duplicates_review.json", duplicates)
    write_json(output_dir / "existing_unchanged_sample.json", existing_unchanged[:500])
    write_json(output_dir / "invalid_records.json", invalid)

    return summary, output_dir


def main():
    parser = argparse.ArgumentParser(
        description="Compara um SQLite informado explicitamente com o CRM e gera um lote incremental seguro."
    )
    parser.add_argument(
        "--source-db",
        required=True,
        type=Path,
        help="Caminho para o SQLite de origem. Nao ha origem padrao para evitar compartilhamento acidental.",
    )
    parser.add_argument(
        "--output-root",
        default=DEFAULT_OUTPUT_ROOT,
        type=Path,
        help="Diretório raiz onde o lote incremental será gerado.",
    )
    args = parser.parse_args()

    if not args.source_db.exists():
        raise FileNotFoundError(f"Banco de origem não encontrado: {args.source_db}")

    summary, output_dir = analyze_incremental(args.source_db, args.output_root)

    print(f"Matrix total: {summary['matrix_total']}")
    print(f"CRM total: {summary['crm_total']}")
    print(f"Já existem no CRM: {summary['already_in_crm']}")
    print(f"Atualizações necessárias: {summary['needs_update']}")
    print(f"Novos registros: {summary['new_records']}")
    print(f"Duplicados / ambíguos: {summary['duplicates']}")
    print(f"Inválidos: {summary['invalid_records']}")
    print(f"Lote incremental salvo em: {output_dir}")


if __name__ == "__main__":
    main()
