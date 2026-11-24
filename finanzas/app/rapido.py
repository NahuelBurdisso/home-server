"""Understands a cash expense written or dictated in one line, for Siri and the local AI:

  "gasté 8500 en la verdulería"        "12 mil en el super ayer"
  "pagué $3.200 de nafta el lunes"     "15 lucas de ropa"
  "cobré 50000 de un trabajo"          "compré dólares por 120.000"

Only cash: what you pay with the card, Mercado Pago or a transfer arrives with its statement,
so writing it here too would count it twice. No AI involved, like Recordatorios' parser:
the same sentence always means the same thing."""
import re
import unicodedata
from datetime import date, timedelta

from . import categories, classifier, ledger, people
from .money import ars, parse_amount

WEEKDAYS = {"lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3, "viernes": 4, "sabado": 5, "domingo": 6}
MULTIPLIERS = {"mil": 1_000, "k": 1_000, "luca": 1_000, "lucas": 1_000, "palo": 1_000_000, "palos": 1_000_000,
               "millon": 1_000_000, "millones": 1_000_000}
# Paid some other way: its statement brings it.
ELSEWHERE = [
    (r"\bcon (?:la )?(?:tarjeta|visa|master|credito|debito)\b", "la tarjeta"),
    (r"\bcon (?:mercado ?pago|mp|qr)\b", "Mercado Pago"),
    (r"\b(?:por |con )?transferencia\b|\btransferi\b", "la cuenta del banco"),
]
INCOME = r"\b(?:cobre|me pagaron|me depositaron|ingreso|ingresaron|me entraron|vendi)\b"
USD_BUY = r"\bcompre (?:dolares|usd|verdes)\b"
FILLER = {"gaste", "gasto", "pague", "pago", "compre", "cobre", "me", "pagaron", "depositaron", "ingreso",
          "ingresaron", "entraron", "vendi", "anota", "anotame", "anotar", "carga", "cargame", "cargar", "de", "del",
          "en", "el", "la", "los", "las", "un", "una", "unos", "unas", "al", "a", "por", "para", "y", "que", "hoy",
          "pesos", "peso", "$", "efectivo", "cash", "con", "plata"}


def fold(s: str) -> str:
    """Lowercase without accents, one character per character (so positions match the original)."""
    return "".join(unicodedata.normalize("NFD", c.lower())[0] for c in s)


class _Text:
    def __init__(self, s: str):
        self.orig, self.low = s, fold(s)

    def take(self, pattern: str):
        m = re.search(pattern, self.low)
        if m:
            a, b = m.span()
            self.low = self.low[:a] + " " * (b - a) + self.low[b:]
            self.orig = self.orig[:a] + " " * (b - a) + self.orig[b:]
        return m


def _past_day_of_month(today: date, day: int) -> date | None:
    """The last day-th up to today: 'el 15' on the 21st is this month's, on the 10th last month's."""
    for back in range(2):
        y, m = divmod(today.month - 1 - back, 12)
        try:
            d = date(today.year + y, m + 1, day)
        except ValueError:
            continue
        if d <= today:
            return d
    return None


def _when(t: _Text, today: date) -> date:
    if t.take(r"\b(?:antes de ayer|anteayer)\b"):
        return today - timedelta(days=2)
    if t.take(r"\bayer\b"):
        return today - timedelta(days=1)
    if (m := t.take(r"\b(?:el )?(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b")):
        y = int(m[3]) if m[3] else today.year
        try:
            d = date(y + 2000 if y < 100 else y, int(m[2]), int(m[1]))
            return d if d <= today or m[3] else d.replace(year=d.year - 1)
        except ValueError:
            pass
    if (m := t.take(rf"\b(?:el |este |el pasado )?({'|'.join(WEEKDAYS)})(?: pasado)?\b")):
        return today - timedelta(days=(today.weekday() - WEEKDAYS[m[1]]) % 7)
    # "el 15", but not "el 15 mil": a number followed by a multiplier is the amount.
    if (m := t.take(rf"\bel (\d{{1,2}})\b(?! ?(?:{'|'.join(MULTIPLIERS)})\b)")):
        if (d := _past_day_of_month(today, int(m[1]))):
            return d
    return today


def _amount(t: _Text) -> int | None:
    """Centavos. '8500', '8.500', '$ 8.500,50', '12 mil', '2,5 mil', '1.500 mil', '15 lucas', 'mil'.
    With several numbers ('2 pizzas por 8500') the amount is the one marked as money ($, 'mil',
    'pesos', after 'por'), else the biggest."""
    pattern = rf"(\$ ?|\bpor )?(\d[\d.,]*)(?: ?({'|'.join(MULTIPLIERS)})\b)?(?: ?(pesos)\b)?"
    best = None
    for m in re.finditer(pattern, t.low):
        try:
            cents = parse_amount(m[2].rstrip(".,"))
        except ValueError:
            continue
        if m[3]:
            cents = round(cents * MULTIPLIERS[m[3]])
        marked = bool(m[1] or m[3] or m[4])
        if best is None or (marked, cents) > (best[0], best[1]):
            best = (marked, cents, m.span(2) if not (m[1] or m[3] or m[4]) else m.span())
    if best:
        a, b = best[2]
        t.low = t.low[:a] + " " * (b - a) + t.low[b:]
        t.orig = t.orig[:a] + " " * (b - a) + t.orig[b:]
        return best[1]
    if t.take(r"\bmil\b(?: pesos\b)?"):
        return 100_000
    return None


def amount_of(text: str) -> int | None:
    """The amount in a short text (a form field), in centavos, or None."""
    return _amount(_Text(" ".join(str(text).split())))


def _description(s: str) -> str:
    words = s.replace(",", " ").split()
    strip = lambda w: fold(w).strip(".;:!?¿¡-")  # noqa: E731
    while words and strip(words[0]) in FILLER | {""}:
        words.pop(0)
    while words and strip(words[-1]) in FILLER | {""}:
        words.pop()
    text = " ".join(words).strip(" .;:-")
    return text[:1].upper() + text[1:]


def parse(text: str, today: date | None = None) -> dict:
    """{'kind', 'date', 'amount' (centavos, positive), 'description'}. Raises ValueError with a
    sentence for the person when it can't be written down."""
    today = today or date.today()
    t = _Text(" ".join(text.split()))
    if re.search(r"\b(?:dolares|usd|us\$|verdes)\b", t.low) and not re.search(USD_BUY, t.low):
        raise ValueError("Decime cuánto fue en pesos: los dólares en efectivo no los anoto.")
    if not re.search(r"\befectivo\b", t.low):
        for pattern, where in ELSEWHERE:
            if re.search(pattern, t.low):
                raise ValueError(f"Eso llega solo con lo que cargás de {where}: no lo anoto para no contarlo dos veces.")
    kind = "usd_buy" if t.take(USD_BUY) else "income" if re.search(INCOME, t.low) else "expense"
    day = _when(t, today)
    cents = _amount(t)
    if not cents:
        raise ValueError("No entendí cuánto. Probá por ejemplo: gasté 8500 en la verdulería.")
    if day > today:
        raise ValueError("Esa fecha todavía no llegó.")
    default = {"expense": "Gasto en efectivo", "income": "Ingreso en efectivo", "usd_buy": "Compra de dólares"}[kind]
    return {"kind": kind, "date": day, "amount": abs(cents), "description": _description(t.orig) or default}


def _day_text(d: date, today: date) -> str:
    return {0: "hoy", 1: "ayer", 2: "anteayer"}.get((today - d).days, f"el {d.day}/{d.month}")


class Written(tuple):
    """(sentence, category still unknown), plus .id: the new movement, for the bot's undo (X-Id)."""
    id: int


def write_down(conn, text: str, today: date | None = None, who: str | None = None) -> Written:
    """Saves it and answers in one sentence (Siri reads it out). The bool says whether the
    category is still unknown, so the caller can ask the classifier in the background."""
    today = today or date.today()
    f = parse(text, today)
    signed = f["amount"] if f["kind"] == "income" else -f["amount"]
    tx_id = ledger.add_manual(conn, f["date"], f["description"], signed, f["kind"], who=who or people.HOLDER)
    ledger.categorize(conn)
    conn.commit()
    when = _day_text(f["date"], today)
    if f["kind"] == "income":
        out = Written((f"Anotado: ingreso de {ars(f['amount'])} ({f['description']}), {when}.", False))
    elif f["kind"] == "usd_buy":
        out = Written((f"Anotado: compra de dólares por {ars(f['amount'])}, {when}.", False))
    else:
        cat = conn.execute("SELECT category FROM transactions WHERE id = ?", (tx_id,)).fetchone()[0]
        tail = f" {categories.label(cat)}." if cat else (
            " La categoría te la sugiere la IA en Revisar." if classifier.get().name != "none" else "")
        out = Written((f"Anotado: {ars(f['amount'])} en {f['description']}, {when}, en efectivo.{tail}", cat is None))
    out.id = tx_id
    return out
