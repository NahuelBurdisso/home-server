"""Reads any CSV or Excel with a header row like 'Fecha | Descripción | Importe'
(or separate Débito / Crédito columns). The bank-specific readers build on this."""
import csv
import io
import unicodedata
from dataclasses import dataclass
from datetime import date

from ..money import parse_amount, parse_date


@dataclass
class Row:
    date: date
    description: str
    amount: int | None   # centavos, negative = money out; None if only charged in dollars
    month: str | None = None  # set when it counts for another month (card statements)
    usd: int | None = None    # US$ centavos, same sign rule; converted to pesos when saved
    key: str | None = None    # its own id at the source (Mercado Pago's API), instead of one made from the row


class ImportError_(Exception):
    """A file we can't read. The message is shown to the user as is."""


def norm(text) -> str:
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return " ".join(text.lower().replace("_", " ").split())


DATE_COLS = ["fecha", "fecha de operacion", "fecha operacion", "fecha de origen", "fecha de creacion",
             "fecha de aprobacion", "fecha de liberacion", "fecha movimiento", "date"]
DESC_COLS = ["descripcion", "concepto", "detalle", "description", "movimiento", "tipo de operacion",
             "referencia"]
AMOUNT_COLS = ["importe", "monto", "importe en pesos", "monto neto", "valor", "amount",
               "monto neto acreditado", "net amount"]
DEBIT_COLS = ["debito", "debitos", "debe", "egreso", "egresos", "salida"]
CREDIT_COLS = ["credito", "creditos", "haber", "ingreso", "ingresos", "entrada"]


def _find(header: list[str], names: list[str]) -> int | None:
    for name in names:          # exact names first, in order of preference
        if name in header:
            return header.index(name)
    for i, cell in enumerate(header):
        if any(cell.startswith(name) for name in names):
            return i
    return None


def _xls_value(cell, datemode):
    import xlrd
    if cell.ctype == xlrd.XL_CELL_DATE:
        return xlrd.xldate_as_datetime(cell.value, datemode)
    if isinstance(cell.value, str):
        # BBVA's exports lose accents ('PERCEPCI�N'); put back the common one, drop the rest.
        return cell.value.replace("CI�N", "CIÓN").replace("�", "")
    return cell.value


def read_table(filename: str, data: bytes) -> list[list]:
    if filename.lower().endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        rows = []
        for ws in wb.worksheets:
            rows += [list(r) for r in ws.iter_rows(values_only=True)]
        return rows
    if filename.lower().endswith(".xls"):
        import xlrd  # the old Excel format BBVA still exports
        try:
            wb = xlrd.open_workbook(file_contents=data)
        except xlrd.XLRDError as e:
            raise ImportError_(f"No pude abrir el Excel ({e}).")
        rows = []
        for sh in wb.sheets():
            rows += [[_xls_value(sh.cell(r, c), wb.datemode) for c in range(sh.ncols)] for r in range(sh.nrows)]
        return rows
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(text[:5000], delimiters=";,\t|")
    except csv.Error:
        dialect = csv.excel
    return [r for r in csv.reader(io.StringIO(text), dialect)]


def parse_rows(table: list[list]) -> list[Row]:
    for start, raw in enumerate(table[:40]):
        header = [norm(c) for c in raw]
        d, s = _find(header, DATE_COLS), _find(header, DESC_COLS)
        a, deb, cred = _find(header, AMOUNT_COLS), _find(header, DEBIT_COLS), _find(header, CREDIT_COLS)
        if d is not None and s is not None and (a is not None or deb is not None or cred is not None):
            break
    else:
        raise ImportError_("No encontré una fila de títulos con Fecha, Descripción e Importe.")

    rows = []
    for raw in table[start + 1:]:
        cells = list(raw) + [None] * len(header)
        if not cells[d] or not str(cells[d]).strip():
            continue
        try:
            when = parse_date(cells[d])
        except ValueError:
            continue  # subtotal lines, footers
        if a is not None:
            try:
                amount = parse_amount(cells[a])
            except ValueError:
                continue
        else:
            out = parse_amount(cells[deb]) if deb is not None and cells[deb] not in (None, "") else 0
            inn = parse_amount(cells[cred]) if cred is not None and cells[cred] not in (None, "") else 0
            amount = abs(inn) - abs(out)
        if amount == 0:
            continue
        rows.append(Row(when, " ".join(str(cells[s] or "").split()), amount))
    if not rows:
        raise ImportError_("El archivo tiene títulos pero ningún movimiento que pueda leer.")
    return rows


def parse(filename: str, data: bytes) -> list[Row]:
    return parse_rows(read_table(filename, data))
