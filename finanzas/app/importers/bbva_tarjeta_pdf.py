"""BBVA credit card: the monthly statement PDF that arrives by email ("Resumen ... Visa").

Rows are read by position, because the text alone can't tell a peso amount from a dollar one:
each table has a header FECHA | DESCRIPCIÓN | NRO. CUPÓN | PESOS | DÓLARES, amounts are
right-aligned under PESOS or DÓLARES, and the coupon number sits under NRO. CUPÓN.

The statement also says when it closed and when the previous one did; ledger.store() uses
that window so a statement replaces whatever was loaded for the same period from the Excel."""
import re
from datetime import date

from ..money import parse_amount
from .generic import ImportError_, Row

MONTHS = {m: i + 1 for i, m in enumerate(
    ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"])}
DATE = re.compile(r"^(\d{2})-([A-Za-z]{3})-(\d{2})$")
AMOUNT = re.compile(r"^-?[\d.]+,\d{2}$")
INSTALLMENT = re.compile(r"\b(?:C\.?|CUOTA)\s?(\d{1,2})/(\d{1,2})\b", re.I)


class Statement(list):
    """The rows, plus the statement's dates (ISO strings) and what it asks to pay (centavos)."""
    close = prev_close = due = next_close = None
    total = total_usd = minimum = None


def _date(text: str) -> date | None:
    m = DATE.match(text)
    if not m or m[2].lower() not in MONTHS:
        return None
    return date(2000 + int(m[3]), MONTHS[m[2].lower()], int(m[1]))


def _dates_after(text: str, label: str, n: int) -> list[date]:
    """The first n dates that follow a label like 'CIERRE ACTUAL' in the page text."""
    i = text.find(label)
    if i < 0:
        return []
    found = [_date(t) for t in re.findall(r"\d{2}-[A-Za-z]{3}-\d{2}", text[i:])]
    return [d for d in found if d][:n]


def _lines(words: list[dict]) -> list[list[dict]]:
    """Groups words into lines: same row when their tops are within 3 points."""
    lines = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and abs(lines[-1][0]["top"] - w["top"]) <= 3:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


def detect_text(first_page_text: str) -> bool:
    t = first_page_text or ""
    return "CIERRE ACTUAL" in t and "SALDO ACTUAL U$S" in t and "Tarjetas de Cr" in t


def parse_pages(pages: list[dict]) -> Statement:
    """pages: [{"text": str, "words": [{"text", "x0", "x1", "top"}, ...]}, ...]"""
    st = Statement()
    first = pages[0]["text"] if pages else ""
    current = _dates_after(first, "CIERRE ACTUAL", 2)
    others = _dates_after(first, "CIERRE ANTERIOR", 4)
    if len(current) < 1:
        raise ImportError_("No encontré la fecha de cierre del resumen de la tarjeta.")
    st.close = current[0].isoformat()
    st.due = current[1].isoformat() if len(current) > 1 else None
    # The line under the CIERRE ACTUAL header: close, due, SALDO ACTUAL $, SALDO ACTUAL U$S, PAGO MÍNIMO $.
    under = first[first.find("CIERRE ACTUAL"):].splitlines()[1:2]
    amounts = re.findall(r"-?[\d.]+,\d{2}", under[0]) if under else []
    if len(amounts) == 3:
        st.total, st.total_usd, st.minimum = (parse_amount(a) for a in amounts)
    if len(others) == 4:
        st.prev_close, _, st.next_close, _ = (d.isoformat() for d in others)

    for page in pages:
        cols = None
        for line in _lines(page["words"]):
            texts = [w["text"] for w in line]
            if "FECHA" in texts and "PESOS" in texts:
                x1 = {w["text"]: w["x1"] for w in line}
                coupon = next((w["x0"] for w in line if w["text"] == "NRO."), None)
                cols = {"ars": x1["PESOS"], "usd": x1.get("DÓLARES", x1.get("DOLARES")), "coupon": coupon}
                continue
            if not cols:
                continue
            if texts[:2] == ["SALDO", "ACTUAL"] or (len(texts) > 1 and texts[1:3] == ["SALDO", "ACTUAL"]):
                cols = None
                continue
            when = _date(texts[0])
            if not when:
                continue
            ars = usd = None
            desc = []
            for w in line[1:]:
                is_amount = AMOUNT.match(w["text"])
                if is_amount and abs(w["x1"] - cols["ars"]) < 12:
                    ars = parse_amount(w["text"])
                elif is_amount and cols["usd"] and abs(w["x1"] - cols["usd"]) < 12:
                    usd = parse_amount(w["text"])
                elif cols["coupon"] and w["x0"] >= cols["coupon"] - 5:
                    continue  # coupon number
                else:
                    desc.append(w["text"])
            # Dollar purchases repeat the amount in the description: 'in1UL4LeBUSD 100,00'.
            text = " ".join(desc)
            text = re.sub(r"USD\s+[\d.]+,\d{2}$", "", text).strip()
            if not ars and not usd:
                continue
            row = Row(when, text, -ars if ars else None, usd=-usd if usd else None)
            if (m := INSTALLMENT.search(text)) and int(m[2]) > 1:
                row.month = st.close[:7]  # installments count when the statement charges them
            st.append(row)
    if not st:
        raise ImportError_("El resumen de la tarjeta no tiene movimientos.")
    return st


def _pages(data: bytes) -> list[dict]:
    import io

    import pdfplumber
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return [{"text": p.extract_text() or "",
                 "words": [{k: w[k] for k in ("text", "x0", "x1", "top")} for w in p.extract_words()]}
                for p in pdf.pages]


def detect(filename: str, data: bytes) -> bool:
    if not filename.lower().endswith(".pdf"):
        return False
    try:
        import io

        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return detect_text(pdf.pages[0].extract_text() or "")
    except Exception:
        return False


def parse(filename: str, data: bytes) -> Statement:
    return parse_pages(_pages(data))
