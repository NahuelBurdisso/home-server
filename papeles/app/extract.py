"""The text of a document, and what can be read from it: its kind, a title, its date, and when it
expires. PDFs give their text; photos and scanned PDFs go through Tesseract (Spanish), on this Mac."""
import io
import re
import unicodedata
from calendar import monthrange
from datetime import date, timedelta

OCR_PAGES = 5  # a scanned PDF: only its first pages are read (enough to find and title it)


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def text_of(data: bytes, mime: str) -> str:
    """Whatever text can be had; '' when none (never raises: a document is kept even unreadable)."""
    try:
        if mime == "application/pdf":
            return _pdf(data)
        if mime.startswith("image/"):
            return _ocr(_image(data))
        if mime.startswith("text/"):
            return data.decode(errors="replace")
    except Exception:
        return ""
    return ""


def _pdf(data: bytes) -> str:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages[:50])
        if len(text.strip()) >= 40:
            return text
        # No text layer: a scan. Rendered and read like a photo.
        return "\n".join(_ocr(p.to_image(resolution=200).original) for p in pdf.pages[:OCR_PAGES])


def _image(data: bytes):
    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()  # iPhone photos (HEIC)
    except ImportError:
        pass
    img = Image.open(io.BytesIO(data))
    return ImageOps.exif_transpose(img)  # a photo taken sideways reads upright


def _ocr(img) -> str:
    import pytesseract
    img = img.convert("L")
    if max(img.size) > 3000:  # big phone photos: faster, and Tesseract reads them as well
        img.thumbnail((3000, 3000))
    return pytesseract.image_to_string(img, lang="spa")


# kind -> (label, emoji, words that give it away). The kind with the most hits wins.
KINDS = {
    "factura": ("Factura", "🧾", ["factura", "total a pagar", "cae", "comprobante autorizado", "iva", "consumidor final"]),
    "servicio": ("Servicio", "💡", ["epec", "aguas cordobesas", "ecogas", "telecom argentina", "personal flow", "amx argentina",
                                   "movistar", "fibertel", "kilowatt", "kwh", "periodo de facturacion", "medidor", "m3"]),
    "garantia": ("Garantía", "🛡️", ["garantia", "certificado de garantia", "meses de garantia", "service autorizado"]),
    "recibo": ("Recibo", "🧾", ["recibo", "comprobante de pago", "pagado", "recibimos de", "recibi de"]),
    "contrato": ("Contrato", "📜", ["contrato", "locacion", "locador", "locatario", "alquiler", "clausula", "las partes"]),
    "impuesto": ("Impuestos", "🏛️", ["afip", "arca", "rentas", "inmobiliario", "monotributo", "ganancias",
                                     "bienes personales", "automotor", "tasa municipal", "contribucion"]),
    "seguro": ("Seguro", "☂️", ["poliza", "seguro", "asegurado", "aseguradora", "cobertura", "siniestro"]),
    "salud": ("Salud", "🩺", ["receta", "diagnostico", "laboratorio", "paciente", "medico", "dr.", "dra.",
                             "obra social", "prepaga", "osde", "swiss medical", "apross", "rp/"]),
    "auto": ("Auto", "🚗", ["vtv", "patente", "dominio", "cedula verde", "titulo del automotor", "registro automotor"]),
    "banco": ("Banco", "🏦", ["resumen de cuenta", "cbu", "saldo", "tarjeta de credito", "bbva", "mercado pago"]),
    "identidad": ("Identidad", "🪪", ["documento nacional de identidad", "pasaporte", "registro de conducir", "renaper"]),
    "otro": ("Otro", "📄", []),
}


GENERIC = ("factura", "recibo")  # most papers are one of these too: a more specific kind wins


def kind_of(text: str, name: str = "") -> str:
    t = f" {' '.join(re.sub(r'[^a-z0-9.%$ ]', ' ', fold(f'{name} {text}')).split())} "
    scores = {k: sum(t.count(f" {w} ") for w in words) for k, (_, _, words) in KINDS.items() if words}
    specific = {k: v for k, v in scores.items() if k not in GENERIC and v}
    if specific:
        return max(specific, key=specific.get)
    best = max(scores, key=scores.get)
    return best if scores[best] else "otro"


DATE = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\b")
MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
          "noviembre", "diciembre"]
DATE_WORDS = re.compile(r"\b(\d{1,2}) de (" + "|".join(MONTHS) + r")(?: del?)? (\d{4})\b")  # "de 2026", "del 2026"


def _date(d: str, m: str, y: str) -> date | None:
    try:
        y = int(y)
        return date(y + 2000 if y < 100 else y, int(m), int(d))
    except ValueError:
        return None


