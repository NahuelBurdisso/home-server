"""Parsing and formatting of Argentine amounts and dates."""
import re
from datetime import date, datetime

MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
          "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def parse_amount(value) -> int:
    """Returns centavos. Understands '$ -1.234,56', '1,234.56', '(500)', '1.234-' and numbers."""
    if isinstance(value, (int, float)):
        return round(value * 100)
    s = str(value).strip()
    if not s:
        raise ValueError("empty amount")
    s = s.replace("−", "-")  # the Unicode minus (this app writes it, spreadsheets copy it)
    # A minus anywhere before the first digit ('$ -1.234,56', 'ARS -500'), or one at the end.
    negative = "-" in re.split(r"\d", s, maxsplit=1)[0] or s.endswith("-") or (s.startswith("(") and s.endswith(")"))
    s = re.sub(r"[^\d.,]", "", s)
    if not s:
        raise ValueError(f"not an amount: {value!r}")
    if "." in s and "," in s:
        decimal = "," if s.rfind(",") > s.rfind(".") else "."
    elif "," in s:
        decimal = "," if re.search(r",\d{1,2}$", s) and s.count(",") == 1 else None
    elif "." in s:
        decimal = "." if re.search(r"\.\d{1,2}$", s) and s.count(".") == 1 else None
    else:
        decimal = None
    if decimal:
        whole, _, frac = s.rpartition(decimal)
        whole = re.sub(r"[.,]", "", whole) or "0"
        cents = int(whole) * 100 + int(frac.ljust(2, "0")[:2])
    else:
        cents = int(re.sub(r"[.,]", "", s)) * 100
    return -cents if negative else cents


def parse_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3]))
    m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})", s)
    if m:
        year = int(m[3])
        year += 2000 if year < 100 else 0
        return date(year, int(m[2]), int(m[1]))
    raise ValueError(f"not a date: {value!r}")


def ars(cents: int, sign: bool = False) -> str:
    """'$ 412.000' (no centavos: they don't matter for a monthly view)."""
    pesos = round(abs(cents) / 100)
    text = "$ " + f"{pesos:,}".replace(",", ".")
    if cents < 0:
        return "−" + text
    return ("+" + text) if sign and cents > 0 else text


def usd(amount: float) -> str:
    text = "US$ " + f"{round(abs(amount)):,}".replace(",", ".")
    return "−" + text if round(amount) < 0 else text


def month_name(month: str, year: bool = True) -> str:
    y, m = month.split("-")
    name = MONTHS[int(m) - 1]
    return f"{name} {y}" if year else name


def month_short(month: str) -> str:
    y, m = month.split("-")
    return f"{MONTHS[int(m) - 1][:3]} {y[2:]}"


def shift_month(month: str, delta: int) -> str:
    y, m = map(int, month.split("-"))
    total = y * 12 + (m - 1) + delta
    return f"{total // 12:04d}-{total % 12 + 1:02d}"
