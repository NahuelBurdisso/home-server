"""Pagos del mes: what the house pays every month (power, gas, water, the city tax, the house itself),
with what each payment page asks for, and whether this month's is paid.

A bill is paid in a month when an expense dated that month has one of its `match` words in the
description (the bank or Mercado Pago brought it), or when it was marked by hand (the page, the bot,
Siri, or the "Hecho" of its reminder). Marking by hand never writes a movement: the house instalment
already counts through "Todos los meses, en dólares", and a cash payment written down as a quick
expense matches by itself.

Finanzas keeps one reminder per bill in Recordatorios for the current month (sync_reminders), like
Papeles does for its papers; only with FINANZAS_PAGOS_AVISOS=1."""
import json
import math
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import date

from . import db, notify
from .importers import norm
from .money import MONTHS, ars, month_name, shift_month, usd

RECORDATORIOS = os.environ.get("RECORDATORIOS_URL", "http://host.docker.internal:3014")
TIMEOUT = 4  # seconds: Recordatorios runs on this Mac; a page never waits long for it
EVERY = 600  # seconds between reminder rounds

# Words around a bill's name in "ya pagué la cuota de la casa de noviembre".
# How it was paid isn't part of what was paid: "pagué la luz con la tarjeta" is still the light.
FILLER = {"ya", "pague", "pagamos", "pago", "pagado", "pagada", "la", "el", "los", "las", "de", "del", "mi", "mis",
          "cuota", "factura", "boleta", "este", "mes", "hoy", "ayer", "recien", "todo", "toda", "con", "por", "desde",
          "tarjeta", "visa", "debito", "credito", "efectivo", "transferencia", "mercado", "home", "banking", "app",
          *MONTHS}
FIELDS = ("name", "emoji", "details", "url", "notes", "day", "match", "amount_usd", "total_usd", "paid_before_usd",
          "start_month", "active")
# A change to any of these makes this month's reminder stale (its title, notes or date).
REMINDER_FIELDS = ("name", "details", "url", "day", "active", "amount_usd", "start_month")


# ---- bills ------------------------------------------------------------------------------------

def _check(day, start_month: str) -> None:
    if not isinstance(day, int) or not 1 <= day <= 28:
        raise ValueError("El día tiene que ser entre 1 y 28.")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", start_month or ""):
        raise ValueError("Falta el mes desde el que se paga.")


def add(conn: sqlite3.Connection, name: str, day: int, start_month: str, *, emoji: str = "🧾", details: str = "",
        url: str = "", notes: str = "", match: str = "", amount_usd: int | None = None, total_usd: int | None = None,
        paid_before_usd: int = 0) -> int:
    """The new bill's id. ValueError (a sentence for the person) when the day or month is wrong."""
    _check(day, start_month)
    if not name.strip():
        raise ValueError("Falta el nombre.")
    cur = conn.execute(
        "INSERT INTO bills (name, emoji, details, url, notes, day, match, amount_usd, total_usd, paid_before_usd,"
        " start_month) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name.strip(), emoji or "🧾", details.strip(), url.strip(), notes.strip(), day, match.strip(), amount_usd,
         total_usd, paid_before_usd or 0, start_month))
    conn.commit()
    return cur.lastrowid


