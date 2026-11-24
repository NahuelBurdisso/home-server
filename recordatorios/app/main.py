"""Recordatorios: events and dates you want the iPhone to remind you of.

Write one line ("mañana 18hs dentista", "cumple de mamá 15/3") in the app, or dictate it to
Siri with a Shortcut; ntfy brings the alert to the phone. A calendar feed shows it all in the
iPhone's Calendar app."""
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import asistente, ia, ics, notify, when
from .db import connect
from .parse import parse

HERE = Path(__file__).parent
ALLOWED = {s.strip().lower() for s in os.environ.get("RECORDATORIOS_ALLOWED_LOGINS", "").split(",") if s.strip()}
UPCOMING_DAYS = 60
# The calendar feed on the standard HTTPS port (tailscale serve mounts the app at /recordatorios there):
# iOS refuses to subscribe to the same feed on :3014.
CALENDAR_URL = os.environ.get("RECORDATORIOS_CALENDAR_URL",
                              "https://mac-server.tailnet-ejemplo.ts.net/recordatorios/calendario.ics")


@asynccontextmanager
async def lifespan(app):
    if os.environ.get("RECORDATORIOS_ALERTS", "on") != "off":
        notify.start()
    yield


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
app.mount("/ia", asistente.ia)  # tools for the local AI in Open WebUI (app/asistente.py)
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(REPEATS=when.REPEATS, ALERTS=when.ALERTS, PUBLIC_URL=notify.PUBLIC_URL, links=notify.links,
                             CALENDAR_URL=CALENDAR_URL, WEBCAL_URL="webcal://" + CALENDAR_URL.split("://", 1)[1])


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
    RECORDATORIOS_ALLOWED_LOGINS get in; direct requests on this Mac (no proxy) are allowed too."""
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


def now() -> datetime:
    return datetime.now().replace(second=0, microsecond=0)


def add(conn, f: dict) -> int:
    cur = conn.execute("INSERT INTO reminders (title, notes, date, time, repeat, alerts) VALUES (?, ?, ?, ?, ?, ?)",
                       (f["title"], f.get("notes", ""), f["date"], f["time"], f["repeat"], f["alerts"]))
    conn.commit()
    return cur.lastrowid


def get(conn, rid: int):
    return conn.execute("SELECT * FROM reminders WHERE id = ?", (rid,)).fetchone()


def confirmation(r, at: datetime) -> str:
    return f"{r['title']}: {when.long_when(r, at)}. {when.alerts_text(r)}"


def upcoming(conn, at: datetime, days: int):
    """Every occurrence from the start of today, soonest first."""
    start = at.replace(hour=0, minute=0)
    items = []
    for r in conn.execute("SELECT * FROM reminders").fetchall():
        for occ in when.occurrences(r, start, start + timedelta(days=days)):
            items.append({"r": r, "occ": occ, "past": occ < at and r["time"] is not None,
                          "years": when.years(r, occ), "when": when.when_text(occ, not r["time"], at)})
    return sorted(items, key=lambda i: (i["occ"], i["r"]["title"].lower()))


def grouped(items, today: date):
    groups = {"Hoy": [], "Mañana": [], "Esta semana": [], "Más adelante": []}
    for i in items:
        days = (i["occ"].date() - today).days
        key = "Hoy" if days == 0 else "Mañana" if days == 1 else "Esta semana" if days < 7 else "Más adelante"
        groups[key].append(i)
    return [(k, v) for k, v in groups.items() if v]


def read_form(titulo: str, fecha: str, hora: str, repetir: str, avisos: list[str], notas: str) -> dict:
    """Checks the edit form. Raises ValueError with a message for the person."""
    if not titulo.strip():
        raise ValueError("Falta el título.")
    try:
        d = date.fromisoformat(fecha)
    except ValueError:
        raise ValueError("Falta la fecha.") from None
    t = None
    if hora.strip():
        try:
            t = datetime.strptime(hora.strip()[:5], "%H:%M").strftime("%H:%M")
        except ValueError:
            raise ValueError("La hora no se entiende.") from None
    alerts = sorted({int(a) for a in avisos if a.isdigit() and int(a) in when.ALERTS}, reverse=True)
    return {"title": titulo.strip(), "notes": notas.strip(), "date": d.isoformat(), "time": t,
            "repeat": repetir if repetir in when.REPEATS else "none", "alerts": ",".join(map(str, alerts))}


@app.get("/health")
def health():
    connect().execute("SELECT 1")
    return {"ok": True}


@app.get("/")
def home(request: Request, ok: int | None = None):
    conn = connect()
    at = now()
    added = get(conn, ok) if ok else None
    return render(request, "proximos.html", groups=grouped(upcoming(conn, at, UPCOMING_DAYS), at.date()),
                  added=added, added_text=confirmation(added, at) if added else None, days=UPCOMING_DAYS)


@app.post("/rapido")
def quick_add(texto: str = Form("")):
    at = now()
    found = (parse(texto, at) or ia.understand(texto, at)) if texto.strip() else None
    if found is None:
        # No date in it: open the full form with what was written as the title.
        return RedirectResponse("/nuevo?" + urlencode({"titulo": texto, "sin_fecha": "1"}), status_code=303)
    return RedirectResponse(f"/?ok={add(connect(), found)}", status_code=303)


@app.get("/nuevo")
def new_form(request: Request, titulo: str = "", sin_fecha: str = ""):
    blank = {"id": None, "title": titulo, "notes": "", "date": "", "time": "", "repeat": "none", "alerts": "60,0"}
    return render(request, "editar.html", r=blank, error="No encontré cuándo: elegí la fecha." if sin_fecha else None)


@app.post("/nuevo")
def create(request: Request, titulo: str = Form(""), fecha: str = Form(""), hora: str = Form(""),
           repetir: str = Form("none"), avisos: list[str] = Form([]), notas: str = Form("")):
    try:
        f = read_form(titulo, fecha, hora, repetir, avisos, notas)
    except ValueError as e:
        r = {"id": None, "title": titulo, "notes": notas, "date": fecha, "time": hora, "repeat": repetir,
             "alerts": ",".join(avisos)}
        return render(request, "editar.html", r=r, error=str(e))
    return RedirectResponse(f"/?ok={add(connect(), f)}", status_code=303)


@app.get("/r/{rid}")
def edit_form(request: Request, rid: int):
    r = get(connect(), rid)
    if r is None:
        return RedirectResponse("/", status_code=303)
    return render(request, "editar.html", r=r, error=None, next=when.next_occurrence(r, now()))


@app.post("/r/{rid}")
def update(request: Request, rid: int, titulo: str = Form(""), fecha: str = Form(""), hora: str = Form(""),
           repetir: str = Form("none"), avisos: list[str] = Form([]), notas: str = Form("")):
    conn = connect()
    try:
        f = read_form(titulo, fecha, hora, repetir, avisos, notas)
    except ValueError as e:
        r = {**dict(get(conn, rid) or {}), "id": rid, "title": titulo, "notes": notas, "date": fecha, "time": hora,
             "repeat": repetir, "alerts": ",".join(avisos)}
        return render(request, "editar.html", r=r, error=str(e))
    # A changed date, time or repeat starts over: nothing counts as done any more. Editing only the
    # title, notes or alerts keeps what was already marked done (and its snoozes).
    old = get(conn, rid)
    moved = old is None or (old["date"], old["time"], old["repeat"]) != (f["date"], f["time"], f["repeat"])
    conn.execute("UPDATE reminders SET title = ?, notes = ?, date = ?, time = ?, repeat = ?, alerts = ?"
                 + (", done_until = NULL" if moved else "") + " WHERE id = ?",
                 (f["title"], f["notes"], f["date"], f["time"], f["repeat"], f["alerts"], rid))
    if moved:
        conn.execute("DELETE FROM snoozes WHERE reminder_id = ?", (rid,))
    conn.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/r/{rid}/borrar")
def delete(rid: int):
    conn = connect()
    conn.execute("DELETE FROM reminders WHERE id = ?", (rid,))
    conn.commit()
    return RedirectResponse("/", status_code=303)


@app.get("/todos")
def everything(request: Request):
    conn = connect()
    at = now()
    active, past = [], []
    for r in conn.execute("SELECT * FROM reminders").fetchall():
        nxt = when.next_occurrence(r, at)
        item = {"r": r, "next": nxt, "when": when.when_text(nxt, not r["time"], at) if nxt else None,
                "repeat": when.long_when(r, at) if r["repeat"] != "none" else None}
        (active if nxt else past).append(item)
    active.sort(key=lambda i: i["next"])
    past.sort(key=lambda i: i["r"]["date"], reverse=True)
    return render(request, "todos.html", active=active, past=past[:50])


@app.get("/ayuda")
def help_page(request: Request):
    try:
        topic = notify.topic()
    except OSError:
        topic = None
    return render(request, "ayuda.html", topic=topic)


@app.api_route("/calendario.ics", methods=["GET", "HEAD"])  # Calendar clients check with HEAD first
def calendar_feed():
    rows = connect().execute("SELECT * FROM reminders ORDER BY date").fetchall()
    return Response(ics.calendar(rows), media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": 'inline; filename="recordatorios.ics"'})


# ---- For Siri Shortcuts and the notification buttons: short plain-text answers. ----

@app.post("/api/rapido")
async def api_quick_add(request: Request):
    """Body: JSON {"texto": "..."}, a form field texto, or the text itself. X-Id is the new reminder,
    so the WhatsApp bot (mensajes/) can take it back."""
    kind = request.headers.get("content-type", "")
    if "json" in kind:
        try:
            body = await request.json()
        except ValueError:  # not JSON after all
            body = None
        texto = str(body.get("texto", "")) if isinstance(body, dict) else body if isinstance(body, str) else ""
    elif "form" in kind:
        texto = str((await request.form()).get("texto", ""))
    else:
        texto = (await request.body()).decode("utf-8", "replace")
    at = now()
    # The local AI takes seconds (up to a minute): in a worker thread, so the app keeps answering.
    found = await run_in_threadpool(lambda: parse(texto, at) or ia.understand(texto, at)) if texto.strip() else None
    if found is None:
        return PlainTextResponse("No entendí cuándo. Probá por ejemplo: mañana a las 18 dentista.", status_code=422)
    conn = connect()
    note = " Lo interpretó la IA: revisalo en la app." if found.get("ia") else ""
    rid = add(conn, found)
    return PlainTextResponse("Listo. " + confirmation(get(conn, rid), at) + note, headers={"X-Id": str(rid)})


@app.post("/api/recordatorio")
async def api_add(request: Request):
    """For n8n: one reminder whose fields are already known. JSON {"titulo", "fecha" (YYYY-MM-DD),
    "hora" (HH:MM, optional), "avisos" (minutes before, as in the form), "notas"}. The same title on
    the same date is only saved once, so sending it again is harmless."""
    try:
        body = await request.json()
        f = read_form(str(body.get("titulo", "")), str(body.get("fecha", "")), str(body.get("hora") or ""),
                      "none", [str(a) for a in body.get("avisos", [0])], str(body.get("notas", "")))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    conn = connect()
    at = now()
    old = conn.execute("SELECT id FROM reminders WHERE title = ? AND date = ?", (f["title"], f["date"])).fetchone()
    rid = old["id"] if old else add(conn, f)
    return {"id": rid, "nuevo": old is None, "texto": confirmation(get(conn, rid), at)}


@app.get("/api/proximos")
def api_upcoming(dias: int = 7):
    """What's coming, as a sentence Siri can read out."""
    at = now()
    items = [i for i in upcoming(connect(), at, max(1, min(dias, 60))) if not i["past"]]
    if not items:
        return PlainTextResponse(f"No tenés nada en los próximos {dias} días.")
    parts = [f"{i['when']}: {i['r']['title']}" for i in items[:10]]
    return PlainTextResponse(". ".join(parts) + ".")


