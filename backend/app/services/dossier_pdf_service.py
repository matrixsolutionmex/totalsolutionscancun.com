from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from textwrap import wrap

from app.models.lead import Lead
from app.models.lead_document import LeadDocument
from app.models.lead_event import LeadEvent
from app.models.service_order import ServiceOrder
from app.models.user import User


PROPERTY_TYPE_LABELS = {
    "CASA": "Casa",
    "APARTAMENTO": "Apartamento",
    "HOTEL": "Hotel",
    "AIRBNB": "Airbnb",
    "LOJA": "Loja",
    "INDUSTRIA": "Industria",
    "ESCOLA": "Escola",
    "CLINICA": "Clinica",
    "ESCRITORIO": "Escritorio",
    "CONDOMINIO": "Condominio",
    "OBRA": "Obra",
    "OUTRO": "Outro",
}

SERVICE_TYPE_LABELS = {
    "HIDRAULICA": "Hidraulica",
    "ELETRICA": "Eletrica",
    "AR_CONDICIONADO": "Ar condicionado",
    "PINTURA": "Pintura",
    "IMPERMEABILIZACAO": "Impermeabilizacao",
    "CISTERNA": "Cisterna",
    "MARCENARIA": "Marcenaria",
    "ALVENARIA": "Alvenaria",
    "LIMPEZA_TECNICA": "Limpeza tecnica",
    "OUTRO": "Outro",
}

DOCUMENT_TYPE_LABELS = {
    "ANTES_SERVICIO": "Antes do servico",
    "DURANTE_SERVICIO": "Durante o servico",
    "DESPUES_SERVICIO": "Depois do servico",
    "ORCAMENTO": "Orcamento",
    "NOTA_FISCAL": "Nota fiscal",
    "GARANTIA": "Garantia",
    "CONTRATO": "Contrato",
    "VIDEO": "Videos",
    "OUTRO": "Outros",
}

STATUS_LABELS = {
    "NOVO LEAD": "Nuevo contacto",
    "ATENDIMENTO": "Atencion inicial",
    "TENTATIVA DE CONTATO": "Visita agendada",
    "VISITA": "Diagnostico",
    "MONTAGEM DE PASTA": "Cotizacion enviada",
    "VENDA GANHA": "Servicio aprobado",
    "PERDIDO": "Cancelado",
}

ORIGIN_LABELS = {
    "WHATSAPP": "WhatsApp",
    "GOOGLE_BUSINESS": "Google Business",
    "GOOGLE_ADS": "Google Ads",
    "META_ADS": "Meta Ads",
    "INSTAGRAM": "Instagram",
    "FACEBOOK": "Facebook",
    "LANDING_PAGE": "Landing Page",
    "LLAMADA": "Llamada",
    "INDICACION": "Indicacion",
    "CLIENTE_ANTIGUO": "Cliente antiguo",
    "PROSPECCION": "Prospeccion",
    "LEADVAULT": "Importacion anterior",
    "SOCIO_COMERCIAL": "Socio comercial",
    "OTRO": "Otro",
}

URGENCY_LABELS = {
    "BAJA": "Baja",
    "NORMAL": "Normal",
    "ALTA": "Alta",
    "EMERGENCIA": "Emergencia",
}

IMPORTANT_EVENT_TYPES = {"ENTRADA", "DOCUMENTO", "ATRIBUICAO", "PIPELINE", "BANCO", "NOTA"}

OS_STATUS_LABELS = {
    "ABERTA": "Aberta",
    "EM_ATENDIMENTO": "Em atendimento",
    "AGENDADA": "Agendada",
    "EM_DIAGNOSTICO": "Em diagnostico",
    "COTIZACAO_ENVIADA": "Cotizacion enviada",
    "APROVADA": "Servicio aprobado",
    "CANCELADA": "Cancelada",
}


def _value(value):
    if value is None or value == "":
        return "No informado"
    if isinstance(value, Decimal):
        return f"MX$ {float(value):,.2f}"
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y %H:%M")
    return str(value)


def _user_name(user: User | None):
    if not user:
        return "Sin asignar"
    return user.full_name or user.username or f"ID {user.id}"


