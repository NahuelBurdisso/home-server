"""Vigía: the server watches the outside world. Vigilancias: pages checked on a schedule (a price,
a change, "turnos" appearing) and a message when something changes. Guardado: a link, a video or a
long voice note kept with its text and a summary from the local model, searchable.

Both halves share the fetcher and its safety rules (fetch.py), the notifier (notify.py) and the
Telegram wiring (the bot in mensajes/ calls the /api endpoints below)."""
import json
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from . import asistente, extract, guardado, watch
from .db import connect, get_meta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
HERE = Path(__file__).parent
ALLOWED = {s.strip().lower() for s in os.environ.get("VIGIA_ALLOWED_LOGINS", "").split(",") if s.strip()}
PUBLIC_URL = os.environ.get("VIGIA_URL", "https://mac-server.tailnet-ejemplo.ts.net:3021")
STALE = 300  # seconds without a scheduler pass before /health says so
STARTED = time.time()


def workers_on() -> bool:
    return os.environ.get("VIGIA_WORKERS", "1") != "0"


@asynccontextmanager
async def lifespan(app):
    if workers_on():
        connect().close()  # the file and its schema exist before the two threads open it at once
        threading.Thread(target=watch.scheduler, name="scheduler", daemon=True).start()
        threading.Thread(target=guardado.worker, name="guardado", daemon=True).start()
    yield


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
app.mount("/ia", asistente.ia)  # tools for the local AI in Open WebUI and the bot (app/asistente.py)
templates = Jinja2Templates(directory=HERE / "templates")


def marked(snippet: str | None) -> Markup:
    """An FTS snippet with its match marks (\\x02…\\x03) as <mark>, the rest escaped."""
    s = str(escape(snippet or ""))
    return Markup(s.replace("\x02", "<mark>").replace("\x03", "</mark>"))


def ago(ts: str | None) -> str:
    if not ts:
        return "nunca"
    d = datetime.fromisoformat(ts)
    secs = (datetime.now() - d).total_seconds()
    if secs < 90:
        return "recién"
    if secs < 3600:
        return f"hace {int(secs // 60)} min"
    if d.date() == datetime.now().date():
        return f"hoy {d:%H:%M}"
    return f"{d:%d/%m %H:%M}"


templates.env.globals.update(PUBLIC_URL=PUBLIC_URL, money=extract.money, ago=ago, card_text=watch.card_text,
                             state=watch.state, every_text=watch.every_text, marked=marked,
                             KIND_NAMES=guardado.KIND_NAMES)


SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
OWN_HOSTS = {"mac-server.tailnet-ejemplo.ts.net", "localhost", "127.0.0.1"}


def same_site(request: Request) -> bool:
    """A form or script on another web site can make the browser POST here (CSRF): browsers say so
    in Sec-Fetch-Site and Origin. Siri, the bot and curl send neither, and are let through."""
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
    VIGIA_ALLOWED_LOGINS get in; direct requests on this Mac (no proxy: the bot, Siri through
    Hoy) are allowed too."""
    if request.method not in SAFE_METHODS and not same_site(request):
        return JSONResponse({"error": "Pedido desde otro sitio: rechazado."}, status_code=403)
    if request.url.path != "/health":
        login = request.headers.get("tailscale-user-login", "").lower()
        proxied = "x-forwarded-for" in request.headers
        if (login and ALLOWED and login not in ALLOWED) or (proxied and not login):
            return JSONResponse({"error": "Solo para el dueño del tailnet."}, status_code=403)
    return await call_next(request)


def render(request: Request, template: str, **ctx):
    return templates.TemplateResponse(request, template, {"page": template.split(".")[0], **ctx})


def _who(request: Request) -> str:
    """Who asked: the bot's X-Quien (the person's name), else the Tailscale login's user."""
    if q := request.headers.get("x-quien"):
        return unquote(q)[:60]
    return request.headers.get("tailscale-user-login", "").split("@")[0][:60]


@app.get("/health")
def health():
    """503 when the database doesn't answer or the scheduler hasn't looped in 5 minutes (the first
    minutes after a start get the benefit of the doubt)."""
    conn = connect()
    conn.execute("SELECT 1")
    last = int(get_meta(conn, "loop") or 0)
    age = int(time.time() - last) if last else None
    stale = workers_on() and time.time() - STARTED > STALE and (age is None or age > STALE)
    return JSONResponse({"ok": not stale, "scheduler_age_s": age}, status_code=503 if stale else 200)


