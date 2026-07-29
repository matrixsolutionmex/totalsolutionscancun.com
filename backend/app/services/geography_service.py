import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class GeographyMatch:
    estado: str | None = None
    cidade: str | None = None
    confidence: str = "none"
    strategy: str = "none"


BR_STATES = {
    "AC": "Acre",
    "AL": "Alagoas",
    "AP": "Amapa",
    "AM": "Amazonas",
    "BA": "Bahia",
    "CE": "Ceara",
    "DF": "Distrito Federal",
    "ES": "Espirito Santo",
    "GO": "Goias",
    "MA": "Maranhao",
    "MT": "Mato Grosso",
    "MS": "Mato Grosso do Sul",
    "MG": "Minas Gerais",
    "PA": "Para",
    "PB": "Paraiba",
    "PR": "Parana",
    "PE": "Pernambuco",
    "PI": "Piaui",
    "RJ": "Rio de Janeiro",
    "RN": "Rio Grande do Norte",
    "RS": "Rio Grande do Sul",
    "RO": "Rondonia",
    "RR": "Roraima",
    "SC": "Santa Catarina",
    "SP": "Sao Paulo",
    "SE": "Sergipe",
    "TO": "Tocantins",
}

MX_STATE_ALIASES = {
    "cdmx": "Ciudad de Mexico",
    "ciudad de mexico": "Ciudad de Mexico",
    "q r": "Quintana Roo",
    "qr": "Quintana Roo",
    "quintana roo": "Quintana Roo",
    "yuc": "Yucatan",
    "yucatan": "Yucatan",
    "jal": "Jalisco",
    "jalisco": "Jalisco",
    "nl": "Nuevo Leon",
    "nuevo leon": "Nuevo Leon",
    "bcs": "Baja California Sur",
    "baja california sur": "Baja California Sur",
    "bc": "Baja California",
    "baja california": "Baja California",
    "nay": "Nayarit",
    "nayarit": "Nayarit",
    "oax": "Oaxaca",
    "oaxaca": "Oaxaca",
    "pue": "Puebla",
    "puebla": "Puebla",
    "qro": "Queretaro",
    "queretaro": "Queretaro",
    "gro": "Guerrero",
    "guerrero": "Guerrero",
    "gto": "Guanajuato",
    "guanajuato": "Guanajuato",
    "chih": "Chihuahua",
    "chihuahua": "Chihuahua",
    "coah": "Coahuila",
    "coahuila": "Coahuila",
    "sin": "Sinaloa",
    "sinaloa": "Sinaloa",
    "son": "Sonora",
    "sonora": "Sonora",
    "tab": "Tabasco",
    "tabasco": "Tabasco",
    "tamps": "Tamaulipas",
    "tamaulipas": "Tamaulipas",
    "ver": "Veracruz",
    "veracruz": "Veracruz",
    "mich": "Michoacan",
    "michoacan": "Michoacan",
}

UK_CITIES = {
    "london": ("London", "England"),
    "birmingham": ("Birmingham", "England"),
    "manchester": ("Manchester", "England"),
    "liverpool": ("Liverpool", "England"),
    "leeds": ("Leeds", "England"),
    "bristol": ("Bristol", "England"),
    "sheffield": ("Sheffield", "England"),
    "nottingham": ("Nottingham", "England"),
    "leicester": ("Leicester", "England"),
    "coventry": ("Coventry", "England"),
    "oxford": ("Oxford", "England"),
    "cambridge": ("Cambridge", "England"),
    "southampton": ("Southampton", "England"),
    "newcastle upon tyne": ("Newcastle upon Tyne", "England"),
    "glasgow": ("Glasgow", "Scotland"),
    "edinburgh": ("Edinburgh", "Scotland"),
    "cardiff": ("Cardiff", "Wales"),
    "belfast": ("Belfast", "Northern Ireland"),
}

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}