def _occ(occ: str | None) -> datetime:
    try:
        return datetime.fromisoformat(occ) if occ else now()
    except ValueError:
        return now()


@app.get("/api/r/{rid}")
def api_get(rid: int):
    """For the apps that made a reminder (Finanzas' monthly bills): whether its "Hecho" was tapped.
    Only a one-time reminder can be done for good; a repeating one always has a next time."""
    r = get(connect(), rid)
    if r is None:
        return JSONResponse({"error": "Ese recordatorio ya no existe."}, status_code=404)
    first = when.anchor(r).isoformat(timespec="minutes")
    done = r["repeat"] == "none" and bool(r["done_until"]) and r["done_until"] >= first
    return {"id": r["id"], "titulo": r["title"], "fecha": r["date"], "hora": r["time"], "notas": r["notes"],
            "hecho": done}


@app.post("/api/r/{rid}/hecho")
def api_done(rid: int, occ: str | None = None):
    notify.mark_done(connect(), rid, _occ(occ))
    return PlainTextResponse("Hecho.")


@app.post("/api/r/{rid}/posponer")
def api_snooze(rid: int, occ: str | None = None, min: int = 0, hasta: str = ""):
    conn = connect()
    if get(conn, rid) is None:
        return PlainTextResponse("Ya no existe.", status_code=404)
    notify.snooze(conn, rid, _occ(occ), now(), minutes=None if hasta == "manana" else (min or 60))
    return PlainTextResponse("Te vuelvo a avisar.")