# --- pages: vigilancias --------------------------------------------------------------------------

@app.get("/")
def home(request: Request, ok: str = "", error: str = ""):
    rows = connect().execute("SELECT * FROM watches ORDER BY active DESC, COALESCE(last_change, created_at) DESC")
    return render(request, "vigilancias.html", watches=rows.fetchall(), ok=ok, error=error)


def _number(s: str) -> float | None:
    s = (s or "").strip()
    return extract.parse_price(s) if s else None


@app.post("/vigilar")
def add_form(request: Request, url: str = Form(""), kind: str = Form("cambio"), selector: str = Form(""),
             needle: str = Form(""), cuando: str = Form("aparece"), objetivo: str = Form(""),
             umbral: str = Form("1"), every_min: str = Form("60"), name: str = Form("")):
    fields = {"url": url, "kind": kind, "selector": selector, "needle": needle, "cuando": cuando,
              "objetivo": _number(objetivo), "umbral": _number(umbral) or 1, "name": name,
              "every_min": int(every_min) if every_min.strip().isdigit() else 60, "added_by": _who(request)}
    try:
        wid, sentence, _ = watch.add(connect(), fields)
    except watch.Invalid as e:
        wid, sentence = None, str(e)
    if wid is None:
        return RedirectResponse("/?error=" + quote(sentence.replace("*", "")), status_code=303)
    return RedirectResponse(f"/v/{wid}?ok=" + quote(sentence.replace("*", "")), status_code=303)


@app.get("/v/{wid}")
def watch_page(request: Request, wid: int, ok: str = ""):
    conn = connect()
    w = conn.execute("SELECT * FROM watches WHERE id = ?", (wid,)).fetchone()
    if w is None:
        return RedirectResponse("/", status_code=303)
    checks = conn.execute("SELECT * FROM checks WHERE watch_id = ? ORDER BY ts DESC, id DESC LIMIT 60", (wid,)).fetchall()
    stats = watch.price_stats(conn, w) if w["kind"] == "precio" else None
    line = Markup(watch.svg_line(stats["points"])) if stats else ""
    return render(request, "vigilancia.html", w=w, checks=checks, stats=stats, line=line, ok=ok)


@app.post("/v/{wid}/pausar")
def pause(wid: int):
    conn = connect()
    conn.execute("UPDATE watches SET active = 1 - active WHERE id = ?", (wid,))
    conn.commit()
    return RedirectResponse(f"/v/{wid}", status_code=303)


@app.post("/v/{wid}/borrar")
def delete_watch(wid: int):
    """Also the bot's "borrar" (the id came in X-Ids)."""
    conn = connect()
    conn.execute("DELETE FROM checks WHERE watch_id = ?", (wid,))
    conn.execute("DELETE FROM watches WHERE id = ?", (wid,))
    conn.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/v/{wid}/probar")
def try_now(wid: int):
    """'Probar ahora': one check right away, with no random delay; it alerts like any other."""
    conn = connect()
    alert = watch.run(conn, wid)
    w = conn.execute("SELECT * FROM watches WHERE id = ?", (wid,)).fetchone()
    if w is None:
        return RedirectResponse("/", status_code=303)
    if w["last_error"] and w["last_check"] != w["last_ok"]:
        msg = f"No pude leerla: {w['last_error']}"
    elif alert:
        msg = "Cambió: te mandé el aviso."
    else:
        msg = f"Leí: {watch.card_text(w)}. Sin novedades."
    return RedirectResponse(f"/v/{wid}?ok=" + quote(msg.replace("*", "")), status_code=303)


# --- pages: guardado ---------------------------------------------------------------------------------

@app.get("/guardado")
def saved_page(request: Request, q: str = "", ok: str = "", error: str = ""):
    rows = guardado.search(connect(), q)
    return render(request, "guardado.html", rows=rows, q=q, ok=ok, error=error)


@app.post("/guardar")
def save_form(request: Request, url: str = Form(""), texto: str = Form(""), titulo: str = Form("")):
    try:
        iid, _, sentence = guardado.save(connect(), url=url, texto=texto, titulo=titulo, quien=_who(request))
    except guardado.Invalid as e:
        return RedirectResponse("/guardado?error=" + quote(str(e)), status_code=303)
    return RedirectResponse(f"/g/{iid}?ok=" + quote(sentence.replace("*", "")), status_code=303)