def dates_in(text: str) -> list[tuple[int, date]]:
    """(position, date) of every plausible date, in order."""
    t = fold(text)
    out = [(m.start(), d) for m in DATE.finditer(t) if (d := _date(*m.groups())) and 1990 <= d.year <= 2100]
    out += [(m.start(), d) for m in DATE_WORDS.finditer(t)
            if (d := _date(m[1], str(MONTHS.index(m[2]) + 1), m[3]))]
    return sorted(out)


def doc_date(text: str) -> date | None:
    """The document's own date: the first one in it that isn't in the future."""
    today = date.today()
    return next((d for _, d in dates_in(text) if d <= today), None)


EXPIRES = re.compile(r"(?:fecha de )?(?:1(?:er|°)? )?(?:vencimiento|vence|vto\.?|valido hasta|vigencia hasta|"
                     r"vigente hasta|valida hasta)")
NOT_EXPIRY = re.compile(r"(?:de|del) cae\b|cae\s*$")  # AFIP's "Fecha de Vto. de CAE", on every e-invoice
PREVIOUS = re.compile(r"[\s.:]*anterior\b")  # a card statement's "Vencimiento anterior": last month's
OWN_LABEL = re.compile(r"[^\d\n:]{0,25}:")  # the rest of the keyword's label: " actual:", " de la factura:"
LABEL = re.compile(r"[a-z][a-z .°]*:")  # the next field: "periodo facturado:", "total a pagar:"
WARRANTY = re.compile(r"garantia(?: de| por)? (\d{1,2}|un|una) (meses|mes|anos|ano)\b|"
                      r"(\d{1,2}|un|una) (meses|mes|anos|ano) de garantia")


def _value_span(t: str, end: int) -> tuple[int, int]:
    """Where the value of an expiry keyword ending at `end` can be: up to 60 characters after it,
    stopping at the next field or keyword, and at the end of its line when the line has a value
    of its own (a keyword alone at the end of its line is a table header: its value is below)."""
    rest = t[end:end + 60]
    own = OWN_LABEL.match(rest)
    start = own.end() if own else 0
    stop = len(rest)
    line_end = rest.find("\n", start)
    if line_end >= 0 and any(c.isdigit() for c in rest[start:line_end]):
        stop = line_end
    for pattern in (LABEL, EXPIRES):
        if nxt := pattern.search(rest, start, stop):
            stop = nxt.start()
    return end + start, end + stop


def expiry_of(text: str, issued: date | None) -> date | None:
    """When it stops being valid: the date right after "vencimiento"/"vence"/"válido hasta" (not the
    CAE's, not last period's, not on or before the issue date, not over a year ago), or a
    warranty's months counted from the document's date."""
    t = fold(text)
    found = dates_in(text)
    oldest = date.today() - timedelta(days=365)
    for m in EXPIRES.finditer(t):
        if (NOT_EXPIRY.search(t[m.end():m.end() + 12]) or "cae" in t[max(0, m.start() - 12):m.start()]
                or PREVIOUS.match(t, m.end())):
            continue
        # The first date in the keyword's value that is after the issue date ("Fecha de emisión
        # Vencimiento" as a table header puts both dates under it), and not long gone.
        lo, hi = _value_span(t, m.end())
        after = [d for pos, d in found if lo <= pos < hi and (issued is None or d > issued) and d >= oldest]
        if after:
            return after[0]
    w = WARRANTY.search(t)
    if w and issued:
        n, unit = (w[1], w[2]) if w[1] else (w[3], w[4])
        n = 1 if n in ("un", "una") else int(n)
        months = n * 12 if unit.startswith("ano") else n
        y, mo = divmod(issued.month - 1 + months, 12)
        y, mo = issued.year + y, mo + 1
        return date(y, mo, min(issued.day, monthrange(y, mo)[1]))  # the 31st into a short month: its last day
    return None


JUNK_NAME = re.compile(r"^(img|image|scan|scanned|documento?|file|archivo|foto|photo|pdf|whatsapp|captura|screenshot)"
                       r"[\s_\-0-9]*", re.I)


def title_of(name: str, text: str, kind: str, when: date | None) -> str:
    """The file's name when it says something; else the first line of text that reads like one;
    else the kind and date."""
    stem = re.sub(r"\.[a-z0-9]{2,5}$", "", name, flags=re.I).replace("_", " ").strip()
    if stem and not JUNK_NAME.fullmatch(stem) and re.search(r"[a-zA-Z]{3}", stem):
        return (stem[:1].upper() + stem[1:])[:80]
    for line in text.splitlines():
        line = " ".join(line.split())
        if 6 <= len(line) <= 70 and sum(c.isalpha() for c in line) >= len(line) * 0.6:
            return line
    label = KINDS[kind][0]
    return f"{label} del {when:%d/%m/%Y}" if when else label
