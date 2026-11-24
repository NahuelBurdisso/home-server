"""Finanzas: how much was left each month, from BBVA, Mercado Pago and cash."""
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import BackgroundTasks, FastAPI, Form, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import asistente, aviso, categories, classifier, correo, ledger, mpsync, notify, pagos, people, rapido, reports
from .db import ACCOUNTS, KINDS, connect
from .importers import ImportError_, parse_file
from .money import ars, month_name, month_short, parse_amount, shift_month, usd

HERE = Path(__file__).parent


@asynccontextmanager
async def lifespan(app):
    notify.start()
    mpsync.start()
    correo.start()
    pagos.start()  # reminders in Recordatorios, only with FINANZAS_PAGOS_AVISOS=1
    yield


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
app.mount("/ia", asistente.ia)  # tools for the local AI in Open WebUI (app/asistente.py)
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(ars=ars, usd=usd, month_name=month_name, month_short=month_short,
                             ACCOUNTS=ACCOUNTS, KINDS=KINDS, GROUPS=categories.GROUPS, cat_label=categories.label,
                             PEOPLE=people.PEOPLE)
templates.env.filters["month_short"] = month_short  # for "{{ months | map('month_short') }}"

ALLOWED = {s.strip().lower() for s in os.environ.get("FINANZAS_ALLOWED_LOGINS", "").split(",") if s.strip()}


SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
OWN_HOSTS = {"mac-server.tailnet-ejemplo.ts.net", "localhost", "127.0.0.1"}


def same_site(request: Request) -> bool:
    """A form or script on another web site can make the browser POST here (CSRF): browsers say so
    in Sec-Fetch-Site and Origin. Siri, n8n and curl send neither, and are let through."""
    site = request.headers.get("sec-fetch-site")
    if site and site not in ("same-origin", "same-site", "none"):
        return False
    origin = request.headers.get("origin")
    if origin and origin != "null":
        return urlparse(origin).hostname in OWN_HOSTS | {request.url.hostname}
    return True


@app.middleware("http")
async def tailnet_owner_only(request: Request, call_next):
    """tailscale serve tells us who is asking (Tailscale-User-Login). Only the people in
    FINANZAS_ALLOWED_LOGINS get in; direct requests on this Mac (no proxy) are allowed too."""
    if request.method not in SAFE_METHODS and not same_site(request):
        return JSONResponse({"error": "Pedido desde otro sitio: rechazado."}, status_code=403)
    if request.url.path != "/health":
        login = request.headers.get("tailscale-user-login", "").lower()
        proxied = "x-forwarded-for" in request.headers
        if (login and ALLOWED and login not in ALLOWED) or (proxied and not login):
            return JSONResponse({"error": "Solo para el dueño del tailnet."}, status_code=403)
    return await call_next(request)


def render(request: Request, name: str, **ctx):
    return templates.TemplateResponse(request, name, {"page": name.split(".")[0], **ctx})


def local(back: str, default: str) -> str:
    """Only redirect within the app (no '//other.site')."""
    return back if back.startswith("/") and not back.startswith("//") else default


def current_month(conn, requested: str | None) -> str:
    if requested and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", requested):
        return requested
    known = reports.months(conn)
    return known[-1] if known else date.today().isoformat()[:7]


@app.get("/health")
def health():
    connect().execute("SELECT 1")
    return {"ok": True}


@app.get("/")
def home():
    return RedirectResponse("/mes")


@app.get("/mes")
def month_view(request: Request, m: str | None = None, quien: str | None = None):
    conn = connect()
    ledger.apply_recurring(conn)
    month = current_month(conn, m)
    who = people.valid(quien)
    s = reports.summary(conn, month, who)
    mep = reports.mep_for(conn, month)
    top = max(s["income"], s["spent"], 1)
    known = reports.months(conn)
    groups = reports.spending_view(conn, month, 1, "group", None, who)["items"]
    # What you have today, on today's month only (a past month's page is about that month).
    have = reports.balances(conn) if month == date.today().isoformat()[:7] else None
    bills = pagos.summary(conn, date.today()) if have is not None else None
    return render(request, "mes.html", s=s, mep=mep, top=top, empty=not known, groups=groups[:5], have=have, who=who,
                  current=month == date.today().isoformat()[:7],
                  bills=bills,
                  more_groups=len(groups) > 5, to_review=len(ledger.pending_review(conn, month)),
                  prev=shift_month(month, -1), next=shift_month(month, 1),
                  has_prev=bool(known) and known[0] < month, has_next=bool(known) and known[-1] > month)