COUNTRY_ALIASES = {
    "br": "BR", "brasil": "BR", "brazil": "BR",
    "mx": "MX", "mexico": "MX",
    "uk": "UK", "gb": "UK", "reino unido": "UK", "united kingdom": "UK",
    "us": "US", "usa": "US", "eua": "US", "united states": "US",
}

STREET_WORDS = {
    "rua", "avenida", "av", "rodovia", "estrada", "street", "road", "avenue",
    "boulevard", "blvd", "highway", "calle", "carretera", "paseo",
}


def _fold(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in text if not unicodedata.combining(char)).casefold()


def _token(value):
    return re.sub(r"[^a-z0-9]+", " ", _fold(value)).strip()


def normalize_country(value):
    return COUNTRY_ALIASES.get(_token(value), str(value or "").strip().upper())


def _clean_city(value):
    candidate = re.sub(r"^\s*\d{4,6}(?:-\d{3})?\s+", "", str(value or "")).strip(" -")
    if not candidate or any(char.isdigit() for char in candidate):
        return None
    words = set(_token(candidate).split())
    if words & STREET_WORDS or len(candidate) > 70:
        return None
    return re.sub(r"\s+", " ", candidate)


def _infer_brazil(address):
    state_codes = "|".join(BR_STATES)
    pattern = re.compile(
        rf"(?:^|[,;]|\s-\s)\s*(?P<city>[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .'-]{{1,65}}?)"
        rf"\s*(?:-|,)\s*(?P<state>{state_codes})(?=\s*(?:,|\d{{5}}|$))",
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(address))
    if matches:
        match = matches[-1]
        city = _clean_city(match.group("city"))
        state_code = match.group("state").upper()
        if city:
            return GeographyMatch(BR_STATES[state_code], city, "high", "br-city-uf")

    parts = [part.strip() for part in address.split(",") if part.strip()]
    state_aliases = {_token(name): name for name in BR_STATES.values()}
    state_aliases.update({code.casefold(): name for code, name in BR_STATES.items()})
    for index in range(len(parts) - 1, 0, -1):
        state = state_aliases.get(_token(parts[index]))
        if state:
            city = _clean_city(parts[index - 1])
            if city:
                return GeographyMatch(state, city, "high", "br-city-state")
    return GeographyMatch()


def _infer_mexico(address):
    parts = [part.strip() for part in address.split(",") if part.strip()]
    for index in range(len(parts) - 1, 0, -1):
        state = MX_STATE_ALIASES.get(_token(parts[index]))
        if not state:
            continue
        city = _clean_city(parts[index - 1])
        if city:
            confidence = "high" if re.match(r"^\s*\d{5}\b", parts[index - 1]) else "medium"
            return GeographyMatch(state, city, confidence, "mx-city-state")
    return GeographyMatch()


def _infer_uk(address):
    folded = _fold(address)
    for key in sorted(UK_CITIES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(key)}\b", folded):
            city, state = UK_CITIES[key]
            return GeographyMatch(state, city, "high", "uk-known-city")
    return GeographyMatch()


def _infer_us(address):
    state_codes = "|".join(US_STATES)
    pattern = re.compile(
        rf"(?:^|,)\s*(?P<city>[A-Za-z .'-]{{2,60}}?),\s*(?P<state>{state_codes})"
        rf"(?:\s+\d{{5}}(?:-\d{{4}})?)?(?:,|$)",
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(address))
    if not matches:
        return GeographyMatch()
    match = matches[-1]
    city = _clean_city(match.group("city"))
    state_code = match.group("state").upper()
    if not city:
        return GeographyMatch()
    return GeographyMatch(US_STATES[state_code], city, "high", "us-city-state")


def infer_geography(address, country):
    address = str(address or "").strip()
    if not address:
        return GeographyMatch()

    country_code = normalize_country(country)
    if country_code == "BR":
        return _infer_brazil(address)
    if country_code == "MX":
        return _infer_mexico(address)
    if country_code == "UK":
        return _infer_uk(address)
    if country_code == "US":
        return _infer_us(address)
    return GeographyMatch()
