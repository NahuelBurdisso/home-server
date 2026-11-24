"""Mercado Pago by itself, with no file to download: every morning the app asks Mercado Pago for its
'available money' report (release report) of the days since the last one, up to yesterday, and saves
those movements like an uploaded file. So a movement shows up the day after, once the report is made.

The report has every movement of the balance (payments, transfers in and out, money loaded from the
bank, the savings pocket, interest) and the balance after each one, but no names. The payments API
adds them where it has them: shops, Mercado Libre products, subscriptions, who sent you money.
Transfers to a bank account arrive as 'Transferencia enviada' with no name: Mercado Pago doesn't give it.

Needs the production Access Token of an app of yours (Mercado Pago Developers → Tus integraciones →
Credenciales de producción) in secrets/mercadopago-token. Mercado Pago takes hours to make a report,
so a round asks for it and a later round (every 10 minutes) picks it up."""
import csv
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from . import db, ledger
from .importers import Row
from .money import parse_amount

TOKEN_FILE = os.environ.get("FINANZAS_MP_TOKEN_FILE", "/run/secrets/mercadopago-token")
API = "https://api.mercadopago.com"
SOURCE = "Mercado Pago (automático)"   # the imports row, shown in Cargar
TZ = timezone(timedelta(hours=-3))     # Argentina, no daylight saving
ASK_FROM = 6                           # o'clock: one report a day (each one is a file in your account)
GIVE_UP = timedelta(hours=20)          # a report that never shows up is asked for again
SKIP = ("reserve_for_payment", "reserve_for_payout")  # held and released in the same second: nets to zero


def token() -> str | None:
    try:
        return open(TOKEN_FILE).read().strip() or None
    except OSError:
        return None