@app.get("/evolucion")
def evolution(request: Request, ver: str = "ars"):
    conn = connect()
    ledger.apply_recurring(conn)
    reports.refresh_rates(conn)
    rows = reports.series(conn)
    factors, cpi_month = reports.inflation_factors(conn, [r["month"] for r in rows])
    if ver == "real" and not cpi_month:
        ver = "ars"
    for r in rows:
        mep = reports.mep_for(conn, r["month"])
        # Shown amounts: pesos, dollars at that month's MEP (each month its own price), or pesos
        # of the last month INDEC published (each month's pesos grown by the inflation since).
        div = 100 * mep if ver == "usd" and mep else 100
        if ver == "real":
            div = 100 / (factors.get(r["month"]) or 1)
        r["value"], r["income_v"], r["spent_v"] = r["left"] / div, r["income"] / div, r["spent"] / div
    # Bar chart geometry (SVG, 0..100 wide per bar slot, zero line in the middle when needed).
    hi = max([r["value"] for r in rows] + [0])
    lo = min([r["value"] for r in rows] + [0])
    span = (hi - lo) or 1
    height = 180
    zero = height * hi / span
    for r in rows:
        h = height * abs(r["value"]) / span
        r["bar_y"] = zero - h if r["value"] >= 0 else zero
        r["bar_h"] = max(h, 1)
    # At least 6 slots wide so labels keep their size with few months; newest month on the right.
    width = max(len(rows), 6) * 60
    for i, r in enumerate(rows):
        r["x"] = width - (len(rows) - i) * 60
    # The average of the finished months with every account: an incomplete one (or this month, still
    # going) would pull it to whatever happened to be loaded. With none, all of them, and it says so.
    this = date.today().isoformat()[:7]
    whole = [r for r in rows if not r["missing"] and r["month"] < this]
    base = whole or rows
    money = usd if ver == "usd" else (lambda v: ars(v * 100))
    return render(request, "evolucion.html", rows=rows, ver=ver, zero=zero, height=height, width=width, cpi_month=cpi_month,
                  avg=sum(r["value"] for r in base) / len(base) if base else 0, avg_months=[r["month"] for r in whole],
                  money=money, short=usd if ver == "usd" else (lambda v: short_ars(v * 100)), this_month=this)


@app.get("/movimientos")
def movements(request: Request, m: str | None = None, tipo: str | None = None, quien: str | None = None):
    conn = connect()
    ledger.apply_recurring(conn)
    month = current_month(conn, m)
    who = people.valid(quien)
    sql = "SELECT * FROM transactions WHERE month = ?"
    args = [month]
    if tipo in KINDS:
        sql += " AND kind = ?"
        args.append(tipo)
    if who:
        sql += " AND who = ?"
        args.append(who)
    rows = conn.execute(sql + " ORDER BY date DESC, id DESC", args).fetchall()
    return render(request, "movimientos.html", rows=rows, month=month, tipo=tipo, who=who,
                  prev=shift_month(month, -1), next=shift_month(month, 1))


@app.get("/movimientos/{tx_id}")
def movement(request: Request, tx_id: int, back: str = "/movimientos"):
    conn = connect()
    t = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    if t is None:
        return RedirectResponse("/movimientos", status_code=303)
    similar = conn.execute("SELECT COUNT(*) FROM transactions WHERE merchant = ? AND id != ? AND kind = 'expense'",
                           (t["merchant"], tx_id)).fetchone()[0]
    return render(request, "movimiento.html", t=t, similar=similar, back=local(back, "/movimientos"))


@app.post("/movimientos/{tx_id}/categoria")
def change_category(tx_id: int, category: str = Form(...), learn: str = Form(""),
                    back: str = Form("/movimientos")):
    ledger.set_category(connect(), tx_id, category, learn=bool(learn))
    return RedirectResponse(local(back, "/movimientos"), status_code=303)


@app.post("/movimientos/{tx_id}/quien")
def change_who(tx_id: int, quien: str = Form(...), back: str = Form("/movimientos")):
    ledger.set_who(connect(), tx_id, quien)
    return RedirectResponse(local(back, "/movimientos"), status_code=303)


