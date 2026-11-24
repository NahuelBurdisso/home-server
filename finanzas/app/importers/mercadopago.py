"""Mercado Pago: 'Reporte de cuenta' CSV (account_statement-<id>.csv).

A balance block first (INITIAL_BALANCE;CREDITS;...), then one line per movement:
RELEASE_DATE;TRANSACTION_TYPE;REFERENCE_ID;TRANSACTION_NET_AMOUNT;PARTIAL_BALANCE"""
from ..money import parse_amount, parse_date
from .generic import ImportError_, Row

HEADER = "RELEASE_DATE;TRANSACTION_TYPE"


def _text(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def detect(filename: str, data: bytes) -> bool:
    return filename.lower().endswith(".csv") and HEADER in _text(data[:4000])


def parse(filename: str, data: bytes) -> list[Row]:
    lines = _text(data).splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(HEADER)), None)
    if start is None:
        raise ImportError_("No parece un reporte de cuenta de Mercado Pago.")
    cols = lines[start].split(";")
    i_date, i_type = cols.index("RELEASE_DATE"), cols.index("TRANSACTION_TYPE")
    i_amount = cols.index("TRANSACTION_NET_AMOUNT")
    rows = []
    for line in lines[start + 1:]:
        cells = line.split(";")
        if len(cells) < len(cols) or not cells[i_date].strip():
            continue
        amount = parse_amount(cells[i_amount])
        if amount:
            rows.append(Row(parse_date(cells[i_date]), " ".join(cells[i_type].split()), amount))
    if not rows:
        raise ImportError_("El reporte de Mercado Pago no tiene movimientos.")
    return rows
