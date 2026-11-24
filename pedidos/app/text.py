"""Words and amounts: folding, singular/plural, and how much a line or a presentation is. Plain
rules, the same Lista uses (lista/app/parse.py and store._same), so a thing keys the same in both."""
import re
import unicodedata


def fold(s: str) -> str:
    """Lowercase, without accents (ñ → n): how texts are compared."""
    s = unicodedata.normalize("NFKD", (s or "").lower()).replace("⁄", "/")  # ½ → 1⁄2 → 1/2
    return "".join(c for c in s if not unicodedata.combining(c))


def words(s: str) -> list[str]:
    return re.sub(r"[^a-z0-9/ ]", " ", fold(s)).split()


def forms(word: str) -> set[str]:
    """A word and what it could be in singular: 'tomates' -> tomates, tomate, tomat."""
    out = {word}
    if word.endswith("s"):
        out.add(word[:-1])
    if word.endswith("es"):
        out.add(word[:-2])
    return out


def same_word(a: str, b: str) -> bool:
    return bool(forms(a) & forms(b))


def same(a: str, b: str) -> bool:
    """The same thing, singular or plural: 'papas' = 'papa', 'limones' = 'limon'."""
    wa, wb = words(a), words(b)
    return len(wa) == len(wb) and all(same_word(x, y) for x, y in zip(wa, wb))


def nice(name: str) -> str:
    """Catalogs shout ("BANANA ECUATORIANA"); the order reads better as "Banana ecuatoriana"."""
    name = " ".join((name or "").split())
    if name.upper() == name:
        name = name.lower()
    return name[:1].upper() + name[1:]


# --- amounts ---------------------------------------------------------------------------------------

NUMBER_WORDS = {"un": 1, "una": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
                "ocho": 8, "nueve": 9, "diez": 10, "doce": 12, "media": .5, "medio": .5, "un par de": 2}
HALVES = {"medio": .5, "media": .5, "cuarto": .25}
KG = r"kg|kgs|kilo|kilos"
GR = r"g|gr|grs|gramo|gramos"
OTHER_MEASURES = r"l|lt|lts|litro|litros|ml|cc|cm|m|metro|metros"
NUM = r"\d+/\d+|\d+(?:[.,]\d+)?|un par de|una|uno|un|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|doce|media|medio"
LINE_AMOUNT = re.compile(rf"^(?P<n>{NUM})\b\s*(?:y (?P<h1>medio|media|cuarto)\b\s*)?(?:(?P<u>{KG}|{GR}|{OTHER_MEASURES})\b\s*)?"
                         r"(?:y (?P<h2>medio|media|cuarto)\b)?")


def number(s: str) -> float | None:
    s = s.strip()
    if s in NUMBER_WORDS:
        return NUMBER_WORDS[s]
    if "/" in s:
        a, b = s.split("/", 1)
        return int(a) / int(b) if a.isdigit() and b.isdigit() and int(b) else None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def line_amount(text: str) -> tuple[float | None, float | None]:
    """(grams, units) asked on a list line: '1,5 kg de papas' → (1500, None), 'un kilo y medio' →
    (1500, None), '500 g' → (500, None), '3 bananas' or '2 atados de rúcula' → (None, 3 / 2),
    'leche' or '2 litros de leche' → (None, None)."""
    t = re.sub(r"(\d)(?=[a-z])", r"\1 ", " ".join(fold(text).split()))  # "2kg" → "2 kg"
    m = LINE_AMOUNT.match(t)
    if not m:
        return None, None
    n = number(m["n"])
    if n is None or n <= 0:
        return None, None
    n += HALVES.get(m["h1"] or m["h2"] or "", 0)
    unit = m["u"] or ""
    if re.fullmatch(KG, unit):
        return n * 1000, None
    if re.fullmatch(GR, unit):
        return n, None
    if unit:  # litres, metres...: an amount, but not one a shop's presentations speak
        return None, None
    return None, n


PRES_WEIGHT = re.compile(rf"^(?P<n>\d+/\d+|\d+(?:[.,]\d+)?)?\s*(?P<u>{KG}|{GR})$")
PRES_UNIT = re.compile(r"^(?P<n>\d+/\d+|\d+(?:[.,]\d+)?)?\s*(?:unidad|unidades|unid|und|un|u)$")
PRES_HALF = re.compile(r"^(?:1\s+)?(?:media|medio|mitad)(?:\s+unidad)?$")
PRES_FRACTION = re.compile(r"^(?P<n>\d+/\d+)\s+[a-z]+$")  # "1/2 SANDÍA": half of one


def presentation_amount(name: str) -> tuple[float | None, float | None]:
    """(grams, units) of a presentation: '1 KG' → (1000, None), '1/4 kg' → (250, None), '500 GR' →
    (500, None), 'UNIDAD' / '1 UNID' → (None, 1), 'MEDIA' / '1 MITAD' / '1/2 SANDÍA' → (None, 0.5);
    anything else ("2 MAPLES") → (None, None)."""
    t = " ".join(fold(name).replace(".", " ").split())
    t = re.sub(r"(\d)(?=[a-z])", r"\1 ", t)  # "1/2KG" → "1/2 KG"
    if m := PRES_WEIGHT.match(t):
        n = number(m["n"]) if m["n"] else 1
        if n:
            return (n * 1000 if re.fullmatch(KG, m["u"]) else n), None
    if m := PRES_UNIT.match(t):
        n = number(m["n"]) if m["n"] else 1
        if n:
            return None, n
    if PRES_HALF.match(t):
        return None, .5
    if (m := PRES_FRACTION.match(t)) and (n := number(m["n"])) and n < 1:
        return None, n
    return None, None


# --- keys, as Lista makes them (lista/app/parse.py: AMOUNT and key_of) --------------------------------

_HALF = r"(?:y (?:medio|media|cuarto)\b\s*)?"
KEY_AMOUNT = re.compile(r"^(?:\d+/\d+|\d+(?:[.,]\d+)?|un par de|unas|unos|una|uno|un|dos|tres|cuatro|cinco|seis|media|medio)\b\s*"
                        + _HALF + r"(?:(?:kg|kilo|kilos|g|gr|gramos|l|lt|litro|litros|paquete|paquetes|caja|cajas|lata|latas|"
                        r"botella|botellas|docena|docenas|unidad|unidades|frasco|frascos|bolsa|bolsas|sachet|sachets|"
                        r"pack|packs|rollo|rollos|atado|atados|planta|plantas)\b\s*)?" + _HALF + r"(?:de\s+)?")


def key_of(text: str) -> str:
    """What it is, without the amount: '2 kg de papas' → 'papas' (for lines typed here, when Lista
    isn't the one reading them)."""
    folded = " ".join(fold(text).split())
    return KEY_AMOUNT.sub("", folded).strip() or folded