def _call(tok: str, method: str, path: str, body: dict | None = None, raw: bool = False):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode() if body is not None else None,
                                 method=method, headers={"Authorization": f"Bearer {tok}",
                                                         "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    return data if raw else json.loads(data or b"null")


# --- the report and its names (pure, tested) -------------------------------------------

@dataclass
class Move:
    source_id: str
    type: str          # DESCRIPTION: payment, refund, payout, payouts_cash, asset_management...
    when: datetime
    amount: int        # centavos, negative = money out
    balance: int       # available money after it


def parse_release(text: str) -> list[Move]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")), delimiter=";")
    out = []
    for r in reader:
        kind = (r.get("DESCRIPTION") or "").strip()
        if not kind or kind in SKIP:  # the opening and closing balance lines have no description
            continue
        amount = parse_amount(r["NET_CREDIT_AMOUNT"] or "0") - parse_amount(r["NET_DEBIT_AMOUNT"] or "0")
        if amount:
            out.append(Move(r["SOURCE_ID"].strip(), kind, datetime.fromisoformat(r["DATE"]).astimezone(TZ), amount,
                            parse_amount(r["BALANCE_AMOUNT"] or "0")))
    return out


def _merchant(p: dict) -> str:
    text = " ".join((p.get("description") or p.get("statement_descriptor") or "").split())
    return re.sub(r"^producto de ", "", text, flags=re.I)


def _who(p: dict) -> str:
    payer = p.get("payer") or {}
    name = " ".join(filter(None, (payer.get("first_name"), payer.get("last_name"))))
    return name or (payer.get("email") or "").split("@")[0]


def describe(move: Move, p: dict | None) -> str:
    """Worded like Mercado Pago's own account report, so the rules (savings pocket, money loaded
    from the bank, refunds) and what you taught the app keep working."""
    if move.type == "asset_management":
        return "Rendimientos"
    if move.type == "payouts_cash":
        return "Retiro de dinero"
    if move.type == "payout":
        return "Transferencia enviada"
    if p is None:
        return "Movimiento de Mercado Pago"
    op = p.get("operation_type")
    merchant = _merchant(p)
    if move.type == "refund":
        return f"Devolución de pago {merchant or 'Mercado Pago'}"
    if op == "partition_transfer":
        return "Reserva por gastos Ahorro" if move.amount < 0 else "Dinero retirado Ahorro"
    if op == "account_fund":
        return "Ingreso de dinero"
    if op == "money_transfer":
        return f"Transferencia recibida {_who(p)}".strip() if move.amount > 0 else "Transferencia enviada"
    if move.amount > 0:
        return f"Cobro {merchant}".strip()
    sub_unit = ((p.get("point_of_interaction") or {}).get("business_info") or {}).get("sub_unit")
    if op == "recurring_payment" or sub_unit == "subscription":
        return "Pago de suscripción " + re.sub(r"^suscripci[oó]n a ", "", merchant, flags=re.I)
    if sub_unit == "qr":
        return f"Pago con QR {merchant}"
    if (p.get("order") or {}).get("type") == "mercadolibre":
        return f"Compra en Mercado Libre: {merchant}" if merchant else "Compra en Mercado Libre"
    return f"Pago {merchant or 'Mercado Pago'}"


def rows_from(moves: list[Move], payments: dict[str, dict]) -> list[Row]:
    seen = Counter()
    rows = []
    for m in moves:
        base = f"mp:{m.source_id}:{m.type}:{'+' if m.amount > 0 else '-'}"
        seen[base] += 1
        rows.append(Row(m.when.date(), describe(m, payments.get(m.source_id)), m.amount, key=f"{base}:{seen[base]}"))
    return rows


# --- talking to Mercado Pago -----------------------------------------------------------

def _utc(day: str) -> str:
    """Midnight in Argentina, as the API wants it."""
    return datetime.fromisoformat(day).replace(tzinfo=TZ).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _payments(tok: str, since: str, ids: set[str]) -> dict[str, dict]:
    """The payments behind the report's lines, for their names: a search from a little before,
    then one by one whatever the search didn't bring (payments made long before they were released)."""
    out, offset = {}, 0
    while True:
        page = _call(tok, "GET", f"/v1/payments/search?sort=date_created&criteria=asc&range=date_created"
                                 f"&begin_date={_utc((date.fromisoformat(since) - timedelta(days=2)).isoformat())}"
                                 f"&end_date=NOW&limit=100&offset={offset}")
        out.update({str(p["id"]): p for p in page.get("results", [])})
        offset += 100
        if offset >= min(page.get("paging", {}).get("total", 0), 2000):
            break
    for pid in ids - out.keys():
        try:
            out[pid] = _call(tok, "GET", f"/v1/payments/{pid}")
        except urllib.error.HTTPError:
            pass  # interest, payouts: not payments
    return out


def _state(conn) -> dict:
    row = conn.execute("SELECT value FROM meta WHERE key = 'mp_sync'").fetchone()
    return json.loads(row[0]) if row else {}


def _save(conn, state: dict) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('mp_sync', ?)", (json.dumps(state),))
    conn.commit()


def status(conn) -> dict:
    """For the screens: when it last brought movements, Mercado Pago's own balance then, any error."""
    return {**_state(conn), "on": bool(token())}


def _clear(state: dict) -> None:
    for k in ("task", "asked", "asked_from", "asked_to"):
        state.pop(k, None)


def sync(conn, now: datetime | None = None) -> str:
    """One round: picks up the report asked for earlier, or asks for today's. Returns what it did."""
    tok = token()
    if not tok:
        return "off"
    now = now or datetime.now(TZ)
    today = now.date()
    state = _state(conn)
    if "from" not in state:
        # Right after the last movement already loaded by hand (the day after), or this month.
        last = conn.execute("SELECT MAX(date) FROM transactions WHERE account = 'mercadopago'").fetchone()[0]
        state["from"] = (date.fromisoformat(last) + timedelta(days=1)).isoformat() if last else today.isoformat()[:8] + "01"
    try:
        if state.get("task"):
            task = _call(tok, "GET", f"/v1/account/release_report/task/{state['task']}")
            if task.get("file_name"):
                text = _call(tok, "GET", f"/v1/account/release_report/{task['file_name']}", raw=True).decode("utf-8-sig")
                moves = parse_release(text)
                fresh = [m for m in moves if not conn.execute(
                    "SELECT 1 FROM transactions WHERE dedupe_key LIKE ?", (f"mp:{m.source_id}:{m.type}:%",)).fetchone()]
                new = 0
                if fresh:
                    payments = _payments(tok, state["asked_from"], {m.source_id for m in fresh})
                    new = ledger.store(conn, SOURCE, "mercadopago", rows_from(moves, payments))["new"]
                if moves:
                    state["balance"], state["balance_at"] = moves[-1].balance, moves[-1].when.isoformat()
                next_day = (date.fromisoformat(state["asked_to"]) + timedelta(days=1)).isoformat()
                state.update({"from": next_day, "ok": now.isoformat(), "new": new, "error": None})
                _clear(state)
                _save(conn, state)
                return f"saved {new}"
            if now - datetime.fromisoformat(state["asked"]) < GIVE_UP:
                _save(conn, state)
                return "waiting"
            state["error"] = f"Mercado Pago no terminó el reporte ({task.get('status')})"
            _clear(state)
        yesterday = (today - timedelta(days=1)).isoformat()
        asked_today = (state.get("last_ask") or "")[:10] == today.isoformat()
        if now.hour < ASK_FROM or asked_today or state["from"] > yesterday:
            _save(conn, state)
            return "idle"
        end = (datetime.fromisoformat(today.isoformat()).replace(tzinfo=TZ) - timedelta(seconds=1)).astimezone(timezone.utc)
        task = _call(tok, "POST", "/v1/account/release_report",
                     {"begin_date": _utc(state["from"]), "end_date": end.strftime("%Y-%m-%dT%H:%M:%SZ")})
        state.update({"task": task["id"], "asked": now.isoformat(), "asked_from": state["from"], "asked_to": yesterday,
                      "last_ask": now.isoformat()})
        _save(conn, state)
        return "asked"
    except Exception as e:  # no internet, Mercado Pago down, a revoked token: try again next round
        conn.rollback()
        state["error"] = f"{type(e).__name__}: {getattr(e, 'code', '') or e}"[:200]
        _save(conn, state)
        return "error"


def _loop():
    from . import classifier
    while True:
        conn = db.connect()
        try:
            if sync(conn).startswith("saved") and _state(conn).get("new"):
                classifier.suggest_pending()
        except Exception:
            pass
        finally:
            conn.close()
        time.sleep(600)


def start() -> None:
    if token():
        threading.Thread(target=_loop, name="mpsync", daemon=True).start()
