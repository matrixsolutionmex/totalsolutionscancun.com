from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.models.lead import Lead
from app.services.geography_service import infer_geography

BATCH_SIZE = 1000


@dataclass
class ImportStats:
    total_received: int = 0
    inserted: int = 0
    skipped_duplicates: int = 0
    invalid: int = 0


def clean_text(value):
    if value is None:
        return None

    text = str(value).strip()
    return text or None


def normalize_score(value):
    if value in (None, ""):
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_money(value):
    if value in (None, ""):
        return Decimal("0")

    try:
        return Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation):
        return Decimal("0")


def normalize_phone(value):
    text = clean_text(value)
    if not text:
        return None

    digits = "".join(char for char in text if char.isdigit())
    return digits or None


def normalize_email(value):
    text = clean_text(value)
    return text.lower() if text else None


def normalize_name(value):
    text = clean_text(value)
    return text.casefold() if text else None


def normalize_domain(value):
    text = clean_text(value)
    if not text:
        return None

    candidate = text if "://" in text else f"https://{text}"
    parsed = urlparse(candidate)
    domain = parsed.netloc or parsed.path
    domain = domain.lower().strip().strip("/")
    if domain.startswith("www."):
        domain = domain[4:]
    return domain or None


def lead_fingerprints(lead):
    email = normalize_email(lead.get("email"))
    phone = normalize_phone(lead.get("contato"))
    domain = normalize_domain(lead.get("site"))
    name = normalize_name(lead.get("nome"))

    fingerprints = set()

    if email:
        fingerprints.add(("email", email))
    if phone:
        fingerprints.add(("phone", phone))
    if domain and name:
        fingerprints.add(("domain_name", domain, name))
    elif domain:
        fingerprints.add(("domain", domain))

    return fingerprints


def load_existing_fingerprints(session: Session):
    fingerprints = set()
    for nome, contato, email, site in session.query(Lead.nome, Lead.contato, Lead.email, Lead.site).all():
        fingerprints.update(
            lead_fingerprints(
                {
                    "nome": nome,
                    "contato": contato,
                    "email": email,
                    "site": site,
                }
            )
        )
    return fingerprints


def prepare_lead_mapping(record):
    now = datetime.utcnow()
    endereco = clean_text(record.get("endereco"))
    pais = clean_text(record.get("pais"))
    estado = clean_text(record.get("estado") or record.get("state"))
    cidade = clean_text(record.get("cidade") or record.get("city"))

    if not estado or not cidade:
        geography = infer_geography(endereco, pais)
        estado = estado or geography.estado
        cidade = cidade or geography.cidade

    lead = {
        "nome": clean_text(record.get("nome")),
        "contato": clean_text(record.get("contato")),
        "email": normalize_email(record.get("email")),
        "site": clean_text(record.get("site")),
        "endereco": endereco,
        "instagram": clean_text(record.get("instagram")),
        "linkedin": clean_text(record.get("linkedin")),
        "facebook": clean_text(record.get("facebook")),
        "redes_sociais": clean_text(record.get("redes_sociais")),
        "observacoes": clean_text(record.get("observacoes")),
        "nicho": clean_text(record.get("nicho")),
        "pais": pais,
        "estado": estado,
        "cidade": cidade,
        "score": normalize_score(record.get("score")),
        "valor_negocio": normalize_money(record.get("valor_negocio")),
        "pipeline": "NOVO LEAD",
        "pipeline_updated_at": now,
        "created_at": now,
        "updated_at": now,
    }
    return lead


def is_valid_lead_payload(lead):
    return any(
        lead.get(field)
        for field in ("nome", "contato", "email", "site", "endereco")
    )


def flush_batch(session: Session, batch):
    if not batch:
        return 0

    session.bulk_insert_mappings(Lead, batch)
    session.commit()
    return len(batch)


def import_lead_records(session: Session, records, batch_size: int = BATCH_SIZE):
    stats = ImportStats(total_received=len(records))
    existing_fingerprints = load_existing_fingerprints(session)
    staged_fingerprints = set()
    batch = []

    for raw_record in records:
        lead = prepare_lead_mapping(raw_record)
        if not is_valid_lead_payload(lead):
            stats.invalid += 1
            continue

        fingerprints = lead_fingerprints(lead)
        if fingerprints and (
            fingerprints & existing_fingerprints or fingerprints & staged_fingerprints
        ):
            stats.skipped_duplicates += 1
            continue

        staged_fingerprints.update(fingerprints)
        existing_fingerprints.update(fingerprints)
        batch.append(lead)

        if len(batch) >= batch_size:
            stats.inserted += flush_batch(session, batch)
            batch.clear()

    stats.inserted += flush_batch(session, batch)
    return stats
