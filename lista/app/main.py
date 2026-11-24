"""Lista: the shopping list of the house. Type or dictate "leche, pan y 2 kg de papas" and each thing
lands in its section of the store; tick them off at the market; "terminé las compras" keeps them
as history, which suggests what you usually buy. Siri and the local AI add and read it too."""
import os
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import asistente, store
from .db import connect
from .parse import ORDER, SECTIONS, key_of, section_of, split

HERE = Path(__file__).parent
ALLOWED = {s.strip().lower() for s in os.environ.get("LISTA_ALLOWED_LOGINS", "").split(",") if s.strip()}
PUBLIC_URL = os.environ.get("LISTA_URL", "https://mac-server.tailnet-ejemplo.ts.net:3016")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
app.mount("/ia", asistente.ia)  # tools for the local AI in Open WebUI (app/asistente.py)
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(SECTIONS=SECTIONS, PUBLIC_URL=PUBLIC_URL)


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
    LISTA_ALLOWED_LOGINS get in; direct requests on this Mac (no proxy) are allowed too."""
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


def grouped(rows) -> list[tuple[str, list]]:
    """(section id, items) in the order you walk the store."""
    by = {}
    for r in rows:
        by.setdefault(r["section"], []).append(r)
    return [(sid, by[sid]) for sid in ORDER if sid in by]


@app.get("/health")
def health():
    connect().execute("SELECT 1")
    return {"ok": True}


@app.get("/")
def home(request: Request, ok: str = ""):
    conn = connect()
    pending, cart = store.pending(conn), store.cart(conn)
    return render(request, "lista.html", groups=grouped(pending), cart=cart, count=len(pending),
                  usual=store.usual(conn), added=ok)


@app.post("/agregar")
def add(texto: str = Form("")):
    res = store.add(connect(), texto)
    return RedirectResponse("/?ok=" + ("1" if res["added"] or res["already"] else ""), status_code=303)


@app.post("/i/{item_id}/tilde")
def toggle(request: Request, item_id: int):
    """Ticks or unticks; answers JSON to the page's script, a redirect to a plain form."""
    done = store.toggle(connect(), item_id)
    if request.headers.get("accept", "").startswith("application/json"):
        return {"ok": done is not None, "done": done}
    return RedirectResponse("/", status_code=303)


@app.post("/i/{item_id}/borrar")
def delete(item_id: int):
    store.delete(connect(), item_id)
    return RedirectResponse("/", status_code=303)


@app.post("/terminar")
def finish():
    store.finish(connect())
    return RedirectResponse("/", status_code=303)


@app.get("/historial")
def history(request: Request):
    conn = connect()
    return render(request, "historial.html", trips=store.trips(conn), top=store.usual(conn, 20, include_pending=True))


@app.get("/ayuda")
def help_page(request: Request):
    return render(request, "ayuda.html")


# --- for Siri Shortcuts (plain text in, one sentence out) ---------------------------------------

@app.post("/api/agregar")
async def api_add(request: Request):
    """'Oye Siri, agregá a la lista' → Dictate Text → Get Contents of URL (POST, the text as body or
    {"texto": ...}). Answers what it added, for Siri to say; X-Ids lists the added items, so the
    WhatsApp bot (mensajes/) can take them back."""
    raw = (await request.body()).decode(errors="replace").strip()
    text = raw
    if raw.startswith("{"):
        try:
            text = (await request.json()).get("texto", "")
        except ValueError:
            text = ""
        text = text if isinstance(text, str) else ""  # {"texto": 5} or null: nothing to add, not a 500
    res = store.add(connect(), text)
    return PlainTextResponse(store.added_sentence(res), headers={"X-Ids": ",".join(map(str, res["ids"]))})


@app.get("/api/lista")
def api_list():
    """'¿Qué falta comprar?': the list in one sentence, by section."""
    return PlainTextResponse(store.list_sentence(connect()))


# --- for Pedidos (pedidos/, on this Mac): what to order, and ticking what was ordered ---------------

async def _json(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


@app.get("/api/pendientes")
def api_pending():
    """What's still on the list: [{"id", "text", "key", "section"}]."""
    return [{"id": r["id"], "text": r["text"], "key": r["key"], "section": r["section"]} for r in store.pending(connect())]


@app.post("/api/leer")
async def api_read(request: Request):
    """{"texto"} → the things in it the way the list reads them ([{"text", "key", "section"}]),
    nothing stored: "pedí 2 kg de papas y bananas" for an order that isn't on the list."""
    text = (await _json(request)).get("texto")
    return [{"text": t, "key": key_of(t), "section": section_of(t)} for t in split(text if isinstance(text, str) else "")]


@app.post("/api/tildar")
async def api_tick(request: Request):
    """{"ids": [...]} → ticks those still pending (an order was sent) → {"tildados": n}."""
    ids = (await _json(request)).get("ids")
    ids = [i for i in ids if isinstance(i, int) and not isinstance(i, bool)] if isinstance(ids, list) else []
    return {"tildados": store.tick_ids(connect(), ids)}
