"""BBVA savings account in pesos: 'Detalle de Movimientos de Cuenta' from home banking (.xls).

Title 'Detalle de Movimientos de Cuenta: CA$ 207-...', then
Fecha | Concepto | (channel, untitled) | Importe | Saldo, newest first."""
from ..money import parse_amount, parse_date
from .generic import ImportError_, Row, norm, read_table

TITLE = "detalle de movimientos de cuenta"


def _title(table: list[list]) -> str | None:
    for raw in table[:5]:
        for cell in raw:
            if norm(cell).startswith(TITLE):
                return str(cell)
    return None


def detect(filename: str, data: bytes) -> bool:
    if not filename.lower().endswith((".xls", ".xlsx")):
        return False
    try:
        title = _title(read_table(filename, data))
    except Exception:
        return False
    # Only the peso account for now: a dollar account says CAU$S / U$S instead of CA$.
    return bool(title) and "CA$" in title.replace(" ", "")


def parse_table(table: list[list]) -> list[Row]:
    title = _title(table)
    if not title or "CA$" not in title.replace(" ", ""):
        raise ImportError_("No parece el detalle de la caja de ahorro en pesos de BBVA.")
    start = next((i for i, raw in enumerate(table[:10])
                  if [norm(c) for c in raw][:2] == ["fecha", "concepto"]), None)
    if start is None:
        raise ImportError_("No encontré los títulos Fecha / Concepto / Importe.")
    header = [norm(c) for c in table[start]]
    i_amount = header.index("importe")
    rows = []
    for raw in table[start + 1:]:
        cells = list(raw) + [""] * len(header)
        try:
            when = parse_date(cells[0])
            amount = parse_amount(cells[i_amount])
        except ValueError:
            continue
        if amount:
            rows.append(Row(when, " ".join(str(cells[1]).split()), amount))
    if not rows:
        raise ImportError_("El detalle de cuenta no tiene movimientos.")
    return rows


def parse(filename: str, data: bytes) -> list[Row]:
    return parse_table(read_table(filename, data))