def get(conn: sqlite3.Connection, bill_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()


def all_bills(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM bills ORDER BY active DESC, day, id").fetchall()


def update(conn: sqlite3.Connection, bill_id: int, today: date | None = None, **fields) -> None:
    """Changes a bill. When what its reminder says changed (or it was turned off), this month's
    reminder goes, so the next round makes a fresh one if it's still unpaid."""
    old = get(conn, bill_id)
    if old is None:
        return
    fields = {k: v for k, v in fields.items() if k in FIELDS}
    _check(fields.get("day", old["day"]), fields.get("start_month", old["start_month"]))
    if not fields:
        return
    conn.execute(f"UPDATE bills SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), bill_id))
    conn.commit()
    if any(k in fields and fields[k] != old[k] for k in REMINDER_FIELDS):
        drop_reminder(conn, bill_id, (today or date.today()).isoformat()[:7])


def delete(conn: sqlite3.Connection, bill_id: int, today: date | None = None) -> None:
    drop_reminder(conn, bill_id, (today or date.today()).isoformat()[:7])
    conn.execute("DELETE FROM bills WHERE id = ?", (bill_id,))
    conn.commit()


def short(bill) -> str:
    """'Luz (EPEC)' -> 'luz'."""
    return bill["name"].split(" (")[0].strip().lower()


def lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def detail_lines(bill) -> list[str]:
    return [line.strip() for line in (bill["details"] or "").splitlines() if line.strip()]


def money(bill, cents: int | None) -> str | None:
    """An amount the way the app writes it: dollars for a dollar bill, pesos otherwise."""
    if cents is None:
        return None
    return usd(cents / 100) if bill["amount_usd"] is not None else ars(cents)


# ---- paid or not ------------------------------------------------------------------------------

def mark_paid(conn: sqlite3.Connection, bill_id: int, month: str, paid_on: date, amount: int | None = None,
              via: str = "manual") -> None:
    """Paid by hand (or by its reminder's "Hecho"). Never writes a movement."""
    conn.execute("INSERT INTO bill_months (bill_id, month, paid_on, paid_via, amount) VALUES (?, ?, ?, ?, ?)"
                 " ON CONFLICT (bill_id, month) DO UPDATE SET paid_on = excluded.paid_on,"
                 " paid_via = excluded.paid_via, amount = excluded.amount",
                 (bill_id, month, paid_on.isoformat(), via, amount))
    conn.commit()


def unmark(conn: sqlite3.Connection, bill_id: int, month: str) -> None:
    """Takes back a hand mark (a movement that pays it still does). Its reminder goes too: it may
    already say done, and the next round makes a fresh one if it's still unpaid."""
    row = conn.execute("SELECT paid_on FROM bill_months WHERE bill_id = ? AND month = ?", (bill_id, month)).fetchone()
    if row is None or row["paid_on"] is None:
        return
    conn.execute("UPDATE bill_months SET paid_on = NULL, paid_via = NULL, amount = NULL WHERE bill_id = ? AND month = ?",
                 (bill_id, month))
    conn.commit()
    drop_reminder(conn, bill_id, month)


def terms(bill) -> list[str]:
    return [t for t in (norm(x) for x in (bill["match"] or "").split(",")) if t]


def _matcher(words: list[str]):
    """Words are found anywhere ('municipalidad' in 'merpago*municipalidaddeco'); an account number only
    as a whole number, so '123456' doesn't turn up inside some other payment's long operation code."""
    return re.compile("|".join(rf"(?<!\d){re.escape(w)}(?!\d)" if w.isdigit() else re.escape(w) for w in words))


def _payments(conn, bills, lo: str, hi: str) -> dict:
    """{(bill id, month): [movements]} for the months lo..hi, by the day each was paid (not a card
    statement's month). Only pesos bills with match words: a dollar one is paid by hand."""
    termed = [(b["id"], _matcher(terms(b))) for b in bills if b["amount_usd"] is None and terms(b)]
    if not termed:
        return {}
    out = {}
    for r in conn.execute("SELECT id, date, description, amount FROM transactions WHERE kind = 'expense'"
                          " AND amount < 0 AND date >= ? AND date < ? ORDER BY date, id",
                          (lo + "-01", shift_month(hi, 1) + "-01")):
        text = norm(r["description"])
        for bid, words in termed:
            if words.search(text):
                out.setdefault((bid, r["date"][:7]), []).append(r)
    return out


def _marks(conn, lo: str, hi: str) -> dict:
    return {(r["bill_id"], r["month"]): r for r in conn.execute(
        "SELECT * FROM bill_months WHERE month >= ? AND month <= ?", (lo, hi))}


def _entry(bill, month: str, pays: dict, marks: dict, today: date) -> dict:
    movs = pays.get((bill["id"], month), [])
    mark = marks.get((bill["id"], month))
    hand = bool(mark and mark["paid_on"])
    paid_on, amount, via, tx_id = None, None, None, None
    if movs:  # a movement wins over a hand mark for what is shown
        paid_on, amount, via, tx_id = date.fromisoformat(movs[0]["date"]), -sum(r["amount"] for r in movs), \
            "movimiento", movs[0]["id"]
    elif hand:
        paid_on, via = date.fromisoformat(mark["paid_on"]), mark["paid_via"]
        amount = mark["amount"] if mark["amount"] is not None else bill["amount_usd"]
    due = date(int(month[:4]), int(month[5:7]), bill["day"])
    return {
        "bill": bill, "month": month, "paid": paid_on is not None, "paid_on": paid_on, "amount": amount, "via": via,
        "tx_id": tx_id, "due": due, "manual": hand,
        "late": paid_on is None and month == today.isoformat()[:7] and today > due,
        "reminder_id": mark["reminder_id"] if mark else None,
        "reminder_closed": bool(mark and mark["reminder_closed"]),
    }


def due_bills(conn, month: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM bills WHERE active = 1 AND start_month <= ? ORDER BY day, id", (month,)).fetchall()


def month_status(conn: sqlite3.Connection, month: str, today: date | None = None, bills=None) -> list[dict]:
    """Each bill due in that month: paid or not, when, how much, and the last earlier amount
    ("último: $ 18.656 (octubre)", looking back up to a year)."""
    today = today or date.today()
    bills = due_bills(conn, month) if bills is None else bills
    if not bills:
        return []
    lo = shift_month(month, -12)
    pays, marks = _payments(conn, bills, lo, month), _marks(conn, lo, month)
    out = []
    for b in bills:
        e = _entry(b, month, pays, marks, today)
        e["last"] = None
        m = shift_month(month, -1)
        while m >= max(lo, b["start_month"]):  # an older payment to the same place was for something else
            prev = _entry(b, m, pays, marks, today)
            if prev["paid"] and prev["amount"]:
                e["last"] = {"amount": prev["amount"], "month": m}
                break
            m = shift_month(m, -1)
        out.append(e)
    return out


def status_of(conn, bill_id: int, month: str, today: date | None = None) -> dict | None:
    """One bill in one month, due that month or not."""
    bill = get(conn, bill_id)
    return month_status(conn, month, today, bills=[bill])[0] if bill else None


def history(conn, bill_id: int, today: date | None = None, months: int = 12) -> list[dict]:
    """The bill's last months, newest first: from when it started (earlier ones only if paid)."""
    today = today or date.today()
    bill = get(conn, bill_id)
    if bill is None:
        return []
    hi = today.isoformat()[:7]
    lo = shift_month(hi, -(months - 1))
    pays, marks = _payments(conn, [bill], lo, hi), _marks(conn, lo, hi)
    out = []
    for i in range(months):
        e = _entry(bill, shift_month(hi, -i), pays, marks, today)
        if e["paid"] or e["month"] >= bill["start_month"]:
            out.append(e)
    return out


def progress(conn, bill) -> dict | None:
    """Paying something off (total_usd): what was paid so far and what's left, in US$ centavos."""
    if not bill["total_usd"]:
        return None
    paid = bill["paid_before_usd"] or 0
    for r in conn.execute("SELECT amount FROM bill_months WHERE bill_id = ? AND paid_on IS NOT NULL", (bill["id"],)):
        paid += r["amount"] if r["amount"] is not None else (bill["amount_usd"] or 0)
    total = bill["total_usd"]
    left = max(total - paid, 0)
    pct = round(paid / total * 100)
    cuotas = math.ceil(left / bill["amount_usd"]) if bill["amount_usd"] and left else None
    text = f"Pagaste {usd(paid / 100)} de {usd(total / 100)} ({pct}%) · faltan {usd(left / 100)}"
    if cuotas:
        text += f" · ≈ {cuotas} cuota{'s' if cuotas != 1 else ''}"
    return {"paid": paid, "total": total, "left": left, "pct": min(pct, 100), "cuotas": cuotas, "text": text}


# ---- what's left this month, in a sentence ------------------------------------------------------

def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " y " + parts[-1]


def summary(conn: sqlite3.Connection, today: date | None = None) -> dict | None:
    """This month's bills for /api/estado (Hoy) and the bot; None when no bill is due this month."""
    today = today or date.today()
    month = today.isoformat()[:7]
    items = month_status(conn, month, today)
    if not items:
        return None
    left = [e for e in items if not e["paid"]]
    name = month_name(month, year=False)
    paid = len(items) - len(left)
    if not left:
        frase = f"Pagos de {name}: los {len(items)} hechos." if len(items) > 1 else f"Pagos de {name}: el único, hecho."
    else:
        parts = [f"{short(e['bill'])} ({'venció el' if e['late'] else 'el'} {e['due'].day})" for e in left]
        frase = (f"Pagos de {name}: {paid} de {len(items)} {'hecho' if paid == 1 else 'hechos'}. "
                 f"{'Faltan' if len(left) > 1 else 'Falta'} {_join(parts)}.")
    return {
        "mes": month, "nombre": name, "total": len(items), "pagados": paid,
        "faltan": [{"id": e["bill"]["id"], "nombre": e["bill"]["name"], "corto": short(e["bill"]),
                    "dia": e["bill"]["day"], "vence": e["due"].isoformat(), "atrasado": e["late"]} for e in left],
        "frase": frase,
    }


def sentence(conn, today: date | None = None) -> str:
    s = summary(conn, today)
    return s["frase"] if s else "No hay pagos fijos cargados."


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", norm(text))


def _close(a: str, b: str) -> bool:
    return a == b or (min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a)))


def find(conn: sqlite3.Connection, text: str) -> sqlite3.Row:
    """The bill a person means ("pagué la luz", "muni", "la cuota de la casa"); ValueError with a
    sentence when none or more than one fits. Every word left after the filler has to be the bill's:
    "pagué el almuerzo de mis viejos" isn't the house instalment just because its name says "viejos"."""
    wanted = [w for w in _words(text) if w not in FILLER and len(w) >= 3]
    bills = conn.execute("SELECT * FROM bills WHERE active = 1 ORDER BY day, id").fetchall()
    hits = []
    for b in bills:
        keys = set(_words(b["name"])) | {w for t in terms(b) for w in _words(t)}
        if wanted and all(any(_close(w, k) for k in keys) for w in wanted):
            hits.append(b)
    if len(hits) == 1:
        return hits[0]
    if not bills:
        raise ValueError("No hay pagos fijos cargados.")
    raise ValueError("No encontré ese pago. Tengo: " + ", ".join(short(b) for b in bills) + ".")


# ---- reminders in Recordatorios ---------------------------------------------------------------

def request(method: str, path: str, body: dict | None = None) -> tuple[int, dict | None]:
    """(status, JSON answer or None). A 4xx comes back as its status; Recordatorios down raises."""
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request(RECORDATORIOS + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if body is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        return e.code, None
    try:
        return status, json.loads(raw)
    except ValueError:  # a page (after a redirect) or plain text
        return status, None


def enabled() -> bool:
    return os.environ.get("FINANZAS_PAGOS_AVISOS") == "1"


def _save_reminder(conn, bill_id: int, month: str, rid: int | None, closed: int = 0) -> None:
    conn.execute("INSERT INTO bill_months (bill_id, month, reminder_id, reminder_closed) VALUES (?, ?, ?, ?)"
                 " ON CONFLICT (bill_id, month) DO UPDATE SET reminder_id = excluded.reminder_id,"
                 " reminder_closed = excluded.reminder_closed", (bill_id, month, rid, closed))
    conn.commit()


def drop_reminder(conn, bill_id: int, month: str) -> None:
    """Deletes that month's reminder in Recordatorios (as Papeles does) and forgets it."""
    row = conn.execute("SELECT reminder_id FROM bill_months WHERE bill_id = ? AND month = ?", (bill_id, month)).fetchone()
    if not row or not row["reminder_id"]:
        return
    try:
        request("POST", f"/r/{row['reminder_id']}/borrar")
    except Exception:
        pass  # Recordatorios down: the stale one links here, where the bill shows what's true
    conn.execute("UPDATE bill_months SET reminder_id = NULL, reminder_closed = 0 WHERE bill_id = ? AND month = ?",
                 (bill_id, month))
    conn.commit()


def reminder_body(bill, due: date, today: date) -> dict:
    title = "Pagar " + lower_first(bill["name"])
    if bill["amount_usd"] is not None:
        title += f" ({usd(bill['amount_usd'] / 100)})"
    # The first link in the notes is what the notification opens: where it's paid, when there is one.
    notes = [" · ".join(detail_lines(bill)), bill["url"].strip(),
             f"Al pagarlo tocá Hecho. Detalle: {notify.APP_URL}/pagos/{bill['id']}"]
    return {"titulo": title, "fecha": max(due, today).isoformat(), "avisos": [1440, 0],
            "notas": "\n".join(n for n in notes if n)}


def _sync_one(conn, e: dict, today: date) -> None:
    bill, month, rid = e["bill"], e["month"], e["reminder_id"]
    if e["paid"]:
        if rid and not e["reminder_closed"]:
            status, r = request("GET", f"/api/r/{rid}")
            if status == 200 and r and not r.get("hecho"):
                status, _ = request("POST", f"/api/r/{rid}/hecho?occ={r['fecha']}T09:00")
            if status in (200, 404):
                _save_reminder(conn, bill["id"], month, rid, 1)
        return
    if rid:
        if e["reminder_closed"]:
            return  # told done when it was paid; a movement that went away doesn't bring it back
        status, r = request("GET", f"/api/r/{rid}")
        if status == 200 and r and r.get("hecho"):
            mark_paid(conn, bill["id"], month, today, via="recordatorio")
            _save_reminder(conn, bill["id"], month, rid, 1)
        return  # pending, or 404: the person deleted it, and it stays deleted
    status, r = request("POST", "/api/recordatorio", reminder_body(bill, e["due"], today))
    if status == 200 and r and r.get("id"):
        _save_reminder(conn, bill["id"], month, int(r["id"]))


def sync_reminders(conn: sqlite3.Connection, today: date | None = None) -> None:
    """One reminder per bill due this month: made while unpaid, told done once paid; its "Hecho"
    marks the bill paid. Never raises: Recordatorios down only means the next round tries again."""
    today = today or date.today()
    try:
        items = month_status(conn, today.isoformat()[:7], today)
    except Exception:
        return
    for e in items:
        try:
            _sync_one(conn, e, today)
        except Exception:
            continue


def sync_now() -> None:
    """Right after a change (as a background task), so the reminder follows at once."""
    if not enabled():
        return
    try:
        conn = db.connect()
    except Exception:
        return
    try:
        sync_reminders(conn, date.today())
    finally:
        conn.close()


def _loop():
    while True:
        sync_now()
        time.sleep(EVERY)


def start() -> None:
    if enabled():
        threading.Thread(target=_loop, name="pagos", daemon=True).start()
