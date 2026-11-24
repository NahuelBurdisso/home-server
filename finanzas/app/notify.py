"""Phone notifications through the server's ntfy, checked once an hour (9 to 22 h):

- Monthly summary of last month, as soon as its data is complete (Mercado Pago loaded and a
  card statement that closes after the month ends), or on the 20th anyway, marked provisional.
- Reminder to share the card statement, 3 days after the card closes, if it isn't loaded.
- Reminder for last month's Mercado Pago report, from the 3rd, if it's missing (only when it isn't
  loaded by itself); with the automatic load, a warning when nothing arrived for 2 days.
- A monthly bill (Pagos del mes) still unpaid 3 days after its day.

Each one is sent once (remembered in meta). Enabled with FINANZAS_NOTIFY=1; the topic is read
from the mounted secret, like the server's other alerts."""
import json
import os
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta

from . import db, reports
from .money import ars, month_name, shift_month, usd

NTFY_URL = os.environ.get("FINANZAS_NTFY_URL", "http://host.docker.internal:8091")
TOPIC_FILE = os.environ.get("FINANZAS_NTFY_TOPIC_FILE", "/run/secrets/ntfy-topic")
APP_URL = os.environ.get("FINANZAS_URL", "https://mac-server.tailnet-ejemplo.ts.net:3013")