def _pdf_text(value):
    text = str(value or "")
    text = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return text.encode("latin-1", "replace").decode("latin-1")


def _service_number(lead: Lead, service_order: ServiceOrder | None = None):
    if service_order and service_order.order_number:
        return service_order.order_number
    year = (lead.created_at or datetime.utcnow()).year
    return f"TS-{year}-{lead.id:06d}"


def _label(mapping, value):
    return mapping.get(str(value or "").upper(), _value(value))


def _file_size(size):
    size = int(size or 0)
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{max(1, round(size / 1024))} KB" if size else "0 KB"


class SimplePdf:
    width = 612
    height = 792
    margin = 42
    bottom_margin = 54

    def __init__(self):
        self.pages: list[list[tuple[float, float, str, int, bool]]] = [[]]
        self.y = self.height - self.margin - 34

    def add_page(self):
        if self.pages[-1]:
            self.pages.append([])
        self.y = self.height - self.margin - 34

    def ensure_space(self, points=18):
        if self.y - points < self.bottom_margin:
            self.add_page()

    def wrap_text(self, text, width=88):
        value = _value(text)
        lines: list[str] = []
        for paragraph in str(value).splitlines() or [""]:
            lines.extend(wrap(paragraph, width=width) or [""])
        return lines

    def add_line(self, text="", size=10, bold=False):
        line_height = max(12, int(size * 1.45))
        self.ensure_space(line_height)
        self.pages[-1].append((self.margin, self.y, text, size, bold))
        self.y -= line_height

    def add_title(self, title, subtitle=None):
        self.ensure_space(86)
        self.pages[-1].append((self.margin, self.y, title, 22, True))
        self.y -= 30
        if subtitle:
            self.pages[-1].append((self.margin, self.y, subtitle, 17, True))
            self.y -= 26
        self.add_line("", 8)

    def add_section_title(self, title):
        self.ensure_space(46)
        self.y -= 6
        self.pages[-1].append((self.margin, self.y, title.upper(), 13, True))
        self.y -= 18
        self.pages[-1].append((self.margin, self.y, "-" * 82, 9, False))
        self.y -= 14

    def add_section(self, title):
        self.add_section_title(title)

    def add_field(self, label, value):
        for line in self.wrap_text(f"{label}: {_value(value)}", width=92):
            self.add_line(line, 10)

    def add_pair(self, label, value):
        self.add_field(label, value)

    def add_paragraph(self, text):
        for line in self.wrap_text(text, width=92):
            self.add_line(line, 10)

    def build(self) -> bytes:
        self.pages = [page for page in self.pages if page]
        if not self.pages:
            self.add_line("No informado")

        objects: list[bytes] = []
        page_object_ids = []

        objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
        objects.append(b"")
        objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
        objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")

        next_id = 5
        total_pages = len(self.pages)
        for page_number, page in enumerate(self.pages, start=1):
            content_id = next_id
            page_id = next_id + 1
            next_id += 2
            page_object_ids.append(page_id)

            stream_lines = []
            header = [
                (self.margin, self.height - 28, "TOTAL SOLUTIONS", 9, True),
                (self.width - 116, 24, f"Pagina {page_number} de {total_pages}", 8, False),
            ]
            for x, y, text, size, bold in header + page:
                font = "F2" if bold else "F1"
                stream_lines.append(f"BT /{font} {size} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm ({_pdf_text(text)}) Tj ET")
            stream = "\n".join(stream_lines).encode("latin-1", "replace")
            objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
            objects.append(
                (
                    f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {content_id} 0 R >>"
                ).encode("latin-1")
            )

        kids = " ".join(f"{page_id} 0 R" for page_id in page_object_ids)
        objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_object_ids)} >>".encode("latin-1")

        pdf = bytearray(b"%PDF-1.4\n")
        offsets = [0]
        for index, obj in enumerate(objects, start=1):
            offsets.append(len(pdf))
            pdf.extend(f"{index} 0 obj\n".encode("latin-1"))
            pdf.extend(obj)
            pdf.extend(b"\nendobj\n")
        xref = len(pdf)
        pdf.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("latin-1"))
        for offset in offsets[1:]:
            pdf.extend(f"{offset:010d} 00000 n \n".encode("latin-1"))
        pdf.extend(
            (
                f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
                f"startxref\n{xref}\n%%EOF\n"
            ).encode("latin-1")
        )
        return bytes(pdf)


