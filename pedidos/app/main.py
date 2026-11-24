"""Pedidos: the house orders its food delivered. It turns the shopping list (Lista) into one order
per shop, with the shop's real products and prices where it can read them (pedix catalogs), and
leaves each order one tap away from being sent: a WhatsApp link with the text written, or the
shop's site. Nothing leaves the house without a person's tap. See DISENO.md."""
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import asistente, catalogo, match, orders
from .db import connect, get_meta, set_meta
from .fetch import pedix_url

HERE = Path(__file__).parent
ALLOWED = {s.strip().lower() for s in os.environ.get("PEDIDOS_ALLOWED_LOGINS", "").split(",") if s.strip()}
PUBLIC_URL = orders.PUBLIC_URL

# Guardados keep today's prices on their own: every so often the catalogs of the shops that have one are
# read again and the guardados repriced (0: never; the pages still reprice from the stored catalog).
REFRESH_MINUTES = int(os.environ.get("PEDIDOS_REFRESH_MINUTES", "180"))
log = logging.getLogger("uvicorn.error")


def _refresh_saved():
    conn = connect()
    try:
        return orders.reprice_saved(conn, fetch=True)
    finally:
        conn.close()


async def _keep_saved_fresh():
    await asyncio.sleep(60)  # let the Mac finish starting everything else
    while True:
        try:
            n = await asyncio.to_thread(_refresh_saved)
            log.info("guardados: %d con los precios de hoy", n)
        except Exception:  # never let one bad read stop the loop
            log.exception("guardados: no pude actualizar los precios")
        await asyncio.sleep(REFRESH_MINUTES * 60)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(_keep_saved_fresh()) if REFRESH_MINUTES > 0 else None
    yield
    if task:
        task.cancel()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
app.mount("/ia", asistente.ia)  # tools for the local AI in Open WebUI (app/asistente.py)
templates = Jinja2Templates(directory=HERE / "templates")


def when(ts: str | None) -> str:
    if not ts:
        return "nunca"
    d = datetime.fromisoformat(ts)
    return f"hoy {d:%H:%M}" if d.date() == datetime.now().date() else f"{d:%d/%m %H:%M}"


templates.env.globals.update(PUBLIC_URL=PUBLIC_URL, money=orders.money, when=when, SECTIONS=orders.SECTIONS,
                             KINDS=orders.KINDS, STATUS=orders.STATUS, sections_of=orders.sections_of,
                             missing=orders.missing)


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
    PEDIDOS_ALLOWED_LOGINS get in; direct requests on this Mac (no proxy: the bot) are allowed too."""
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


def back(path: str, ok: str = "", error: str = "") -> RedirectResponse:
    q = f"?ok={quote(ok)}" if ok else f"?error={quote(error)}" if error else ""
    return RedirectResponse(path + q, status_code=303)


def _who(request: Request) -> str:
    """Who asked: the bot's X-Quien (the person's name), else the Tailscale login's user."""
    if q := request.headers.get("x-quien"):
        return unquote(q)[:60]
    return request.headers.get("tailscale-user-login", "").split("@")[0][:60]


@app.get("/health")
def health():
    connect().execute("SELECT 1")
    return {"ok": True}


# --- pages --------------------------------------------------------------------------------------------

@app.get("/")
def home(request: Request, ok: str = "", error: str = ""):
    conn = connect()
    providers = conn.execute("SELECT * FROM providers WHERE active = 1 ORDER BY id").fetchall()
    orders.reprice_saved(conn)
    return render(request, "inicio.html", open=orders.open_orders(conn), saved=orders.saved_orders(conn),
                  providers=providers, ok=ok, error=error)