def send(title: str, body: str, path: str = "/", tags: str = "moneybag") -> None:
    """JSON publishing, so accents survive (HTTP headers can't carry them)."""
    topic = open(TOPIC_FILE).read().strip()
    msg = {"topic": topic, "title": title, "message": body, "tags": [tags], "click": APP_URL + path}
    req = urllib.request.Request(NTFY_URL, json.dumps(msg).encode(), {"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)


def _once(conn, key: str) -> bool:
    """True the first time a key is seen; then remembered."""
    if conn.execute("SELECT 1 FROM meta WHERE key = ?", (f"sent:{key}",)).fetchone():
        return False
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (f"sent:{key}", datetime.now().isoformat()))
    conn.commit()
    return True


def _last_day(month: str) -> str:
    return (date.fromisoformat(shift_month(month, 1) + "-01") - timedelta(days=1)).isoformat()


def due_messages(conn, today: date) -> list[tuple[str, str, str, str, str]]:
    """(key, title, body, path, tags) for every message due today; pure, so it can be tested."""
    out = []
    last = shift_month(today.isoformat()[:7], -1)
    has_mp = conn.execute("SELECT 1 FROM transactions WHERE account = 'mercadopago' AND month = ?", (last,)).fetchone()
    latest = conn.execute("SELECT close, next_close FROM statements WHERE account = 'bbva_tarjeta'"
                          " ORDER BY close DESC LIMIT 1").fetchone()
    card_ok = (latest["close"] >= _last_day(last)) if latest else bool(conn.execute(
        "SELECT 1 FROM transactions WHERE account = 'bbva_tarjeta' AND month = ?", (last,)).fetchone())

    if latest and latest["next_close"]:
        nxt = date.fromisoformat(latest["next_close"])
        if today >= nxt + timedelta(days=3):
            out.append((f"card:{nxt}", "Resumen de la tarjeta",
                        f"La tarjeta cerró el {nxt:%d/%m}. Cuando te llegue el resumen por mail, "
                        "abrí el PDF y tocá Compartir → Finanzas.", "/cargar", "credit_card"))
    from . import mpsync
    mp = mpsync.status(conn)
    if mp["on"]:
        last_ok = date.fromisoformat((mp.get("ok") or mp.get("last_ask") or today.isoformat())[:10])
        if today - last_ok >= timedelta(days=2):
            out.append((f"mpsync:{last_ok}", "Mercado Pago no se está cargando",
                        f"Desde el {last_ok:%d/%m} no llegan movimientos de Mercado Pago"
                        + (f" ({mp['error']})" if mp.get("error") else "") + ". Si cambiaste el token, "
                        "actualizalo en secrets/mercadopago-token; mientras, podés subir el reporte a mano.",
                        "/cargar", "warning"))
    elif today.day >= 3 and not has_mp and conn.execute("SELECT 1 FROM transactions LIMIT 1").fetchone():
        out.append((f"mp:{last}", "Falta Mercado Pago",
                    f"Bajá el reporte de cuenta de Mercado Pago de {month_name(last, year=False)} "
                    "y compartilo a Finanzas.", "/cargar", "inbox_tray"))
    # A budget passed this month (or last month, once its data is complete): once per group and month.
    for month in (today.isoformat()[:7], last):
        for b in reports.budget_status(conn, month):
            if b["pct"] >= 100:
                out.append((f"budget:{month}:{b['id']}", f"Te pasaste en {b['label'].lower()}",
                            f"{month_name(month).capitalize()}: {ars(b['spent'])} de {ars(b['limit'])} ({b['pct']}%).",
                            f"/gastos?m={month}&g={b['id']}", "warning"))
    # A monthly bill still unpaid 3 days after its day (app/pagos.py): once per bill and month. Last
    # month's only when those 3 days end in this one (due on the 28th, nudged on the 1st): an old month
    # nobody marked isn't news.
    from . import pagos
    for month in (last, today.isoformat()[:7]):
        for e in pagos.month_status(conn, month, today):
            nudge = e["due"] + timedelta(days=3)
            if not e["paid"] and today >= nudge and (month != last or nudge >= today.replace(day=1)):
                b = e["bill"]
                extra = " · ".join(pagos.detail_lines(b))
                out.append((f"bill:{month}:{b['id']}", f"Falta pagar {pagos.short(b)}",
                            f"{b['name']} de {month_name(month, year=False)}: era para el {b['day']}."
                            + (f" {extra}" if extra else ""), f"/pagos/{b['id']}", "receipt"))
    if (has_mp and card_ok) or (today.day >= 20 and reports.months(conn) and last in reports.months(conn)):
        out.append((f"summary:{last}", *summary_message(conn, last, provisional=not (has_mp and card_ok))))
    return out


def summary_message(conn, month: str, provisional: bool = False) -> tuple[str, str, str, str]:
    from . import ledger
    s = reports.summary(conn, month)
    mep = reports.mep_for(conn, month)
    verb = "te quedaron" if s["left"] >= 0 else "gastaste de más"
    lines = [f"{month_name(month).capitalize()}: {verb} {ars(abs(s['left']))}"
             + (f" (≈ {usd(abs(s['left']) / 100 / mep)})" if mep else "") + "."]
    groups = reports.spending_view(conn, month, 1, "group", None)["items"][:3]
    if groups:
        lines.append(f"Gastaste {ars(s['spent'])}: " + ", ".join(f"{g['label']} {g['share']}%" for g in groups) + ".")
    over = [b for b in reports.budget_status(conn, month) if b["pct"] >= 100]
    if over:
        lines.append("Te pasaste en " + ", ".join(f"{b['label'].lower()} ({b['pct']}%)" for b in over) + ".")
    pending = len(ledger.pending_review(conn, month))
    if pending:
        lines.append(f"{pending} gasto{'s' if pending > 1 else ''} para revisar.")
    if provisional:
        lines.append("Provisorio: faltan datos de " + ", ".join(s["missing"]) + "." if s["missing"]
                     else "Provisorio: puede cambiar con el próximo resumen.")
    return f"Resumen de {month_name(month, year=False)}", " ".join(lines), f"/mes?m={month}", "moneybag"


def check(today: date | None = None) -> int:
    conn = db.connect()
    try:
        sent = 0
        for key, title, body, path, tags in due_messages(conn, today or date.today()):
            if _once(conn, key):
                try:
                    send(title, body, path, tags)
                    sent += 1
                except Exception:
                    conn.execute("DELETE FROM meta WHERE key = ?", (f"sent:{key}",))  # retry next hour
                    conn.commit()
        return sent
    finally:
        conn.close()


def _loop():
    while True:
        if 9 <= datetime.now().hour < 22:
            try:
                check()
            except Exception:
                pass
        time.sleep(3600)


def start() -> None:
    if os.environ.get("FINANZAS_NOTIFY") == "1":
        threading.Thread(target=_loop, name="notify", daemon=True).start()