@app.get("/gastos")
def spending(request: Request, m: str | None = None, r: int = 1, g: str | None = None, c: str | None = None,
             quien: str | None = None):
    """Where the money went, with as much detail as you want: groups -> subcategories ->
    merchants -> movements. r = how many months (1, 3 or 6, shown as a monthly average);
    quien = only Nahuel's, Sofía's or both's."""
    conn = connect()
    ledger.apply_recurring(conn)
    month = current_month(conn, m)
    who = people.valid(quien)
    span = r if r in (1, 3, 6) else 1
    if c in categories.CATEGORIES:
        g = categories.group_of(c)
    elif g not in categories.GROUP:
        g = None
    view = {"month": month, "span": span, "g": g, "c": c if c in categories.CATEGORIES else None, "who": who}
    if view["c"]:
        main_ = reports.spending_view(conn, month, span, "merchant", view["c"], who)
        subs = None
        marks = ",".join("?" * len(main_["months"]))
        movs = conn.execute(f"SELECT * FROM transactions WHERE kind = 'expense' AND COALESCE(category, ?) = ?"
                            f" AND month IN ({marks}){' AND who = ?' if who else ''} ORDER BY date DESC",
                            [categories.UNKNOWN, view["c"], *main_["months"], *([who] if who else [])]).fetchall()
    elif g:
        main_ = reports.spending_view(conn, month, span, "sub", g, who)
        subs = reports.spending_view(conn, month, span, "merchant", g, who)["items"][:8]
        movs = None
    else:
        main_ = reports.spending_view(conn, month, span, "group", None, who)
        subs, movs = None, None
    known = reports.months(conn)
    # Months of the period without every account: their totals (and so the shares) will still grow.
    incomplete = [m for m in main_["months"] if m in known and reports.summary(conn, m)["missing"]]
    # Budgets are the house's: against everybody's spending only.
    budgets = {b["id"]: b for b in reports.budget_status(conn, month)} if span == 1 and not who else {}
    return render(request, "gastos.html", v=view, main=main_, merchants=subs, movs=movs, budgets=budgets,
                  incomplete=incomplete,
                  budget_limit=reports.budgets_for(conn, month).get(g) if g and not who else None,
                  group=categories.GROUP.get(g) if g else None,
                  sub_label=categories.CATEGORIES.get(view["c"]) if view["c"] else None,
                  to_review=len(ledger.pending_review(conn, month)) if span == 1 else 0,
                  prev=shift_month(month, -1), next=shift_month(month, 1),
                  has_prev=bool(known) and known[0] < month, has_next=bool(known) and known[-1] > month)


@app.post("/presupuesto")
def set_budget(request: Request, grupo: str = Form(...), monto: str = Form(""), m: str = Form("")):
    """A group's monthly limit, in this month's pesos ('' or 0 removes it)."""
    conn = connect()
    if grupo not in categories.GROUP:
        return RedirectResponse("/gastos", status_code=303)
    month = current_month(conn, m)
    amount = None
    if monto.strip() and monto.strip() != "0":
        try:
            amount = form_amount(monto)
        except ValueError:
            return RedirectResponse(f"/gastos?m={month}&g={grupo}", status_code=303)
    reports.set_budget(conn, grupo, amount, month)
    return RedirectResponse(f"/gastos?m={month}&g={grupo}", status_code=303)


@app.get("/saldos")
def balances_page(request: Request):
    conn, today = connect(), date.today().isoformat()
    return render(request, "saldos.html", b=reports.balances(conn), today=today, dues=aviso.upcoming(conn, today))


@app.post("/saldos")
async def set_balances(request: Request):
    """What you counted today. Only the accounts whose number changed start over from today, so
    saving the form as it came doesn't drop the movements of the day."""
    conn = connect()
    form = await request.form()
    now = {it["account"]: it.get("usd", it["now"]) for it in reports.balances(conn)["accounts"]}
    amounts = {}
    for acc in reports.BALANCE_ACCOUNTS:
        text = str(form.get(acc, "")).strip()
        if not text:
            continue
        try:
            cents = 0 if re.fullmatch(r"[$\s]*0+([.,]0*)?", text) else form_amount(text)
        except ValueError as e:
            return render(request, "saldos.html", b=reports.balances(conn), today=date.today().isoformat(), error=str(e))
        if cents != now.get(acc):
            amounts[acc] = cents
    reports.set_balances(conn, amounts, date.today().isoformat())
    return RedirectResponse("/saldos", status_code=303)