@app.post("/armar")
def armar_form(request: Request, texto: str = Form(""), proveedor: str = Form("")):
    conn = connect()
    try:
        res = orders.armar(conn, texto, proveedor, _who(request))
    except orders.Unavailable as e:
        return back("/", error=str(e))
    if res["error"]:
        return back("/", error=res["error"])
    extra = f" Sin proveedor: {', '.join(res['sin_proveedor'])}." if res["sin_proveedor"] else ""
    if len(res["orders"]) == 1:
        return back(f"/p/{res['orders'][0]}", ok="Borrador listo: revisalo y mandalo." + extra)
    return back("/", ok=f"Armé {len(res['orders'])} borradores." + extra)


def _order(conn, oid: int):
    return conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()


@app.get("/p/{oid}")
def order_page(request: Request, oid: int, ok: str = "", error: str = ""):
    conn = connect()
    order = _order(conn, oid)
    if order is None:
        return RedirectResponse("/", status_code=303)
    if order["status"] == "guardado":
        orders.reprice(conn, oid)  # it shows today's prices, the ones using it will get
    p = orders.provider_of(conn, order)
    cat = match.catalog(conn, p["id"])
    rows = [{**dict(r), "alternatives": orders.alternatives(conn, p["id"], r, cat)} for r in orders.lines(conn, oid)]
    names = sorted({orders.nice(rs[0]["name"]) for rs in cat.values()})
    return render(request, "pedido.html", order=order, p=p, lines=rows, total=orders.total(conn, oid),
                  editable=order["status"] in orders.EDITABLE,
                  short=orders.short_of_minimum(conn, order), links=orders.send_links(conn, order),
                  message=orders.message(conn, oid),
                  usual=orders.usual(conn, order) if order["status"] == "borrador" else [],
                  names=names, ok=ok, error=error)


@app.post("/p/{oid}/agregar")
def add_line(oid: int, texto: str = Form(""), ext_id: str = Form(""), pres_id: str = Form("")):
    conn = connect()
    order = _order(conn, oid)
    if order is None or order["status"] not in orders.EDITABLE:
        return back(f"/p/{oid}")
    if not orders.add_line(conn, order, texto, ext_id, pres_id):
        return back(f"/p/{oid}", error="No entendí qué agregar.")
    return back(f"/p/{oid}")


@app.post("/p/{oid}/guardar")
def save_order(request: Request, oid: int, nombre: str = Form("")):
    """'Guardar para repetir': a copy of this order (any status) as a guardado."""
    conn = connect()
    order = _order(conn, oid)
    if order is None:
        return RedirectResponse("/", status_code=303)
    if not orders.lines(conn, oid):
        return back(f"/p/{oid}", error="No hay nada para guardar.")
    sid = orders.save(conn, order, nombre, _who(request))
    return back(f"/p/{sid}", ok="Guardado: lo podés usar cuando quieras.")


@app.post("/p/{oid}/usar")
def use_saved(request: Request, oid: int):
    """A guardado into its shop's draft (added to it, or a new one)."""
    conn = connect()
    saved = _order(conn, oid)
    if saved is None or saved["status"] != "guardado":
        return back(f"/p/{oid}")
    try:
        res = orders.use(conn, saved, _who(request))
    except orders.Invalid as e:
        return back(f"/p/{oid}", error=str(e))
    return back(f"/p/{res['order']}", ok=orders.use_sentence(conn, res, saved).split("\n", 1)[0])


@app.post("/p/{oid}/nombre")
def rename_saved(oid: int, nombre: str = Form("")):
    conn = connect()
    if not orders.rename(conn, oid, nombre):
        return back(f"/p/{oid}", error="Ponele un nombre.")
    return back(f"/p/{oid}")


@app.post("/p/{oid}/borrar")
def delete_saved(oid: int):
    if not orders.delete_saved(connect(), oid):
        return back(f"/p/{oid}")
    return back("/", ok="Borré el guardado.")


def _editable_line(conn, lid: int):
    """The line and its order, only while the order can be changed (a draft or a guardado)."""
    line = conn.execute("SELECT l.*, o.status FROM lines l JOIN orders o ON o.id = l.order_id WHERE l.id = ?",
                        (lid,)).fetchone()
    return line if line and line["status"] in orders.EDITABLE else None


