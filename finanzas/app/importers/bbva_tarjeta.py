"""BBVA credit card: 'Movimientos del Período' from home banking (.xls).

Columns: Nro. Tarjeta | Fecha | Establecimiento | Cuota | Importe en $ | Importe en U$S.
Purchases count in the month you made them; installments ('03/06') count in the month of
the statement that charges them, which is the latest date in the file."""
import re

from ..money import parse_amount, parse_date
from .generic import ImportError_, Row, norm, read_table

HEADER = ["fecha", "establecimiento", "cuota"]


def _header_index(table: list[list]) -> int | None:
    for i, raw in enumerate(table[:20]):
        cells = [norm(c) for c in raw]
        if all(h in cells for h in HEADER) and any(c.startswith("importe en u") for c in cells):
            return i
    return None


def detect(filename: str, data: bytes) -> bool:
    if not filename.lower().endswith((".xls", ".xlsx")):
        return False
    try:
        return _header_index(read_table(filename, data)) is not None
    except Exception:
        return False


def parse_table(table: list[list]) -> list[Row]:
    start = _header_index(table)
    if start is None:
        raise ImportError_("No parece un resumen de tarjeta BBVA.")
    header = [norm(c) for c in table[start]]
    col = {name: header.index(name) for name in HEADER}
    col_ars = next(i for i, c in enumerate(header) if c.startswith("importe en $"))
    col_usd = next(i for i, c in enumerate(header) if c.startswith("importe en u"))

    rows = []
    for raw in table[start + 1:]:
        cells = list(raw) + [""] * len(header)
        try:
            when = parse_date(cells[col["fecha"]])
        except ValueError:
            continue  # card totals and the period total have no date
        ars = str(cells[col_ars]).strip()
        dollars = str(cells[col_usd]).strip()
        amount = -parse_amount(ars) if ars else None   # charges are positive in the file
        usd = -parse_amount(dollars) if dollars else None
        if not amount and not usd:
            continue
        desc = " ".join(str(cells[col["establecimiento"]]).split())
        cuota = re.match(r"\s*(\d+)\s*/\s*(\d+)", str(cells[col["cuota"]]))
        row = Row(when, desc, amount, usd=usd)
        if cuota and int(cuota[2]) > 1:
            row.description = f"{desc} (cuota {int(cuota[1])}/{int(cuota[2])})"
            row.month = "installment"  # resolved below, once the statement date is known
        rows.append(row)
    if not rows:
        raise ImportError_("El resumen de tarjeta no tiene movimientos.")

    statement_month = max(r.date for r in rows).isoformat()[:7]
    for r in rows:
        if r.month == "installment":
            r.month = statement_month
    return rows


def parse(filename: str, data: bytes) -> list[Row]:
    return parse_table(read_table(filename, data))