@app.get("/revisar")
def review(request: Request, m: str | None = None):
    conn = connect()
    ledger.apply_recurring(conn)
    rows = ledger.pending_review(conn, m)
    clf = classifier.get()
    st = classifier.status(conn)
    unsuggested = sum(1 for t in rows if t["category"] is None)
    return render(request, "revisar.html", rows=rows, month=m, clf=clf, unsuggested=unsuggested,
                  running=st.startswith("running"), error=st[6:] if st.startswith("error:") else None)


@app.post("/revisar/{tx_id}/ok")
def confirm(tx_id: int, back: str = Form("/revisar")):
    conn = connect()
    t = conn.execute("SELECT category FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    if t and t["category"]:
        ledger.set_category(conn, tx_id, t["category"], learn=True)
    return RedirectResponse(local(back, "/revisar"), status_code=303)


@app.post("/revisar/ia")
def suggest(background: BackgroundTasks):
    background.add_task(classifier.suggest_pending)
    return RedirectResponse("/revisar", status_code=303)


@app.post("/movimientos/{tx_id}/tipo")
def change_kind(tx_id: int, kind: str = Form(...), back: str = Form("/movimientos")):
    if kind in KINDS:
        ledger.set_kind(connect(), tx_id, kind)
    return RedirectResponse(local(back, "/movimientos"), status_code=303)


@app.post("/movimientos/{tx_id}/borrar")
def delete_manual(tx_id: int, back: str = Form("/movimientos")):
    ledger.delete_manual(connect(), tx_id)
    return RedirectResponse(local(back, "/movimientos"), status_code=303)


def upload_page(request: Request, conn, results=None, form_error=None):
    recurring = conn.execute("SELECT * FROM recurring ORDER BY kind DESC, amount_usd DESC").fetchall()
    return render(request, "cargar.html", results=results, last=reports.last_import(conn), recurring=recurring,
                  mp=mpsync.status(conn), gmail=correo.status(conn),
                  today=date.today().isoformat(), this_month=date.today().isoformat()[:7], form_error=form_error,
                  me=people.of_request(request))


def short_ars(cents: float) -> str:
    """'$ 10,3 M', '$ 845 mil': for a table that has to fit the phone's width."""
    pesos = abs(cents) / 100
    if pesos >= 1_000_000:
        text = f"$ {pesos / 1_000_000:.1f} M".replace(".", ",")
    elif pesos >= 1_000:
        text = f"$ {round(pesos / 1_000)} mil"
    else:
        text = f"$ {round(pesos)}"
    return ("−" if cents < 0 and round(pesos) else "") + text


def form_amount(text: str) -> int:
    """Centavos from a form field, read like quick-add ('8.500', '15 mil', '$ 2,5 mil');
    ValueError with a sentence for the person when there's no amount in it."""
    cents = rapido.amount_of(text)
    if not cents:
        raise ValueError(f"No entendí el monto “{text}”. Probá por ejemplo 8.500 o 15 mil.")
    return abs(cents)


@app.get("/cargar")
def upload_form(request: Request):
    return upload_page(request, connect())


async def import_files(conn, files: list[UploadFile], account: str | None) -> list[dict]:
    results = []
    for f in files:
        if not f.filename:
            continue
        try:
            acc, rows = parse_file(f.filename, await f.read(), account or None)
            res = ledger.store(conn, f.filename, acc, rows)
            months_seen = sorted({(r.month or r.date.isoformat()[:7]) for r in rows})
            results.append({"file": f.filename, "ok": True, "months": months_seen, **res})
        except ImportError_ as e:
            results.append({"file": f.filename, "ok": False, "error": str(e)})
        except Exception as e:  # a malformed file must not break the page
            results.append({"file": f.filename, "ok": False, "error": f"No pude leerlo ({type(e).__name__})."})
    return results


@app.post("/cargar")
async def upload(request: Request, background: BackgroundTasks, files: list[UploadFile], account: str = Form("")):
    conn = connect()
    results = await import_files(conn, files, account or None)
    if any(r["ok"] for r in results):
        background.add_task(classifier.suggest_pending)
    return upload_page(request, conn, results)


@app.post("/api/subir")
async def upload_from_shortcut(background: BackgroundTasks, files: list[UploadFile]):
    """For the iPhone Shortcut (Compartir -> Finanzas): same as Cargar, answers in one line of
    text that the Shortcut shows as a notification."""
    conn = connect()
    results = await import_files(conn, files, None)
    if any(r["ok"] for r in results):
        background.add_task(classifier.suggest_pending)
    lines = []
    for r in results:
        if not r["ok"]:
            lines.append(f"{r['file']}: {r['error']}")
        elif r.get("statement") and not r["new"]:
            lines.append(f"{ACCOUNTS[r['account']]}: ese resumen ya estaba cargado.")
        else:
            extra = f", reemplazó {r['replaced']} del Excel" if r.get("replaced") else ""
            lines.append(f"{ACCOUNTS[r['account']]}: {r['new']} movimientos nuevos{extra}.")
    pending = len(ledger.pending_review(conn))
    if pending:
        lines.append(f"{pending} para revisar.")
    # Only statements that were already loaded (Finanzas read the same mail first, or it was shared
    # before): 409, same text. n8n's mail workflow pushes whatever a 2xx answers, and a non-2xx stops
    # it, so the phone doesn't hear about that statement twice; the Shortcut and the bot still show it.
    again = bool(results) and all(r["ok"] and r.get("statement") and not r["new"] for r in results)
    return PlainTextResponse("\n".join(lines) or "No llegó ningún archivo.", status_code=409 if again else 200)


@app.get("/api/correo")
def gmail_status():
    """For healthcheck.sh: when Finanzas last read the Gmail (app/correo.py), and the last error."""
    s = correo.status(connect())
    age = int((datetime.now() - datetime.fromisoformat(s["ok"])).total_seconds()) if s.get("ok") else None
    return {"ok_age_s": age, **{k: s.get(k) for k in ("on", "ok", "error", "failing_since", "last_uid", "read", "skipped")}}


@app.post("/api/correo")
def gmail_now():
    """A round right now instead of waiting up to 10 minutes; answers what it did."""
    return PlainTextResponse(correo.run_once())


@app.post("/api/aviso-tarjeta")
async def card_notice(request: Request):
    """The text (or HTML) of BBVA's "ya podés descargar tu resumen" mail, by hand or from a script
    (Finanzas reads the mail by itself: app/correo.py). Saves the due date and amounts (app/aviso.py)
    and answers the line for the phone notification. n8n's mail workflow pushes whatever this answers
    (any status), so a notice it already knows gets one short line instead of the same notice again."""
    text = (await request.body()).decode("utf-8", "replace")
    notice = aviso.parse(text)
    if not notice:
        if re.search(r"resumen", text, re.I) and not aviso.about_card(text):  # the account's statement notice
            return PlainTextResponse("Ese aviso de BBVA no es de la tarjeta: no hay nada que anotar.", status_code=422)
        return PlainTextResponse("No encontré el vencimiento y el saldo en ese mail.", status_code=422)
    seen, stored = aviso.record(connect(), notice, date.today())
    if seen == "pdf":
        return PlainTextResponse(f"{ACCOUNTS[aviso.ACCOUNT]}: el resumen que vence el {notice['due']:%d/%m} ya está cargado.")
    if seen == "again":
        return PlainTextResponse(f"{ACCOUNTS[aviso.ACCOUNT]}: ese aviso ya estaba anotado.")
    return PlainTextResponse(aviso.message(stored))


@app.get("/api/estado")
def api_status():
    """For n8n (weekly summary, review nudge, card due dates into Recordatorios): the numbers
    as JSON, with the amounts already written the way the app shows them."""
    conn = connect()
    ledger.apply_recurring(conn)
    reports.refresh_rates(conn)
    today = date.today().isoformat()
    last = shift_month(today[:7], -1)
    month = None
    if last in reports.months(conn):
        s = reports.summary(conn, last)
        groups = reports.spending_view(conn, last, 1, "group", None)["items"][:3]
        month = {"mes": last, "nombre": month_name(last), "ingresos": ars(s["income"]), "gastos": ars(s["spent"]),
                 "quedo": ars(s["left"]), "quedo_centavos": s["left"], "faltan": s["missing"],
                 "top": [f"{g['emoji']} {g['label']} {g['share']}%" for g in groups]}
    dues = aviso.upcoming(conn, today)
    mep = reports.rate_on(conn, "bolsa", today)
    have = reports.balances(conn, today)
    return {
        "hoy": today,
        "saldos": {
            "total": ars(have["total"]), "total_centavos": have["total"],
            "total_usd": usd(have["total_usd"]) if have["total_usd"] is not None else None,
            "cuentas": [{"cuenta": it["label"], "saldo": usd(it["usd"] / 100) if "usd" in it else ars(it["now"])}
                        for it in have["accounts"]],
        } if have["accounts"] else None,
        "mes_anterior": month,
        "para_revisar": len(ledger.pending_review(conn)),
        "vencimientos": [{
            "cuenta": ACCOUNTS[r["account"]], "cierre": r["close"], "vence": r["due"],
            "total": ars(r["total"]) if r["total"] is not None else None,
            "total_usd": usd(r["total_usd"] / 100) if r["total_usd"] else None,
            "minimo": ars(r["minimum"]) if r["minimum"] is not None else None,
        } for r in dues],
        "mep": round(mep) if mep else None,
        "pagos": pagos.summary(conn, date.today()),  # this month's bills (app/pagos.py); None when there are none
        "url": notify.APP_URL,
    }


async def spoken_text(request: Request) -> str:
    """What Siri or the bot said: JSON {"texto": "..."}, a form field texto, or the text itself."""
    kind = request.headers.get("content-type", "")
    if "json" in kind:
        return str((await request.json()).get("texto", ""))
    if "form" in kind:
        return str((await request.form()).get("texto", ""))
    return (await request.body()).decode("utf-8", "replace")


@app.post("/api/gasto")
async def api_quick_expense(request: Request, background: BackgroundTasks):
    """For Siri ("Oye Siri, gasto"): a cash expense in one dictated line (app/rapido.py). Body:
    JSON {"texto": "..."}, a form field texto, or the text itself. Answers one sentence to read out;
    X-Id is the new movement, so the WhatsApp bot (mensajes/) can take it back."""
    texto = await spoken_text(request)
    conn = connect()
    try:
        written = rapido.write_down(conn, texto, who=people.of_request(request))
    except ValueError as e:
        return PlainTextResponse(str(e), status_code=422)
    text, unknown = written
    if unknown:
        background.add_task(classifier.suggest_pending)
    return PlainTextResponse(text, headers={"X-Id": str(written.id)})


@app.get("/atajo")
def shortcut_help(request: Request):
    return render(request, "atajo.html", url=notify.APP_URL + "/api/subir", gasto_url=notify.APP_URL + "/api/gasto",
                  pagos_url=notify.APP_URL + "/api/pagos", pagar_url=notify.APP_URL + "/api/pagos/pagar")


@app.post("/fijos")
def add_recurring(request: Request, description: str = Form(...), amount: str = Form(...), kind: str = Form("income"),
                  start: str = Form(...)):
    conn = connect()
    try:
        cents = form_amount(amount)
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", start[:7]):
            raise ValueError("Falta el mes desde el que cuenta.")
    except ValueError as e:
        return upload_page(request, conn, form_error=str(e))
    ledger.add_recurring(conn, description.strip() or "Fijo en dólares", cents,
                         "income" if kind == "income" else "expense", start[:7])
    return RedirectResponse("/cargar#fijos", status_code=303)


@app.post("/fijos/{rec_id}/borrar")
def delete_recurring(rec_id: int):
    ledger.delete_recurring(connect(), rec_id)
    return RedirectResponse("/cargar#fijos", status_code=303)


@app.post("/manual")
def manual(request: Request, kind: str = Form(...), when: str = Form(...), amount: str = Form(...),
           description: str = Form(""), quien: str = Form("")):
    if kind not in ("income", "expense", "usd_buy"):
        kind = "expense"
    try:
        cents = form_amount(amount)
        day = date.fromisoformat(when)
    except ValueError as e:
        return upload_page(request, connect(), form_error=str(e) if "monto" in str(e) else "Falta la fecha.")
    signed = cents if kind == "income" else -cents
    ledger.add_manual(connect(), day, description.strip() or KINDS[kind], signed, kind,
                      who=people.valid(quien) or people.of_request(request))
    return RedirectResponse(f"/mes?m={day.isoformat()[:7]}", status_code=303)


# ---- Pagos del mes: what the house pays every month (app/pagos.py) ----

MONTH_RE = r"\d{4}-(0[1-9]|1[0-2])"


def _month_or_today(m: str | None) -> str:
    return m if m and re.fullmatch(MONTH_RE, m) else date.today().isoformat()[:7]


def _optional_usd(text: str) -> int | None:
    text = (text or "").strip()
    if not text or re.fullmatch(r"[$\s]*0+([.,]0*)?", text):
        return None
    return form_amount(text)


def bill_fields(form) -> dict:
    """A bill from its form; ValueError with a sentence for the person."""
    get = lambda k: str(form.get(k, "") or "").strip()  # noqa: E731
    if not get("nombre"):
        raise ValueError("Falta el nombre.")
    try:
        day = int(get("dia"))
    except ValueError:
        raise ValueError("El día tiene que ser entre 1 y 28.") from None
    url = get("url")
    if url and not re.match(r"https?://", url, re.I):
        if "://" in url or url.lower().startswith(("javascript:", "data:")):
            raise ValueError("El link tiene que empezar con https://.")
        url = "https://" + url
    return {"name": get("nombre"), "emoji": get("emoji")[:8] or "🧾", "details": get("datos"), "url": url,
            "notes": get("notas"), "day": day, "match": get("detectar"), "amount_usd": _optional_usd(get("monto_usd")),
            "total_usd": _optional_usd(get("total_usd")), "paid_before_usd": _optional_usd(get("pagado_antes_usd")) or 0,
            "start_month": get("desde")[:7], "active": 1 if get("activo") else 0}


def bill_form_values(bill) -> dict:
    """What the edit form shows for a saved bill (amounts in dollars, as typed)."""
    dollars = lambda c: usd(c / 100)[4:] if c else ""  # noqa: E731
    return {"nombre": bill["name"], "emoji": bill["emoji"], "datos": bill["details"], "url": bill["url"],
            "notas": bill["notes"], "dia": bill["day"], "detectar": bill["match"], "monto_usd": dollars(bill["amount_usd"]),
            "total_usd": dollars(bill["total_usd"]), "pagado_antes_usd": dollars(bill["paid_before_usd"]),
            "desde": bill["start_month"], "activo": bool(bill["active"])}


def bill_page(request: Request, conn, bill=None, values=None, error=None):
    today = date.today()
    blank = {"emoji": "🧾", "desde": today.isoformat()[:7], "activo": True}
    return render(request, "pago.html", b=bill, f=values or (bill_form_values(bill) if bill else blank), error=error,
                  history=pagos.history(conn, bill["id"], today) if bill else [], today=today,
                  progress=pagos.progress(conn, bill) if bill else None, details=pagos.detail_lines(bill) if bill else [],
                  money=pagos.money)


@app.get("/pagos")
def bills_view(request: Request, m: str | None = None):
    conn = connect()
    today = date.today()
    month = _month_or_today(m)
    items = pagos.month_status(conn, month, today)
    for e in items:
        e["progress"] = pagos.progress(conn, e["bill"])
    shown = {e["bill"]["id"] for e in items}
    others = [b for b in pagos.all_bills(conn) if b["id"] not in shown]
    first = conn.execute("SELECT MIN(start_month) FROM bills").fetchone()[0]
    this = today.isoformat()[:7]
    return render(request, "pagos.html", items=items, others=others, month=month, today=today, this_month=this,
                  paid=sum(1 for e in items if e["paid"]), money=pagos.money,
                  prev=shift_month(month, -1), next=shift_month(month, 1),
                  has_prev=bool(first) and first < month, has_next=month < shift_month(this, 1))


@app.get("/pagos/nuevo")
def bill_new(request: Request):
    return bill_page(request, connect())


@app.post("/pagos")
async def bill_create(request: Request, background: BackgroundTasks):
    conn = connect()
    form = await request.form()
    try:
        f = bill_fields(form)
        bill_id = pagos.add(conn, f["name"], f["day"], f["start_month"], emoji=f["emoji"], details=f["details"],
                            url=f["url"], notes=f["notes"], match=f["match"], amount_usd=f["amount_usd"],
                            total_usd=f["total_usd"], paid_before_usd=f["paid_before_usd"])
    except ValueError as e:
        return bill_page(request, conn, values=dict(form), error=str(e))
    if not f["active"]:
        pagos.update(conn, bill_id, date.today(), active=0)
    background.add_task(pagos.sync_now)
    return RedirectResponse(f"/pagos/{bill_id}", status_code=303)


@app.get("/pagos/{bill_id}")
def bill_view(request: Request, bill_id: int):
    conn = connect()
    bill = pagos.get(conn, bill_id)
    if bill is None:
        return RedirectResponse("/pagos", status_code=303)
    return bill_page(request, conn, bill)


@app.post("/pagos/{bill_id}")
async def bill_update(request: Request, background: BackgroundTasks, bill_id: int):
    conn = connect()
    bill = pagos.get(conn, bill_id)
    if bill is None:
        return RedirectResponse("/pagos", status_code=303)
    form = await request.form()
    try:
        pagos.update(conn, bill_id, date.today(), **bill_fields(form))
    except ValueError as e:
        return bill_page(request, conn, bill, values=dict(form), error=str(e))
    background.add_task(pagos.sync_now)
    return RedirectResponse(f"/pagos/{bill_id}", status_code=303)


@app.post("/pagos/{bill_id}/borrar")
def bill_delete(background: BackgroundTasks, bill_id: int):
    pagos.delete(connect(), bill_id, date.today())
    background.add_task(pagos.sync_now)
    return RedirectResponse("/pagos", status_code=303)


@app.post("/pagos/{bill_id}/pagada")
def bill_paid(background: BackgroundTasks, bill_id: int, m: str = Form(""), fecha: str = Form(""),
              monto: str = Form(""), back: str = Form("")):
    """Paid by hand ("Ya lo pagué"): never a movement (see app/pagos.py)."""
    conn = connect()
    month = _month_or_today(m)
    back = local(back, f"/pagos?m={month}")
    if pagos.get(conn, bill_id) is None:
        return RedirectResponse(back, status_code=303)
    try:
        day = date.fromisoformat(fecha)
    except ValueError:
        day = date.today()
    try:
        amount = form_amount(monto) if monto.strip() else None
    except ValueError:
        amount = None
    pagos.mark_paid(conn, bill_id, month, day, amount)
    background.add_task(pagos.sync_now)
    return RedirectResponse(back, status_code=303)


@app.post("/pagos/{bill_id}/pendiente")
def bill_unpaid(background: BackgroundTasks, bill_id: int, m: str = Form(""), back: str = Form("")):
    month = _month_or_today(m)
    pagos.unmark(connect(), bill_id, month)
    background.add_task(pagos.sync_now)
    return RedirectResponse(local(back, f"/pagos?m={month}"), status_code=303)


@app.get("/api/pagos")
def api_bills():
    """This month's bills in one sentence, for Siri and the bot."""
    return PlainTextResponse(pagos.sentence(connect(), date.today()))


@app.post("/api/pagos/pagar")
async def api_bill_paid(request: Request, background: BackgroundTasks):
    """ "pagué la luz" from the bot or Siri: this month's is marked paid today. X-Id and X-Mes say what
    to take back (POST /api/pagos/{id}/pendiente?m=); none when it was already paid."""
    texto = await spoken_text(request)
    conn = connect()
    try:
        bill = pagos.find(conn, texto)
    except ValueError as e:
        return PlainTextResponse(str(e), status_code=422)
    today = date.today()
    month = today.isoformat()[:7]
    st = pagos.status_of(conn, bill["id"], month, today)
    name = month_name(month, year=False)
    if bill["start_month"] > month:  # nothing to take back: no X-Id
        return PlainTextResponse(f"{bill['name']} recién va desde {month_name(bill['start_month'], year=False)}:"
                                 f" el de {name} no te toca.")
    if st["paid"]:
        return PlainTextResponse(f"{bill['name']} de {name} ya estaba pagado (el {st['paid_on'].day}/{st['paid_on'].month}).")
    pagos.mark_paid(conn, bill["id"], month, today)
    background.add_task(pagos.sync_now)
    return PlainTextResponse(f"Listo: {pagos.lower_first(bill['name'])} de {name}, pagado hoy.",
                             headers={"X-Id": str(bill["id"]), "X-Mes": month})


@app.post("/api/pagos/{bill_id}/pendiente")
def api_bill_unpaid(background: BackgroundTasks, bill_id: int, m: str | None = None):
    """The bot's undo of "pagué la luz": takes back the hand mark."""
    conn = connect()
    bill = pagos.get(conn, bill_id)
    if bill is None:
        return PlainTextResponse("Ese pago ya no existe.", status_code=404)
    month = _month_or_today(m)
    name = month_name(month, year=False)
    st = pagos.status_of(conn, bill_id, month)
    if not st["manual"]:
        return PlainTextResponse(f"{bill['name']} de {name} no estaba marcado a mano.")
    pagos.unmark(conn, bill_id, month)
    background.add_task(pagos.sync_now)
    still = pagos.status_of(conn, bill_id, month)
    if still["paid"]:  # a movement pays it anyway
        return PlainTextResponse(f"Listo, saqué la marca: {pagos.lower_first(bill['name'])} de {name} igual figura pagado"
                                 " por un movimiento.")
    return PlainTextResponse(f"Listo: {pagos.lower_first(bill['name'])} de {name} vuelve a estar pendiente.")
