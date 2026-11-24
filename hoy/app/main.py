"""Hoy: the morning on one screen. The weather now and in the next hours, today's and tomorrow's
plans, what's missing at home, the card that's due, the month's bills still unpaid, the dollar, the papers that expire and how the
server is, plus two boxes to add to the list or to the calendar without leaving it. It keeps nothing
of its own: it asks the other apps (sources.py) every time it opens."""
import os
from datetime import date
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import sources

HERE = Path(__file__).parent
ALLOWED = {s.strip().lower() for s in os.environ.get("HOY_ALLOWED_LOGINS", "").split(",") if s.strip()}
# Mesa is Nahuel's alone: its card (and its sentence in the morning) only for these logins, or for a
# direct request on this Mac (no proxy, no login).
MESA_LOGINS = {s.strip().lower() for s in os.environ.get("HOY_MESA_LOGINS", "NahuelBurdisso@github").split(",") if s.strip()}
TS = "https://mac-server.tailnet-ejemplo.ts.net"
LINKS = {"lista": f"{TS}:3016", "agenda": f"{TS}:3014", "plata": f"{TS}:3013", "papeles": f"{TS}:3017",
         "server": f"{TS}:3015", "ia": f"{TS}:3012", "mesa": f"{TS}:3020"}
DAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
          "noviembre", "diciembre"]

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(LINKS=LINKS, DAYS=DAYS)


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
    HOY_ALLOWED_LOGINS get in; direct requests on this Mac (no proxy) are allowed too."""
    if request.method not in SAFE_METHODS and not same_site(request):
        return JSONResponse({"error": "Pedido desde otro sitio: rechazado."}, status_code=403)
    if request.url.path != "/health":
        login = request.headers.get("tailscale-user-login", "").lower()
        proxied = "x-forwarded-for" in request.headers
        if (login and ALLOWED and login not in ALLOWED) or (proxied and not login):
            return JSONResponse({"error": "Solo para el dueño del tailnet."}, status_code=403)
    return await call_next(request)


def sees_mesa(request: Request) -> bool:
    login = request.headers.get("tailscale-user-login", "").lower()
    return not login or login in MESA_LOGINS


@app.get("/health")
def health():
    return {"ok": True}


def dmy(iso: str | None) -> str:
    return f"{int(iso[8:10])}/{int(iso[5:7])}" if iso else ""


templates.env.filters["dmy"] = dmy


@app.get("/")
def home(request: Request, ok: str = "", msg: str = ""):
    today = date.today()
    s = sources.gather()
    agenda = (s["agenda"] or {}).get("recordatorios") or []
    plata = s["plata"] or {}
    dues = [v for v in plata.get("vencimientos") or [] if (date.fromisoformat(v["vence"]) - today).days <= 10]
    server = s["server"] or {}
    ctx = {
        "title": f"{DAYS[today.weekday()].capitalize()} {today.day} de {MONTHS[today.month - 1]}",
        "weather": sources.weather_view(s["weather"]),
        "holiday": sources.next_holiday(s["holidays"], today),
        "today": [r for r in agenda if r.get("fecha") == today.isoformat()],
        "tomorrow": [r for r in agenda if r.get("fecha") != today.isoformat()],
        "agenda_ok": s["agenda"] is not None,
        "lista": s["lista"], "plata": plata if s["plata"] else None, "dues": dues,
        "pagos": sources.pagos_view(plata.get("pagos"), today),
        "papeles": (s["papeles"] or {}).get("documentos") or [],
        "server": server if s["server"] else None,
        "mesa": sources.mesa_view(s["mesa"]) if sees_mesa(request) else None,
        "ok": ok, "msg": msg,
    }
    return templates.TemplateResponse(request, "hoy.html", ctx)


@app.post("/lista")
def add_to_list(texto: str = Form("")):
    good, answer = sources.post_text(f"{sources.HOST}:3016/api/agregar", texto)
    return RedirectResponse(f"/?ok={'1' if good else ''}&msg={quote(answer)}", status_code=303)


@app.post("/recordar")
def add_reminder(texto: str = Form("")):
    good, answer = sources.post_text(f"{sources.HOST}:3014/api/rapido", texto)
    return RedirectResponse(f"/?ok={'1' if good else ''}&msg={quote(answer)}", status_code=303)


def _say_list(items: list[str]) -> str:
    items = [i[:1].lower() + i[1:] for i in items]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " y " + items[-1]


@app.get("/api/frase")
def spoken(request: Request, mesa: int = 1):
    """'Oye Siri, buen día': the morning in a few sentences for a Shortcut (Get Contents of URL →
    Speak Text). Only what's worth hearing: an empty list or a quiet server aren't said.
    mesa=0 leaves the Mesa out: the Telegram bot asks so for anyone but Nahuel (its requests come
    straight from the container, with no Tailscale login to tell who's asking)."""
    today = date.today()
    s = sources.gather()
    parts = [f"Hoy es {DAYS[today.weekday()]} {today.day}."]
    w = sources.weather_view(s["weather"])
    if w:
        line = f"Hacen {w['temp']} grados, {w['words']}"
        if w["min"] is not None:
            line += f", con mínima de {w['min']} y máxima de {w['max']}"
        if w["rain"] and w["rain"] >= 30:
            line += f", y {w['rain']} por ciento de probabilidad de lluvia"
        parts.append(line + ".")
        parts += [f"Atención: {x}." for x in w["warn"]]
    h = sources.next_holiday(s["holidays"], today, within=7)
    if h:
        parts.append(f"Hoy es feriado: {h['name']}." if h["days"] == 0 else
                     f"El {DAYS[h['date'].weekday()]} es feriado{', fin de semana largo' if h['long'] else ''}.")
    agenda = (s["agenda"] or {}).get("recordatorios") or []
    hoy_items = [r for r in agenda if r.get("fecha") == today.isoformat()]
    if hoy_items:
        parts.append("Tenés " + _say_list([(f"a las {r['hora']} " if r.get("hora") else "") + r["titulo"] for r in hoy_items]) + ".")
    elif s["agenda"] is not None:
        parts.append("No tenés nada agendado para hoy.")
    lista = s["lista"] or {}
    things = [t for v in (lista.get("por_seccion") or {}).values() for t in v]
    if things:
        parts.append("En la lista falta " + _say_list(things[:6]) + (f" y {len(things) - 6} cosas más" if len(things) > 6 else "") + ".")
    for v in (s["plata"] or {}).get("vencimientos") or []:
        days = (date.fromisoformat(v["vence"]) - today).days
        if 0 <= days <= 3:
            parts.append(f"La {v['cuenta']} vence {'hoy' if days == 0 else 'mañana' if days == 1 else f'en {days} días'}.")
    parts += sources.pagos_frases((s["plata"] or {}).get("pagos"), today)
    server = s["server"] or {}
    if server.get("luz") in ("amarillo", "rojo"):
        parts.append("El server: " + ", ".join(server.get("avisos") or ["hay algo para mirar"]) + ".")
    mesa = sources.mesa_view(s["mesa"]) if mesa and sees_mesa(request) else None
    if mesa:
        parts.append(mesa["frase"] or ("La mesa todavía no empezó." if not mesa["empezo"] else
                                       f"La mesa va {mesa['ret']} contra {mesa['vara']} del S&P."))
    return PlainTextResponse(" ".join(parts))