@app.post("/l/{lid}/cambiar")
def change_line(lid: int, elegido: str = Form("")):
    conn = connect()
    line = _editable_line(conn, lid)
    if line is None:
        return RedirectResponse("/", status_code=303)
    orders.change_line(conn, line, elegido)
    return back(f"/p/{line['order_id']}")


@app.post("/l/{lid}/{op}")
def line_qty(lid: int, op: str):
    """mas, menos (down to zero takes it out) or borrar."""
    conn = connect()
    line = _editable_line(conn, lid)
    if line is None or op not in ("mas", "menos", "borrar"):
        return RedirectResponse("/", status_code=303)
    qty = line["qty"] + (1 if op == "mas" else -1 if op == "menos" else -line["qty"])
    if qty <= 0:
        conn.execute("DELETE FROM lines WHERE id = ?", (lid,))
    else:
        conn.execute("UPDATE lines SET qty = ? WHERE id = ?", (min(qty, 99), lid))
    orders.recompute(conn, line["order_id"])
    return back(f"/p/{line['order_id']}")


@app.post("/p/{oid}/{status}")
def order_status(oid: int, status: str):
    """enviado ('Ya lo pedí'), recibido ('Llegó'), cancelar. Also the bot's "borrar" (cancelar). Declared
    after the other /p/{oid}/... POSTs: FastAPI takes the first route that matches."""
    target = {"enviado": "enviado", "recibido": "recibido", "cancelar": "cancelado"}.get(status)
    if target is None:
        return back(f"/p/{oid}")
    return back(f"/p/{oid}", ok=orders.set_status(connect(), oid, target))


@app.get("/historial")
def history(request: Request):
    rows = connect().execute("SELECT o.*, p.name AS provider FROM orders o JOIN providers p ON p.id = o.provider_id"
                             " WHERE o.status NOT IN ('borrador', 'guardado')"
                             " ORDER BY COALESCE(o.sent_at, o.created_at) DESC, o.id DESC"
                             " LIMIT 60").fetchall()
    return render(request, "historial.html", rows=rows)


@app.get("/proveedores")
def providers_page(request: Request, ok: str = "", error: str = ""):
    conn = connect()
    rows = conn.execute("SELECT p.*, (SELECT COUNT(DISTINCT ext_id) FROM products WHERE provider_id = p.id) AS n"
                        " FROM providers p ORDER BY active DESC, id").fetchall()
    return render(request, "proveedores.html", rows=rows, ok=ok, error=error)


@app.post("/proveedores")
def add_provider(nombre: str = Form(""), link: str = Form(""), sections: list[str] = Form([])):
    conn = connect()
    try:
        kind, url, phone = orders.classify(link)
    except orders.Invalid as e:
        return back("/proveedores", error=str(e))
    name = " ".join(nombre.split())[:60] or (url.rsplit("/", 1)[-1].replace("-", " ").title() if kind == "pedix" else "")
    if not name:
        return back("/proveedores", error="Ponele un nombre.")
    pid = conn.execute("INSERT INTO providers (name, kind, url, phone, sections) VALUES (?, ?, ?, ?, ?)",
                       (name, kind, url, phone, ",".join(s for s in sections if s in orders.SECTIONS))).lastrowid
    conn.commit()
    msg = f"Agregué {name}."
    if kind == "pedix":
        p = conn.execute("SELECT * FROM providers WHERE id = ?", (pid,)).fetchone()
        err = catalogo.refresh(conn, p, force=True)
        msg += f" No pude leer el catálogo: {err}" if err else " Leí su catálogo."
    return back("/proveedores", ok=msg)


