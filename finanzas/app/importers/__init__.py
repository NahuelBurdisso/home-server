"""Turns an uploaded file into rows for one of your accounts.

Bank-specific readers go in SPECIFIC as (account, detect, parse); anything else falls back
to the generic CSV/Excel reader with the account picked on the upload form."""
from . import bbva_cuenta, bbva_tarjeta, bbva_tarjeta_pdf, generic, mercadopago
from .generic import ImportError_, Row, norm

SPECIFIC: list = [
    ("bbva", bbva_cuenta.detect, bbva_cuenta.parse),
    ("bbva_tarjeta", bbva_tarjeta.detect, bbva_tarjeta.parse),
    ("bbva_tarjeta", bbva_tarjeta_pdf.detect, bbva_tarjeta_pdf.parse),
    ("mercadopago", mercadopago.detect, mercadopago.parse),
]


def guess_account(filename: str) -> str | None:
    name = norm(filename)
    if "mercado" in name or name.startswith("mp") or "account statement" in name:
        return "mercadopago"
    if any(w in name for w in ("visa", "master", "tarjeta", "resumen")):
        return "bbva_tarjeta"
    if "bbva" in name or "movimientos" in name:
        return "bbva"
    return None


def parse_file(filename: str, data: bytes, account: str | None) -> tuple[str, list[Row]]:
    for acc, detect, parse in SPECIFIC:
        if (account in (None, acc)) and detect(filename, data):
            return acc, parse(filename, data)
    if filename.lower().endswith(".pdf"):
        raise ImportError_("Todavía no sé leer este PDF. Por ahora leo el resumen de la tarjeta BBVA; "
                           "para otros, bajá el Excel o CSV.")
    account = account or guess_account(filename)
    if not account:
        raise ImportError_("No sé de qué cuenta es este archivo: elegila en el formulario.")
    rows = generic.parse(filename, data)
    if account == "bbva_tarjeta":
        # Card statements list purchases as positive charges; here money out is negative.
        for row in rows:
            row.amount = -row.amount
    return account, rows
