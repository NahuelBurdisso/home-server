"""BBVA's "Ya podés descargar tu resumen de tarjeta" mail: no PDF, but it says when the card is due
and how much. Finanzas reads it from Gmail as soon as it arrives (app/correo.py), so the due date and
the amount are known before the PDF (the detail) is shared with Finanzas."""
import html
import re
import sqlite3
from datetime import date

from .db import ACCOUNTS
from .money import ars, parse_amount, usd

ACCOUNT = "bbva_tarjeta"  # the only card

# An amount with its currency, if any, and a minus touching the digits or the currency (a balance
# in your favor: '$ -5.000,00', '-$ 5.000,00', '5.000,00-'); parse_amount reads the sign. A dash with
# spaces around it is a separator, not a sign.
_AMOUNT = r"((?:-?(?:u\$s|us\$|u\$d|usd|\$)\s*)?-?[\d.,]*\d-?)"
PATTERNS = {
    "due": r"vencimiento\s*:?\s*(\d{1,2})[-/](\d{1,2})[-/](\d{4})",
    "total": r"saldo en pesos\s*:?\s*" + _AMOUNT,
    "total_usd": r"saldo en d[oó]lares\s*:?\s*" + _AMOUNT,
    "minimum": r"pago m[ií]nimo\s*:?\s*" + _AMOUNT,
}
CARD = re.compile(r"tarjeta|visa|master|cr[eé]dito")


def plain(text: str) -> str:
    """The mail as one line of text, whether n8n sent its text part or its HTML."""
    text = re.sub(r"(?is)<(style|script)\b.*?</\1>", " ", text)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text).strip()


def parse(text: str) -> dict | None:
    """{'due', 'total', 'total_usd', 'minimum'} (centavos; US$ centavos), or None when the text isn't
    a statement notice. Only the due date and the pesos are required."""
    t = plain(text).lower()
    found = {k: re.search(p, t) for k, p in PATTERNS.items()}
    if not (found["due"] and found["total"]):
        return None
    d, m, y = (int(x) for x in found["due"].groups())
    try:
        due = date(y, m, d)
    except ValueError:
        return None
    out = {"due": due}
    for key in ("total", "total_usd", "minimum"):
        out[key] = parse_amount(found[key].group(1)) if found[key] else None
    return out


def about_card(text: str) -> bool:
    """Whether a BBVA mail is about the card (its notice for the account's statement isn't)."""
    return bool(CARD.search(plain(text).lower()))


def record(conn: sqlite3.Connection, notice: dict, received: date) -> tuple[str, dict | None]:
    """A notice from the mail, wherever it came from: 'pdf' when that statement's PDF is already
    loaded (it counts; nothing saved), 'again' when the notice was known (updated), 'new'; and what
    was saved. Only a 'new' one is worth a notification."""
    due = notice["due"].isoformat()
    if conn.execute("SELECT 1 FROM statements WHERE account = ? AND due = ?", (ACCOUNT, due)).fetchone():
        return "pdf", None
    known = conn.execute("SELECT 1 FROM card_notices WHERE account = ? AND due = ?", (ACCOUNT, due)).fetchone()
    return ("again" if known else "new"), store(conn, notice, received)


def store(conn: sqlite3.Connection, notice: dict, received: date) -> dict:
    """Saves it (the same notice again only updates it). Its close is the one the last PDF announced,
    when the notice came soon after it; unknown otherwise."""
    latest = conn.execute("SELECT next_close FROM statements WHERE account = ? AND next_close IS NOT NULL"
                          " ORDER BY close DESC LIMIT 1", (ACCOUNT,)).fetchone()
    close = None
    if latest:
        nxt = date.fromisoformat(latest["next_close"])
        if nxt <= received and 0 < (notice["due"] - nxt).days <= 20:
            close = nxt.isoformat()
    conn.execute("INSERT INTO card_notices (account, due, close, total, total_usd, minimum, received)"
                 " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (account, due) DO UPDATE SET close = excluded.close,"
                 " total = excluded.total, total_usd = excluded.total_usd, minimum = excluded.minimum,"
                 " received = excluded.received",
                 (ACCOUNT, notice["due"].isoformat(), close, notice["total"], notice["total_usd"],
                  notice["minimum"], received.isoformat()))
    conn.commit()
    return {**notice, "close": close}


def message(notice: dict) -> str:
    """One line for the phone notification."""
    amounts = " + ".join(filter(None, [ars(notice["total"]),
                                       usd(notice["total_usd"] / 100) if notice["total_usd"] else None]))
    text = f"{ACCOUNTS[ACCOUNT]}: vence el {notice['due']:%d/%m}, {amounts}"
    if notice["minimum"] is not None:
        text += f" (mínimo {ars(notice['minimum'])})"
    return text + ". Para el detalle, bajá el PDF y compartilo con Finanzas."


def upcoming(conn: sqlite3.Connection, today: str) -> list[dict]:
    """Card payments still due: each loaded statement, plus each notice whose PDF hasn't come yet."""
    rows = conn.execute(
        "SELECT account, close, due, total, total_usd, minimum, 1 AS pdf FROM statements WHERE due >= ?"
        " UNION ALL SELECT account, close, due, total, total_usd, minimum, 0 FROM card_notices n WHERE due >= ?"
        " AND NOT EXISTS (SELECT 1 FROM statements s WHERE s.account = n.account AND s.due = n.due)"
        " ORDER BY due", (today, today)).fetchall()
    return [dict(r) for r in rows]