def build_service_dossier_pdf(
    *,
    lead: Lead,
    events: list[LeadEvent],
    documents: list[LeadDocument],
    responsible: User | None = None,
    supervisor: User | None = None,
    administrator: User | None = None,
    uploaders: dict[int, str] | None = None,
    service_order: ServiceOrder | None = None,
    public_base_url: str = "",
) -> bytes:
    pdf = SimplePdf()
    os_number = _service_number(lead, service_order)
    service = SERVICE_TYPE_LABELS.get(lead.tipo_servico or "", lead.nicho or lead.tipo_servico or "-")
    property_type = PROPERTY_TYPE_LABELS.get(lead.tipo_imovel or "", lead.tipo_imovel or "-")
    status = _label(STATUS_LABELS, lead.pipeline)
    os_status = _label(OS_STATUS_LABELS, service_order.status if service_order else None)
    origin = _label(ORIGIN_LABELS, lead.origen)
    urgency = _label(URGENCY_LABELS, lead.urgencia)
    warranty = f"{service_order.warranty_days} dias" if service_order else "90 dias"

    pdf.add_title("TOTAL SOLUTIONS", "DOSSIÊ DO SERVIÇO")
    pdf.add_line("Service Management Platform", 11)
    pdf.add_line("")
    pdf.add_field("OS", os_number)
    pdf.add_field("ID interno", lead.id)
    pdf.add_field("Cliente", lead.nome)
    pdf.add_field("Tipo de imovel", property_type)
    pdf.add_field("Servico", service)
    pdf.add_field("Status", os_status if service_order else status)
    pdf.add_field("Responsavel", _user_name(responsible))
    pdf.add_field("Garantia", warranty)
    pdf.add_field("Data de abertura", service_order.opened_at if service_order else lead.created_at)

    pdf.add_section("Ordem de Servico")
    pdf.add_field("Numero", os_number)
    pdf.add_field("ID interno", lead.id)
    pdf.add_field("Status operacional", os_status if service_order else status)
    pdf.add_field("Data de abertura", service_order.opened_at if service_order else lead.created_at)
    pdf.add_field("Data agendada", service_order.scheduled_at if service_order else lead.proximo_contacto)
    pdf.add_field("Data de conclusao", service_order.completed_at if service_order else None)
    pdf.add_field("Ultima atualizacao", service_order.updated_at if service_order else lead.updated_at)
    pdf.add_field("Garantia", warranty)
    pdf.add_field("Assinaturas digitais", service_order.signature_status if service_order else "PENDENTE")
    pdf.add_field("QR Code", "Preparado para futura ativacao")
    pdf.add_field("Selo de garantia", service_order.warranty_seal_status if service_order else "PENDENTE")
    pdf.add_field("Checklist tecnico", service_order.checklist_status if service_order else "PENDENTE")

    pdf.add_section("Dados do cliente")
    pdf.add_field("Nome", lead.nome)
    pdf.add_field("Contato", lead.contato)
    pdf.add_field("WhatsApp", lead.whatsapp)
    pdf.add_field("Email", lead.email)
    pdf.add_field("Empresa", lead.empresa)
    pdf.add_field("Site", lead.site)
    pdf.add_field("Instagram", lead.instagram)
    pdf.add_field("Facebook", lead.facebook)
    pdf.add_field("LinkedIn", lead.linkedin)

    pdf.add_section("Dados do imovel")
    pdf.add_field("Tipo de imovel", property_type)
    pdf.add_field("Pais", lead.pais)
    pdf.add_field("Estado", lead.estado)
    pdf.add_field("Cidade", lead.cidade)
    pdf.add_field("Colonia", lead.colonia)
    pdf.add_field("Codigo postal", lead.codigo_postal)
    pdf.add_field("Endereco", lead.endereco)
    pdf.add_field("Google Maps", lead.google_maps_url)

    pdf.add_section("Dados da solicitacao")
    pdf.add_field("Servico solicitado", service)
    pdf.add_field("Descricao do problema", lead.descripcion_problema)
    pdf.add_field("Urgencia", urgency)
    pdf.add_field("Origem", origin)
    pdf.add_field("Detalhe da origem", lead.origen_detalle)
    pdf.add_field("Proximo contato", lead.proximo_contacto)
    pdf.add_field("Observacoes", lead.observacoes)
    pdf.add_field("Valor do servico", lead.valor_negocio)

    pdf.add_section("Responsaveis")
    pdf.add_field("Administrador", _user_name(administrator))
    pdf.add_field("Supervisor", _user_name(supervisor))
    pdf.add_field("Tecnico responsavel", _user_name(responsible))

    pdf.add_section("Linha do tempo")
    important_events = [event for event in events if (event.event_type or "").upper() in IMPORTANT_EVENT_TYPES]
    if important_events:
        current_date = None
        for event in important_events:
            date = event.created_at.strftime("%d/%m/%Y") if event.created_at else "No informado"
            hour = event.created_at.strftime("%H:%M") if event.created_at else "No informado"
            if date != current_date:
                current_date = date
                pdf.add_line(date, 11, True)
            pdf.add_field(hour, f"{event.actor_name or 'Sistema'} | {event.event_type} | {event.message}")
    else:
        pdf.add_line("Sin registros")

    pdf.add_section("Evidencias")
    if documents:
        grouped: dict[str, list[LeadDocument]] = {}
        for doc in documents:
            grouped.setdefault(doc.document_type, []).append(doc)
        for doc_type, items in grouped.items():
            pdf.add_line(DOCUMENT_TYPE_LABELS.get(doc_type, doc_type), 11, True)
            for doc in items:
                uploader = uploaders.get(doc.uploaded_by_user_id, "Sin asignar") if uploaders else "Sin asignar"
                pdf.add_field("Nome original", doc.file_name)
                pdf.add_field("Categoria", DOCUMENT_TYPE_LABELS.get(doc.document_type, doc.document_type))
                pdf.add_field("Data/hora", doc.created_at)
                pdf.add_field("Tamanho", _file_size(doc.file_size))
                pdf.add_field("Enviado por", uploader)
                pdf.add_field("Referencia", "Anexo registrado no sistema")
                pdf.add_line("")
    else:
        pdf.add_line("Sin registros")

    pdf.add_section("Informacoes tecnicas")
    pdf.add_line("Materiais utilizados: No informado")
    pdf.add_line("")
    pdf.add_line("Diagnostico: No informado")

    pdf.add_section("Garantia")
    pdf.add_field("Garantia", warranty)
    pdf.add_field("Inicio", service_order.completed_at if service_order else None)
    pdf.add_field("Fim", "Calculado no fechamento da OS")

    pdf.add_page()
    pdf.add_title("RELATÓRIO FINAL")
    pdf.add_field("Cliente", lead.nome)
    pdf.add_field("Servico", service)
    pdf.add_field("Resultado", "Servico executado" if lead.pipeline == "VENDA GANHA" else status)
    pdf.add_field("Tempo gasto", "No informado")
    pdf.add_field("Materiais", "No informado")
    pdf.add_field("Garantia", warranty)
    pdf.add_field("Tecnico", _user_name(responsible))
    pdf.add_field("Supervisor", _user_name(supervisor))
    pdf.add_line("")

    pdf.add_section("Assinaturas")
    pdf.add_line("Cliente:     _________________________________")
    pdf.add_line("")
    pdf.add_line("Tecnico:     _________________________________")
    pdf.add_line("")
    pdf.add_line("Supervisor:  _________________________________")
    pdf.add_line("")
    pdf.add_line(f"QR/Link online da OS: {public_base_url}/leads/{lead.id}" if public_base_url else f"OS online: /leads/{lead.id}")

    return pdf.build()