@app.get("/g/{iid}")
def item_page(request: Request, iid: int, ok: str = ""):
    item = connect().execute("SELECT * FROM saved WHERE id = ?", (iid,)).fetchone()
    if item is None:
        return RedirectResponse("/guardado", status_code=303)
    return render(request, "item.html", item=item, ok=ok)


@app.post("/g/{iid}/borrar")
def delete_item(iid: int):
    """Also the bot's "borrar" (the id came in X-Ids)."""
    conn = connect()
    conn.execute("DELETE FROM saved WHERE id = ?", (iid,))
    conn.commit()
    return RedirectResponse("/guardado", status_code=303)


@app.post("/g/{iid}/reintentar")
def retry_item(iid: int):
    conn = connect()
    conn.execute("UPDATE saved SET status = 'pendiente', error = NULL WHERE id = ? AND status IN ('error', 'sin_resumen')"
                 " AND (url IS NOT NULL OR text != '')", (iid,))
    conn.commit()
    guardado.wake.set()
    return RedirectResponse(f"/g/{iid}", status_code=303)


@app.get("/ayuda")
def help_page(request: Request):
    return render(request, "ayuda.html")


# --- for the bot and Siri (plain text in, one sentence out) -----------------------------------------

async def _body(request: Request) -> tuple[str, dict]:
    """(the text, the JSON body if it was one)."""
    raw = (await request.body()).decode("utf-8", "replace").strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError:
            return "", {}
        if isinstance(data, dict):
            t = data.get("texto")
            return (t if isinstance(t, str) else ""), data
        return "", {}
    return raw, {}


def _str(data: dict, key: str) -> str:
    """A string field of the JSON body; a number only for "chat" (Telegram's chat ids are numbers)."""
    v = data.get(key)
    if key == "chat" and isinstance(v, int) and not isinstance(v, bool):
        return str(v)
    return v.strip() if isinstance(v, str) else ""


@app.post("/api/vigilar")
async def api_watch(request: Request):
    """"seguí el precio de <url>", "avisame si cambia <url>", "avisame cuando aparezca "turnos" en
    <url>". Text, or JSON {"texto", "quien", "chat"} (the bot: chat is where alerts go). Answers what
    the first check read; X-Ids has the new id, so the bot's "borrar" can take it back. 422 when
    nothing was created (the sentence says why)."""
    text, data = await _body(request)
    try:
        fields = watch.parse(text)
    except watch.Invalid as e:
        return PlainTextResponse(str(e), status_code=422)
    fields.update(added_by=_str(data, "quien") or _who(request), chat=_str(data, "chat") or None)
    try:
        wid, sentence, new = watch.add(connect(), fields)
    except watch.Invalid as e:
        return PlainTextResponse(str(e), status_code=422)
    if wid is None:
        return PlainTextResponse(sentence, status_code=422)
    return PlainTextResponse(sentence, headers={"X-Ids": str(wid) if new else "", "X-Id": str(wid)})


@app.get("/api/vigilancias")
def api_watches():
    """'¿Qué estoy vigilando?' in one sentence."""
    return PlainTextResponse(watch.list_sentence(connect()))


@app.post("/api/guardar")
async def api_save(request: Request):
    """{"url": ...} or {"texto": ..., "titulo": ...} (optional "tipo": "audio", "quien", "chat"), or
    the link itself as the body. Answers at once (the reading and the summary happen in the
    background): the sentence, X-Id with the item and X-Ids only when it's new (for "borrar").
    With Accept: application/json, {"id", "nuevo", "respuesta"}."""
    text, data = await _body(request)
    url = _str(data, "url") if data else (text if re.match(r"(?:https?://|www\.)\S+$", text) else "")
    note = _str(data, "texto") if data else ("" if url else text)
    try:
        iid, new, sentence = guardado.save(connect(), url=url, texto=note, titulo=_str(data, "titulo"),
                                           kind=_str(data, "tipo"), quien=_str(data, "quien") or _who(request),
                                           chat=_str(data, "chat") or None)
    except guardado.Invalid as e:
        return PlainTextResponse(str(e), status_code=422)
    headers = {"X-Id": str(iid), "X-Ids": str(iid) if new else ""}
    if request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse({"id": iid, "nuevo": new, "respuesta": sentence}, headers=headers)
    return PlainTextResponse(sentence, headers=headers)
