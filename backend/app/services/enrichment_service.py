import ipaddress
import logging
import socket
from datetime import datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from app.database.connection import SessionLocal
from app.models.lead import Lead
from app.models.lead_event import LeadEvent

logger = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
REQUEST_TIMEOUT_SECONDS = 10
SOCIAL_DOMAINS = {
    "instagram": "instagram.com",
    "linkedin": "linkedin.com",
    "facebook": "facebook.com",
}


class EnrichmentError(Exception):
    pass


def normalize_site_url(site: str) -> str:
    value = site.strip()
    if not value:
        raise EnrichmentError("Lead sem site para enriquecimento")

    if "://" not in value:
        value = f"https://{value}"

    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise EnrichmentError("Site do lead possui URL invalida")
    if parsed.username or parsed.password:
        raise EnrichmentError("Site com credenciais na URL nao e permitido")
    if parsed.port and parsed.port not in {80, 443}:
        raise EnrichmentError("Porta do site nao permitida")

    return value


def ensure_public_url(url: str) -> None:
    parsed = urlsplit(url)
    hostname = parsed.hostname
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise EnrichmentError("URL de destino invalida")

    try:
        addresses = socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise EnrichmentError("Nao foi possivel localizar o site") from exc

    if not addresses:
        raise EnrichmentError("Site sem endereco de rede valido")

    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise EnrichmentError("Site aponta para uma rede privada ou reservada")


def _clean_social_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _social_network(url: str) -> str | None:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path.lower()

    for network, domain in SOCIAL_DOMAINS.items():
        if hostname == domain or hostname.endswith(f".{domain}"):
            if network == "facebook" and path.startswith(("/sharer", "/share", "/dialog", "/plugins")):
                return None
            if network == "linkedin" and path.startswith(("/sharing", "/sharearticle")):
                return None
            return network

    return None


def extract_social_links(html: str, base_url: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    results: dict[str, str] = {}
    candidates: list[str] = []

    for tag in soup.find_all("a", href=True):
        candidates.append(tag["href"])

    for tag in soup.find_all("meta", content=True):
        property_name = str(tag.get("property") or tag.get("name") or "").lower()
        content = str(tag.get("content") or "")
        if property_name.startswith("og:") or any(domain in content.lower() for domain in SOCIAL_DOMAINS.values()):
            candidates.append(content)

    for candidate in candidates:
        absolute_url = urljoin(base_url, candidate.strip())
        network = _social_network(absolute_url)
        if network and network not in results:
            results[network] = _clean_social_url(absolute_url)

    return results


def discover_social_links(site: str, client: httpx.Client | None = None) -> dict[str, str]:
    current_url = normalize_site_url(site)
    owns_client = client is None
    http_client = client or httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=False)

    try:
        for _ in range(MAX_REDIRECTS + 1):
            ensure_public_url(current_url)
            with http_client.stream(
                "GET",
                current_url,
                headers={
                    "User-Agent": "Total Solutions-Enrichment/1.0",
                    "Accept": "text/html,application/xhtml+xml",
                },
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise EnrichmentError("Redirecionamento do site sem destino")
                    current_url = urljoin(current_url, location)
                    continue

                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise EnrichmentError(f"Site respondeu com status {response.status_code}") from exc

                content_type = response.headers.get("content-type", "").lower()
                if "html" not in content_type:
                    raise EnrichmentError("Site nao retornou uma pagina HTML")

                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_RESPONSE_BYTES:
                        raise EnrichmentError("Pagina do site excede o limite de 2 MB")
                    chunks.append(chunk)

                encoding = response.encoding or "utf-8"
                html = b"".join(chunks).decode(encoding, errors="replace")
                return extract_social_links(html, str(response.url))

        raise EnrichmentError("Site excedeu o limite de redirecionamentos")
    except httpx.RequestError as exc:
        raise EnrichmentError("Nao foi possivel acessar o site") from exc
    finally:
        if owns_client:
            http_client.close()


def _event_message(found: dict[str, str]) -> str:
    if not found:
        return "Enriquecimento concluido sem redes sociais encontradas"

    labels = {
        "instagram": "Instagram",
        "linkedin": "LinkedIn",
        "facebook": "Facebook",
    }
    details = [f"{labels[key]}: {value}" for key, value in found.items()]
    return f"Enriquecimento encontrou {', '.join(details)}"


def enrich_lead_record(
    db: Session,
    lead: Lead,
    *,
    actor_id: int | None,
    actor_name: str,
    client: httpx.Client | None = None,
) -> dict[str, str]:
    found = discover_social_links(lead.site or "", client=client)

    for field in ("instagram", "linkedin", "facebook"):
        if field in found:
            setattr(lead, field, found[field])

    lead.updated_at = datetime.utcnow()
    db.add(
        LeadEvent(
            lead_id=lead.id,
            actor_id=actor_id,
            actor_name=actor_name,
            event_type="ENRIQUECIMENTO",
            message=_event_message(found),
        )
    )
    db.commit()
    db.refresh(lead)
    return found


def _record_failure(
    db: Session,
    lead: Lead,
    *,
    actor_id: int | None,
    actor_name: str,
    message: str,
) -> None:
    db.rollback()
    db.add(
        LeadEvent(
            lead_id=lead.id,
            actor_id=actor_id,
            actor_name=actor_name,
            event_type="ENRIQUECIMENTO",
            message=f"Enriquecimento nao concluido: {message}",
        )
    )
    db.commit()


def enrich_leads_in_background(lead_ids: list[int], actor_id: int, actor_name: str) -> None:
    db = SessionLocal()
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=False) as client:
            for lead_id in lead_ids:
                lead = db.query(Lead).filter(Lead.id == lead_id).first()
                if not lead:
                    continue

                try:
                    enrich_lead_record(
                        db,
                        lead,
                        actor_id=actor_id,
                        actor_name=actor_name,
                        client=client,
                    )
                except EnrichmentError as exc:
                    _record_failure(
                        db,
                        lead,
                        actor_id=actor_id,
                        actor_name=actor_name,
                        message=str(exc),
                    )
                except Exception:
                    logger.exception("Falha inesperada ao enriquecer lead %s", lead_id)
                    _record_failure(
                        db,
                        lead,
                        actor_id=actor_id,
                        actor_name=actor_name,
                        message="erro interno",
                    )
    finally:
        db.close()