@app.post("/proveedores/{pid}")
def edit_provider(pid: int, nombre: str = Form(""), url: str = Form(""), phone: str = Form(""),
                  sections: list[str] = Form([]), minimo: str = Form(""), note: str = Form(""), active: str = Form("")):
    conn = connect()
    p = conn.execute("SELECT * FROM providers WHERE id = ?", (pid,)).fetchone()
    if p is None:
        return back("/proveedores")
    try:
        if p["kind"] == "pedix":
            url = pedix_url(url) or ""
            if not url:
                raise orders.Invalid("El link de pedix tiene que ser https://pedix.app/<tienda>.")
        elif p["kind"] == "web":
            url = orders.web_url(url) if url.strip() else ""
        else:
            url = ""
        phone = orders.phone_of(phone) if phone.strip() else ""
    except orders.Invalid as e:
        return back("/proveedores", error=f"{p['name']}: {e}")
    digits = minimo.replace(".", "").replace("$", "").strip()
    conn.execute("UPDATE providers SET name = ?, url = ?, phone = ?, sections = ?, min_purchase = ?, note = ?, active = ?"
                 " WHERE id = ?",
                 (" ".join(nombre.split())[:60] or p["name"], url, phone,
                  ",".join(s for s in sections if s in orders.SECTIONS), float(digits) if digits.isdigit() else None,
                  note.strip()[:300], 1 if active else 0, pid))
    conn.commit()
    return back("/proveedores", ok=f"Guardé {nombre.strip() or p['name']}.")


@app.post("/proveedores/{pid}/actualizar")
def refresh_provider(pid: int):
    conn = connect()
    p = conn.execute("SELECT * FROM providers WHERE id = ?", (pid,)).fetchone()
    if p is None or p["kind"] != "pedix":
        return back("/proveedores")
    err = catalogo.refresh(conn, p, force=True)
    return back("/proveedores", error=f"{p['name']}: {err}") if err else back("/proveedores", ok=f"Precios de {p['name']} al día.")


META = ("direccion", "nombre", "aclaracion", "telefono", "pago", "referencia")


@app.get("/ajustes")
def settings_page(request: Request, ok: str = ""):
    conn = connect()
    return render(request, "ajustes.html", meta={k: get_meta(conn, k) or "" for k in META}, ok=ok)


@app.post("/ajustes")
def settings_save(direccion: str = Form(""), nombre: str = Form(""), aclaracion: str = Form(""),
                  telefono: str = Form(""), pago: str = Form(""), referencia: str = Form("")):
    conn = connect()
    for k, v in zip(META, (direccion, nombre, aclaracion, telefono, pago, referencia)):
        set_meta(conn, k, " ".join(v.split())[:200])
    return back("/ajustes", ok="Guardado.")


@app.get("/ayuda")
def help_page(request: Request):
    return render(request, "ayuda.html")


# --- for the bot and Siri (plain text in, one sentence out) -----------------------------------------

def _str(data: dict, key: str) -> str:
    v = data.get(key)
    return v.strip() if isinstance(v, str) else ""


async def _body(request: Request) -> tuple[dict, str]:
    """A JSON object (else {}) and the raw text, for endpoints that take either."""
    raw = (await request.body()).decode("utf-8", "replace").strip()
    data = {}
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
    return data, raw


@app.post("/api/armar")
async def api_armar(request: Request):
    """{"texto"?, "proveedor"?, "quien"?} (or the things as a plain body). Answers one block per
    draft; X-Ids has the order ids, so the bot's "borrar" can cancel them (POST /p/{id}/cancelar).
    Lista down or nothing to order: a sentence, never a 500."""
    data, raw = await _body(request)
    texto = _str(data, "texto") if data or raw.startswith("{") else raw
    conn = connect()
    try:
        res = orders.armar(conn, texto, _str(data, "proveedor"), _str(data, "quien") or _who(request))
    except orders.Unavailable as e:
        return PlainTextResponse(str(e), headers={"X-Ids": ""})
    return PlainTextResponse(orders.armar_sentence(conn, res), headers={"X-Ids": ",".join(map(str, res["orders"]))})


@app.get("/api/pedidos")
def api_orders():
    """'¿Qué pedidos hay?': the open orders (drafts and sent, not arrived), a sentence each."""
    return PlainTextResponse(orders.open_sentence(connect()))


@app.get("/api/p/{oid}")
def api_order(oid: int):
    """One order as data, for Claude Code on this Mac (.claude/skills/pedidos): what the draft page
    shows, with each line's id and choices, so it can be changed through the same POSTs the page uses."""
    conn = connect()
    order = _order(conn, oid)
    if order is None:
        return JSONResponse({"error": "Ese pedido no existe."}, status_code=404)
    if order["status"] == "guardado":
        orders.reprice(conn, oid)
    p = orders.provider_of(conn, order)
    cat = match.catalog(conn, p["id"]) if order["status"] in orders.EDITABLE else {}
    return {"id": oid, "nombre": order["name"], "proveedor": p["name"], "estado": orders.STATUS[order["status"]],
            "creado": order["created_at"], "por": order["created_by"], "pagina": orders.link(oid),
            "total_estimado": orders.total(conn, oid), "falta_para_el_minimo": orders.short_of_minimum(conn, order),
            "mandar": orders.send_links(conn, order), "mensaje": orders.message(conn, oid),
            "renglones": [{"id": r["id"], "texto": r["text"], "cantidad": r["qty"], "producto": r["product"],
                           "presentacion": r["presentation"], "precio_unitario": r["unit_price"],
                           "en_catalogo": r["ext_id"] is not None,
                           "opciones": orders.alternatives(conn, p["id"], r, cat) if cat else []}
                          for r in orders.lines(conn, oid)]}


@app.get("/api/guardados")
def api_saved():
    """'¿Qué pedidos guardados hay?': name, shop, things and total of each."""
    conn = connect()
    orders.reprice_saved(conn)
    return PlainTextResponse(orders.saved_sentence(conn))


@app.post("/api/usar")
async def api_use(request: Request):
    """{"nombre", "quien"?} (or the name as a plain body): that guardado into its shop's draft. X-Ids
    has the draft's id. A name that fits none: which ones there are, never a 500."""
    data, raw = await _body(request)
    said = _str(data, "nombre") if data or raw.startswith("{") else raw
    conn = connect()
    saved = orders.find_saved(conn, said)
    if saved is None:
        return PlainTextResponse(orders.unknown_saved_sentence(conn, said), headers={"X-Ids": ""})
    try:
        res = orders.use(conn, saved, _str(data, "quien") or _who(request))
    except orders.Invalid as e:
        return PlainTextResponse(str(e), headers={"X-Ids": ""})
    return PlainTextResponse(orders.use_sentence(conn, res, saved), headers={"X-Ids": str(res["order"])})


@app.post("/api/guardar")
async def api_save(request: Request):
    """{"id", "nombre"?}: that order saved as a guardado (empty name: the shop's). X-Ids has its id."""
    data, _ = await _body(request)
    oid = data.get("id")
    conn = connect()
    order = _order(conn, int(oid)) if str(oid).isdigit() else None
    if order is None:
        return PlainTextResponse("Ese pedido no existe.", headers={"X-Ids": ""})
    if not orders.lines(conn, order["id"]):
        return PlainTextResponse("Ese pedido está vacío: no hay nada para guardar.", headers={"X-Ids": ""})
    sid = orders.save(conn, order, _str(data, "nombre"), _str(data, "quien") or _who(request))
    name = _order(conn, sid)["name"]
    if sid == order["id"]:
        return PlainTextResponse(f"Ya está guardado como «{name}».", headers={"X-Ids": str(sid)})
    return PlainTextResponse(f"Guardado como «{name}». Para usarlo: {orders.link(sid)}", headers={"X-Ids": str(sid)})


@app.get("/api/precio")
def api_price(q: str = ""):
    """'¿Cuánto está la banana?': what the catalogs have for it."""
    return PlainTextResponse(orders.price_sentence(connect(), q))
