"""Mesa: Claude invests 10,000 play dollars in US stocks and ETFs, against SPY bought and held (the
vara). Claude decides through POST /api/decisiones twice a week; this app owns everything else:
the limits (app/reglas.py), the paper fills, dividends and splits (app/cartera.py), Yahoo's bars
(app/precios.py), the background thread (app/tareas.py), the pages and the notifications. See DISENO.md.

Pages: /, /decisiones[/<id>], /posiciones/<ticker>, /operaciones, /reglas, /ajustes.
API: /health, /api/frase, /api/estado, /api/cartera, /api/tenencias.csv, /api/ticker/<T>,
/api/decisiones (GET and POST), /accion/mesa/pausar|reanudar, /accion/avisos/prueba.
For the Asistente of Open WebUI: /ia/openapi.json (app/asistente.py, read-only).
"""
import csv
import io
import json
import logging
import os
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlparse

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Undefined
from starlette.concurrency import run_in_threadpool

from . import asistente, cartera, db, graficos, notify, precios, reglas, tareas

HERE = Path(__file__).parent
log = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(_: FastAPI):
    conn = db.conectar()  # the schema and the two portfolios, before the first request
    try:
        anotar_cambios_de_reglas(conn)
    finally:
        conn.close()
    tareas.arrancar()
    yield
    tareas.parar()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
app.mount("/ia", asistente.ia)  # read-only tools for the local AI in Open WebUI (app/asistente.py)
templates = Jinja2Templates(directory=HERE / "templates")

ALLOWED = {s.strip().lower() for s in os.environ.get("MESA_ALLOWED_LOGINS", "").split(",") if s.strip()}

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
PUERTOS = {"http": 80, "https": 443}
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


# --- who may ask (Tailscale login) and from where (CSRF): as in the previous Mesa ----------

def origen(url: str) -> tuple[str, str, int] | None:
    """(scheme, host, port) of a URL or an Origin header, the port filled in; None if it isn't one."""
    u = urlparse(url)
    try:
        puerto = u.port
    except ValueError:
        return None
    if not u.scheme or not u.hostname:
        return None
    esquema = u.scheme.lower()
    return esquema, u.hostname.lower(), puerto or PUERTOS.get(esquema, 0)


def origen_propio(request: Request) -> tuple[str, str, int] | None:
    """This app's origin as the browser sees it. Through tailscale serve the browser talks HTTPS to
    mac-server.tailnet-ejemplo.ts.net:3020 and serve forwards plain HTTP with X-Forwarded-Proto and
    X-Forwarded-Host (a browser can't set those on a cross-site request); on this Mac, the Host it used.
    Proxied without X-Forwarded-Proto: https, since serve only publishes Mesa over HTTPS."""
    proxied = "x-forwarded-for" in request.headers
    esquema = (request.headers.get("x-forwarded-proto") or ("https" if proxied else request.url.scheme))
    esquema = esquema.split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    return origen(f"{esquema}://{host}") if host else None


def same_site(request: Request) -> bool:
    """A form or script on another web site can make the browser POST here (CSRF): browsers say so
    in Sec-Fetch-Site and Origin. Only this very origin counts (scheme, host and port): the other
    services on mac-server are other origins on the same host, and "same-site" would let them in.
    Siri, n8n, Mensajes' bot and Claude Code's curl (server to server) send neither header."""
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        return False
    o = request.headers.get("origin")
    if o is None:
        return True
    if o == "null":
        return site is not None
    propio = origen_propio(request)
    return propio is not None and origen(o) == propio


@app.middleware("http")
async def tailnet_owner_only(request: Request, call_next):
    """tailscale serve tells us who is asking (Tailscale-User-Login). Only the people in
    MESA_ALLOWED_LOGINS get in; direct requests on this Mac (no proxy) are allowed too."""
    if request.method not in SAFE_METHODS and not same_site(request):
        return JSONResponse({"error": "Pedido desde otro sitio: rechazado."}, status_code=403)
    if request.url.path != "/health":
        login = request.headers.get("tailscale-user-login", "").lower()
        proxied = "x-forwarded-for" in request.headers
        if (login and ALLOWED and login not in ALLOWED) or (proxied and not login):
            return JSONResponse({"error": "Solo para el dueño del tailnet."}, status_code=403)
    return await call_next(request)


# --- formatting (es-AR; Córdoba on screen: the container's TZ) -----------------------------

def nada(x) -> bool:
    return x is None or isinstance(x, Undefined)


def num(x: float | None, dec: int = 2) -> str:
    """68.432,10 (rioplatense)."""
    if nada(x):
        return "—"
    return f"{x:,.{dec}f}".replace(",", "_").replace(".", ",").replace("_", ".")


def entero(n) -> str:
    return "—" if nada(n) else f"{int(n):,}".replace(",", ".")


def signo(x: float | None, dec: int = 2) -> str:
    """+28,63 / −12,00 (a real minus sign)."""
    if nada(x):
        return "—"
    return ("+" if x >= 0 else "−") + num(abs(x), dec)


def pct(x: float | None, con_signo: bool = True, dec: int = 1) -> str:
    """A fraction as a percentage: 0.0123 → '+1,2 %'."""
    if nada(x):
        return "—"
    t = f"{abs(x) * 100:.{dec}f} %".replace(".", ",")
    return (("+" if x >= 0 else "−") + t) if con_signo else ("−" + t if x < 0 else t)


def pts(x: float | None) -> str:
    """A difference of fractions in points: +1,2 pts."""
    return "—" if nada(x) else signo(x * 100, 1) + " pts"


def cant(x: float | None) -> str:
    """Shares: up to 6 decimals, the trailing zeros off."""
    if nada(x):
        return "—"
    t = f"{x:,.6f}".rstrip("0").rstrip(".")
    return t.replace(",", "_").replace(".", ",").replace("_", ".")


def clase_signo(x: float | None) -> str:
    return "" if not x else "pos" if x > 0 else "neg"


def local(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000)


def hora(ms: int | None) -> str:
    """'hoy 10:00', 'ayer 21:00', '3 nov 21:00', '3 nov 2025' (Córdoba)."""
    if nada(ms):
        return "—"
    t, hoy = local(ms), date.today()
    if t.date() == hoy:
        return f"hoy {t:%H:%M}"
    if t.date() == hoy - timedelta(days=1):
        return f"ayer {t:%H:%M}"
    if t.year == hoy.year:
        return f"{t.day} {MESES[t.month - 1]} {t:%H:%M}"
    return f"{t.day} {MESES[t.month - 1]} {t.year}"


def dia(iso: str | None) -> str:
    """'2026-11-13' → '13 nov 2026'."""
    if nada(iso) or not iso:
        return "—"
    d = date.fromisoformat(iso[:10])
    return f"{d.day} {MESES[d.month - 1]} {d.year}"


def hace(ms: int | None) -> str:
    if nada(ms):
        return "nunca"
    s = max(0, (db.ahora_ms() - ms) // 1000)
    if s < 3600:
        return f"hace {s // 60} min"
    if s < 86400:
        return f"hace {s // 3600} h"
    return f"hace {s // 86400} días"


LADOS = {"compra": "Compra", "venta": "Venta"}
ESTADOS = {"pendiente": "pendiente", "ejecutada": "ejecutada", "rechazada": "rechazada", "cancelada": "cancelada"}
TIPOS_TESIS = {"apertura": "Apertura", "refuerzo": "Refuerzo", "revision": "Revisión", "cierre": "Cierre"}

templates.env.globals.update(num=num, entero=entero, signo=signo, pct=pct, pts=pts, cant=cant, clase_signo=clase_signo,
                             hora=hora, dia=dia, hace=hace, LADOS=LADOS, TIPOS_TESIS=TIPOS_TESIS,
                             dos_curvas=graficos.dos_curvas)


def aqui(request: Request) -> str:
    q = [(k, v) for k, v in parse_qsl(request.url.query) if k not in ("ok", "error")]
    return request.url.path + ("?" + urlencode(q) if q else "")


def render(request: Request, name: str, **ctx):
    return templates.TemplateResponse(request, name, {
        "page": name.split(".")[0], "aqui": aqui(request), "ok": request.query_params.get("ok"),
        "error": request.query_params.get("error"), **ctx})


def volver_a(destino, default: str = "/") -> str:
    """Only redirect within the app (no '//other.site', nor '/\\other.site')."""
    if not isinstance(destino, str) or not destino.startswith("/") or destino[1:2] in ("/", "\\"):
        return default
    return destino


def con_mensaje(destino: str, **mensaje) -> str:
    u = urlparse(destino)
    q = [(k, v) for k, v in parse_qsl(u.query) if k not in ("ok", "error")] + [(k, v) for k, v in mensaje.items() if v]
    return u.path + ("?" + urlencode(q) if q else "") + (f"#{u.fragment}" if u.fragment else "")


def quien(request: Request) -> str:
    """Who asks: the Tailscale login (through tailscale serve); on this Mac, the name Mensajes sends
    in X-Quien (the person who wrote to the bot), else just this Mac."""
    if login := request.headers.get("tailscale-user-login"):
        return login
    if name := unquote(request.headers.get("x-quien", "")).strip():
        return f"{name[:60]} (Telegram)"
    return "local (Mac)"


@contextmanager
def abrir():
    conn = db.conectar()
    try:
        yield conn
    finally:
        conn.close()


# --- the rules as data, and their changes -------------------------------------------------

def anotar_cambios_de_reglas(conn):
    """The numbers come from the environment. Once the Mesa started, a number that differs from the
    last one seen is noted in `cambios` (DISENO: the verdict is read knowing what changed)."""
    actuales = {k: v for k, v in reglas.todos().items()}
    previas = db.leer(conn, "reglas")
    with db.transaccion(conn):
        if previas is not None and cartera.empezo(conn):
            for k, v in actuales.items():
                if k in previas and previas[k] != v:
                    conn.execute("INSERT INTO cambios (ts, quien, campo, antes, despues, motivo) VALUES (?,?,?,?,?,?)",
                                 (db.ahora_ms(), "entorno (compose.yml)", k, json.dumps(previas[k]), json.dumps(v),
                                  f"La variable {reglas.NUMEROS[k][0]} cambió al reiniciar."))
        if previas != actuales:
            db.guardar(conn, "reglas", actuales)


def reglas_json() -> dict:
    n = reglas.todos()
    return {
        "numeros": n,
        "universo": {"tipos": ["EQUITY", "ETF"], "moneda": "USD", "bolsas": sorted(set(reglas.BOLSAS.values())),
                     "precio_min_usd": n["precio_min_usd"], "volumen_min_usd_mediana_20": n["volumen_min_usd"],
                     "prohibidos_por_nombre": ["2x", "3x", "leveraged", "inverse", "ultrapro", "proshares ultra",
                                               "direxion daily", "bear", "ETN"],
                     "nota": "Nada de opciones, futuros, cripto, cortos, margen ni ETF/ETN apalancados o inversos. "
                             "El efectivo no rinde: para estacionar plata, SGOV o BIL."},
        "limites": {"peso_max_accion_pct": n["max_accion_pct"], "peso_max_etf_pct": n["max_etf_pct"],
                    "ordenes_por_decision": n["max_ordenes"], "decisiones_con_ordenes_por_dia": n["max_decisiones_dia"],
                    "decisiones_con_ordenes_por_semana": n["max_decisiones_semana"],
                    "tesis_min_caracteres": n["tesis_min_caracteres"], "horizonte_min_meses": n["horizonte_min_meses"],
                    "venta_anticipada_dias": n["venta_anticipada_dias"], "orden_min_usd": n["orden_min_usd"]},
        "costos": {"deslizamiento_pb": n["deslizamiento_pb"], "comision_usd_por_orden": n["comision_usd"],
                   "retencion_dividendos_pct": n["retencion_dividendos_pct"]},
        "ejecucion": "A la apertura de la primera rueda que abre después de creada la orden; primero las ventas.",
        "vara": "SPY con el mismo capital, comprado en la misma apertura que la primera operación de Claude, "
                "con los mismos costos y los dividendos netos reinvertidos.",
        "veredicto": f"Una sola vez, a los {n['veredicto_meses']} meses de la primera ejecución: pasa si el retorno de Claude "
                     f"es mayor o igual al de la vara y su peor caída no la supera en más de {n['veredicto_caida_pts']} puntos.",
    }


# --- the state, shared by the pages, /api/estado, /api/cartera and the Asistente ------------

def _orden(o) -> dict:
    d = dict(o)
    d["todo"] = bool(d["todo"])
    d["automatica"] = bool(d["automatica"])
    return d


def decision_dict(conn, f, con_ordenes: bool = True) -> dict:
    d = {"id": f["id"], "ts": f["ts"], "quien": f["quien"], "sesion": f["sesion"], "resumen": f["resumen"],
         "regimen": json.loads(f["regimen_json"]) if f["regimen_json"] else None,
         "skills_usadas": json.loads(f["skills_json"] or "[]"), "con_ordenes": bool(f["con_ordenes"]),
         "lecciones": f["lecciones"]}
    if con_ordenes:
        claude = db.cartera_id(conn, "claude")
        d["ordenes"] = [_orden(o) for o in conn.execute(
            "SELECT * FROM ordenes WHERE decision_id = ? AND cartera_id = ? ORDER BY id", (f["id"], claude))]
        d["tesis"] = [{**dict(t), "fuentes": json.loads(t["fuentes_json"] or "[]")} for t in conn.execute(
            "SELECT * FROM tesis WHERE decision_id = ? ORDER BY id", (f["id"],))]
    return d


def ultimas_decisiones(conn, n: int = 20, ticker: str | None = None) -> list[dict]:
    if ticker:
        filas = conn.execute("SELECT * FROM decisiones WHERE id IN (SELECT decision_id FROM ordenes WHERE ticker = ? "
                             "UNION SELECT decision_id FROM tesis WHERE ticker = ?) ORDER BY ts DESC, id DESC LIMIT ?",
                             (ticker, ticker, n)).fetchall()
    else:
        filas = conn.execute("SELECT * FROM decisiones ORDER BY ts DESC, id DESC LIMIT ?", (n,)).fetchall()
    return [decision_dict(conn, f) for f in filas]


def estado(conn) -> dict:
    """Everything the home page shows."""
    claude, vara = db.cartera_id(conn, "claude"), db.cartera_id(conn, "vara")
    mc, mv = cartera.metricas(conn, claude), cartera.metricas(conn, vara)
    ult = conn.execute("SELECT * FROM decisiones ORDER BY ts DESC, id DESC LIMIT 1").fetchone()
    pend = [{**_orden(o), "cartera": "vara" if o["cartera_id"] == vara else "claude"} for o in cartera.pendientes(conn)]
    exceso = (mc["retorno"] - mv["retorno"]) if mc["retorno"] is not None and mv["retorno"] is not None else None
    ini = cartera.inicio_ts(conn)
    return {"empezo": ini is not None, "desde": precios.fecha_ny(ini) if ini else None, "claude": mc, "vara": mv,
            "exceso": exceso, "posiciones": cartera.detalle_posiciones(conn, claude),
            "posiciones_vara": cartera.detalle_posiciones(conn, vara), "pendientes": pend,
            "ultima_decision": decision_dict(conn, ult) if ult else None, "pausada": bool(db.leer(conn, "pausada", False)),
            "veredicto": cartera.veredicto(conn), "veredicto_fijo": db.leer(conn, "veredicto"),
            "proxima_sesion": reglas.proxima_sesion(db.ahora_ms()), "frase": tareas.frase(conn),
            "curva_claude": [(precios.cierre_ms(f), v) for f, v in cartera.curva(conn, claude)],
            "curva_vara": [(precios.cierre_ms(f), v) for f, v in cartera.curva(conn, vara)]}


def salud(conn) -> tuple[bool, str]:
    ult = db.leer(conn, "ultima_bajada")
    ref = ult if ult is not None else db.leer(conn, "creada", db.ahora_ms())
    dias = reglas.numero("salud_dias")
    if db.ahora_ms() - ref > dias * 86400_000:
        if ult is None:
            return False, f"Nunca bajé precios de Yahoo y la base tiene más de {dias} días."
        return False, f"La última bajada buena de Yahoo fue {hace(ult)}: más de {dias} días."
    return True, "ok"


# --- pages ----------------------------------------------------------------------------------

@app.get("/health", response_class=PlainTextResponse)
def health():
    try:
        with abrir() as conn:
            ok, motivo = salud(conn)
    except Exception as e:
        return PlainTextResponse(f"No puedo abrir la base: {e.__class__.__name__}", status_code=503)
    return PlainTextResponse(motivo, status_code=200 if ok else 503)


@app.get("/")
def home(request: Request):
    with abrir() as conn:
        e = estado(conn)
    return render(request, "mesa.html", e=e)


@app.get("/decisiones")
def decisiones_pagina(request: Request, ticker: str | None = None, n: int = 50):
    t = reglas.normalizar(ticker) if ticker else None
    with abrir() as conn:
        filas = ultimas_decisiones(conn, max(1, min(n, 200)), t)
    return render(request, "decisiones.html", decisiones=filas, ticker=t)


@app.get("/decisiones/{did}")
def decision_pagina(request: Request, did: int):
    with abrir() as conn:
        f = conn.execute("SELECT * FROM decisiones WHERE id = ?", (did,)).fetchone()
        if f is None:
            return render(request, "vacio.html", titulo="Decisión", texto=f"No hay una decisión {did}.")
        d = decision_dict(conn, f)
        vara = conn.execute("SELECT * FROM ordenes WHERE decision_id = ? AND automatica = 1", (did,)).fetchall()
    return render(request, "decision.html", d=d, vara=[_orden(o) for o in vara])


@app.get("/posiciones/{ticker}")
def posicion_pagina(request: Request, ticker: str):
    t = reglas.normalizar(ticker)
    with abrir() as conn:
        claude = db.cartera_id(conn, "claude")
        actual = next((p for p in cartera.detalle_posiciones(conn, claude) if p["ticker"] == t), None)
        historia = [{**dict(x), "fuentes": json.loads(x["fuentes_json"] or "[]")} for x in conn.execute(
            "SELECT * FROM tesis WHERE cartera_id = ? AND ticker = ? ORDER BY ts DESC, id DESC", (claude, t))]
        ords = [_orden(o) for o in conn.execute("SELECT * FROM ordenes WHERE cartera_id = ? AND ticker = ? ORDER BY creada_ts DESC, id DESC",
                                                (claude, t))]
        divs = conn.execute("SELECT * FROM eventos WHERE cartera_id = ? AND ticker = ? ORDER BY fecha DESC", (claude, t)).fetchall()
        cierres = conn.execute("SELECT * FROM cierres WHERE cartera_id = ? AND ticker = ? ORDER BY cerrada_ts DESC", (claude, t)).fetchall()
        m = precios.meta(conn, t)
    if not historia and not ords:
        return render(request, "vacio.html", titulo=t, texto=f"La cartera de Claude nunca tuvo {t}.")
    return render(request, "posicion.html", t=t, p=actual, historia=historia, ordenes=ords, eventos=divs, cierres=cierres,
                  nombre=m["nombre"] if m else None)


@app.get("/operaciones")
def operaciones_pagina(request: Request, ver: str = Query("claude", alias="cartera")):
    clave = ver if ver in ("claude", "vara") else "claude"
    with abrir() as conn:
        cid = db.cartera_id(conn, clave)
        ords = [_orden(o) for o in conn.execute("SELECT * FROM ordenes WHERE cartera_id = ? AND estado != 'pendiente' "
                                                "ORDER BY COALESCE(ejecutada_ts, creada_ts) DESC, id DESC LIMIT 300", (cid,))]
        eventos = conn.execute("SELECT * FROM eventos WHERE cartera_id = ? ORDER BY fecha DESC LIMIT 200", (cid,)).fetchall()
        m = cartera.metricas(conn, cid)
    return render(request, "operaciones.html", ordenes=ords, eventos=eventos, m=m, clave=clave)


@app.get("/reglas")
def reglas_pagina(request: Request):
    with abrir() as conn:
        cambios = conn.execute("SELECT * FROM cambios ORDER BY ts DESC").fetchall()
        empezo = cartera.empezo(conn)
    return render(request, "reglas.html", r=reglas_json(), numeros=reglas.descripcion(), cambios=cambios,
                  empezo=empezo, BOLSAS=reglas.BOLSAS)


@app.get("/ajustes")
def ajustes_pagina(request: Request):
    with abrir() as conn:
        pausa = db.leer_con_ts(conn, "pausada")
        quien_pausa = db.leer(conn, "pausada_por")
        ok, motivo = salud(conn)
        ultima = db.leer(conn, "ultima_bajada")
        latido = db.leer(conn, "latido")
        cierre = db.leer(conn, "ultimo_cierre")
    return render(request, "ajustes.html", pausada=bool(pausa and pausa[0]), pausa_ts=pausa[1] if pausa else None,
                  pausada_por=quien_pausa, salud_ok=ok, salud_motivo=motivo, ultima_bajada=ultima, latido=latido,
                  ultimo_cierre=cierre, notify_on=notify.activo(), tareas_on=tareas.activas())


# --- actions (forms on the pages, or JSON from Mensajes) ----------------------------------

@app.post("/accion/{ruta:path}")
async def accion(request: Request, ruta: str):
    ctype = request.headers.get("content-type", "")
    quiere_json = "application/json" in request.headers.get("accept", "") or "application/json" in ctype
    if "application/json" in ctype:
        try:
            form = await request.json()
        except ValueError:
            form = {}
        form = form if isinstance(form, dict) else {}
    else:
        form = dict(await request.form())
    volver = volver_a(form.get("volver"), "/ajustes")

    def responder(status: int, cuerpo: dict):
        if quiere_json:
            return JSONResponse(cuerpo, status_code=status)
        if status == 200:
            return RedirectResponse(con_mensaje(volver, ok=cuerpo.get("mensaje") or "Hecho."), status_code=303)
        return RedirectResponse(con_mensaje(volver, error=cuerpo.get("error") or "No se pudo."), status_code=303)

    if ruta in ("mesa/pausar", "mesa/reanudar"):
        pausar = ruta == "mesa/pausar"
        q = quien(request)
        with abrir() as conn:
            antes = bool(db.leer(conn, "pausada", False))
            with db.transaccion(conn):
                if antes != pausar:
                    db.guardar(conn, "pausada", pausar)
                    db.guardar(conn, "pausada_por", q)
        if pausar:
            msg = "La mesa ya estaba en pausa." if antes else "Mesa en pausa: no entra ninguna decisión con órdenes. Lo pendiente se ejecuta igual."
        else:
            msg = "La mesa no estaba en pausa." if not antes else "Mesa reanudada: Claude puede volver a operar."
        return responder(200, {"ok": True, "mensaje": msg, "pausada": pausar})
    if ruta == "avisos/prueba":
        if not notify.activo():
            return responder(400, {"ok": False, "error": "Los avisos están apagados (MESA_NOTIFY no es 1)."})
        notify.avisar(*notify.PRUEBA, tags="test_tube")
        return responder(200, {"ok": True, "mensaje": "Aviso de prueba en camino."})
    return responder(404, {"ok": False, "error": f"No hay una acción {ruta}."})


# --- the API --------------------------------------------------------------------------------

@app.get("/api/frase", response_class=PlainTextResponse)
def api_frase():
    with abrir() as conn:
        return tareas.frase(conn)


@app.get("/api/estado")
def api_estado():
    with abrir() as conn:
        e = estado(conn)
    mc, mv = e["claude"], e["vara"]
    u = e["ultima_decision"]
    v = e["veredicto"]
    return {"empezo": e["empezo"], "desde": e["desde"], "valor": _r(mc["valor"]), "retorno_pct": _p(mc["retorno"]),
            "vara_valor": _r(mv["valor"]), "vara_retorno_pct": _p(mv["retorno"]),
            "exceso_pts": _p(e["exceso"]),
            "efectivo_pct": _p(mc["efectivo"] / mc["valor"]) if mc["valor"] else None,
            "posiciones": [{"ticker": p["ticker"], "peso_pct": _p(p["peso"]), "pnl_pct": _p(p["pnl_pct"])} for p in e["posiciones"]],
            "pendientes": len(e["pendientes"]), "pausada": e["pausada"],
            "ultima_decision": {"ts": u["ts"], "resumen": u["resumen"]} if u else None,
            "proxima_sesion": e["proxima_sesion"], "frase": e["frase"],
            "dias_para_veredicto": v.get("dias") if v.get("empezo") else None}


def _r(x, d=2):
    return None if x is None else round(x, d)


def _p(x):
    return None if x is None else round(x * 100, 2)


def _metricas_json(m: dict) -> dict:
    return {"valor": _r(m["valor"]), "efectivo": _r(m["efectivo"]), "capital_inicial": m["capital"],
            "retorno_pct": _p(m["retorno"]), "max_drawdown_pct": _p(m["max_drawdown"]),
            "rotacion_anualizada": _r(m["rotacion_anualizada"], 3), "costos_usd": _r(m["costos"]),
            "comisiones_usd": _r(m["comisiones"]), "deslizamiento_usd": _r(m["deslizamiento"]),
            "dividendos_netos_usd": _r(m["dividendos_netos"]), "posiciones_cerradas": m["cerradas"],
            "cerradas_ganadoras": m["ganadoras"], "inicio": precios.fecha_ny(m["inicio_ts"]) if m["inicio_ts"] else None}


def _posicion_json(p: dict) -> dict:
    t = p["tesis"]
    return {"ticker": p["ticker"], "nombre": p["nombre"], "tipo": p["tipo"], "cantidad": p["cantidad"],
            "precio_prom": _r(p["precio_prom"], 4), "precio": _r(p["precio"], 4), "valor": _r(p["valor"]),
            "peso_pct": _p(p["peso"]), "pnl_usd": _r(p["pnl"]), "pnl_pct": _p(p["pnl_pct"]),
            "abierta": precios.fecha_ny(p["abierta_ts"]),
            "peso_maximo_pct": reglas.peso_maximo_pct(p["tipo"]),
            "tesis": t["texto"] if t else None, "horizonte_meses": t["horizonte_meses"] if t else None,
            "invalida": t["invalida"] if t else None, "fuentes": t["fuentes"] if t else [],
            "revisar_el": t["revisar_el"] if t else None,
            "revisar_vencido": bool(t and t["revisar_el"] and t["revisar_el"] <= precios.fecha_ny(db.ahora_ms())),
            "ultima_revision": t["ultima_revision"] if t else None}


@app.get("/api/cartera")
def api_cartera():
    with abrir() as conn:
        e = estado(conn)
        dec = ultimas_decisiones(conn, 5)
    mc, mv = e["claude"], e["vara"]
    return {"ts": db.ahora_ms(), "empezo": e["empezo"], "desde": e["desde"], "pausada": e["pausada"],
            "reglas": reglas_json(),
            "claude": {**_metricas_json(mc), "posiciones": [_posicion_json(p) for p in e["posiciones"]]},
            "vara": {**_metricas_json(mv), "posiciones": [{"ticker": p["ticker"], "cantidad": p["cantidad"],
                                                            "valor": _r(p["valor"])} for p in e["posiciones_vara"]]},
            "exceso_pts": _p(e["exceso"]),
            "pendientes": [{k: o[k] for k in ("id", "cartera", "decision_id", "ticker", "lado", "monto_usd", "cantidad",
                                              "todo", "creada_ts", "automatica")} for o in e["pendientes"]],
            "veredicto": e["veredicto_fijo"] or e["veredicto"],
            "ultimas_decisiones": dec, "proxima_sesion": e["proxima_sesion"], "frase": e["frase"],
            "nota": ("Todavía no empezó: la primera decisión con órdenes también compra la vara (SPY) a la misma apertura."
                     if not e["empezo"] else None)}


@app.get("/api/tenencias.csv")
def api_tenencias():
    """portfolio-manager/scripts/review_csv.py's format: ETFs and stocks both as 'equity'; cost_basis per share."""
    with abrir() as conn:
        pos = cartera.detalle_posiciones(conn, db.cartera_id(conn, "claude"))
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["symbol", "asset_type", "quantity", "direction", "price", "currency", "cost_basis"])
    for p in pos:
        if p["precio"] is None:
            continue
        w.writerow([p["ticker"], "equity", f"{p['cantidad']:.6f}", "LONG", f"{p['precio']:.4f}", "USD", f"{p['precio_prom']:.4f}"])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8")


def _retorno(closes: list[float], n: int):
    return round((closes[-1] / closes[-1 - n] - 1) * 100, 2) if len(closes) > n and closes[-1 - n] else None


def _sma(closes: list[float], n: int):
    return round(sum(closes[-n:]) / n, 4) if len(closes) >= n else None


@app.get("/api/ticker/{ticker}")
def api_ticker(ticker: str):
    t = reglas.normalizar(ticker)
    if not reglas.TICKER.match(t):
        return JSONResponse({"error": f"«{ticker}» no parece un ticker de EE. UU."}, status_code=400)
    with abrir() as conn:
        err = precios.asegurar(conn, t)
        if err == "no existe":
            return JSONResponse({"error": f"Yahoo no conoce {t}."}, status_code=404)
        m = precios.meta(conn, t)
        if m is None:
            return JSONResponse({"error": f"No tengo datos de {t}: {(err or 'Yahoo no contestó').rstrip('.')}."}, status_code=503)
        u = reglas.universo(conn, t)
        vs = precios.velas(conn, t, 260)
        closes = [v["close"] for v in vs]
        ult252 = vs[-252:]
        claude = db.cartera_id(conn, "claude")
        total, _ = cartera.valor(conn, claude)
        p = conn.execute("SELECT cantidad FROM posiciones WHERE cartera_id = ? AND ticker = ?", (claude, t)).fetchone()
    peso = (p["cantidad"] * closes[-1] / total * 100) if p and closes and total else 0.0
    highs = [v["high"] for v in ult252 if v["high"] is not None]
    lows = [v["low"] for v in ult252 if v["low"] is not None]
    return {"ticker": t, "nombre": m["nombre"], "tipo": u["tipo"] or m["tipo"], "bolsa": m["bolsa_nombre"] or m["bolsa"],
            "precio": _r(closes[-1], 4) if closes else None, "fecha_precio": vs[-1]["fecha"] if vs else None,
            "en_universo": u["en_universo"], "motivos": u["motivos"],
            "volumen_usd_mediana": _r(u["volumen_usd_mediana"], 0),
            "retornos": {"1m": _retorno(closes, 21), "3m": _retorno(closes, 63), "6m": _retorno(closes, 126),
                         "12m": _retorno(closes, 252)},
            "sma50": _sma(closes, 50), "sma200": _sma(closes, 200),
            "max_52s": _r(max(highs), 4) if highs else None, "min_52s": _r(min(lows), 4) if lows else None,
            "peso_actual_pct": round(peso, 2), "peso_maximo_pct": reglas.peso_maximo_pct(u["tipo"]),
            "datos_viejos": err}


@app.get("/api/decisiones")
def api_decisiones(n: int = 20, ticker: str | None = None):
    with abrir() as conn:
        return {"decisiones": ultimas_decisiones(conn, max(1, min(n, 200)), reglas.normalizar(ticker) if ticker else None)}


def _decidir(cuerpo, simular: bool) -> tuple[int, dict]:
    with abrir() as conn:
        tiene_ordenes = isinstance(cuerpo, dict) and bool(cuerpo.get("ordenes"))
        if tiene_ordenes and db.leer(conn, "pausada", False):
            return 409, {"error": "La mesa está en pausa: no entra ninguna decisión con órdenes. Podés anotar una sin órdenes."}
        errores, plan = reglas.validar(conn, cuerpo, refrescar=True)
        if errores:
            return 422, {"errores": errores}
        if simular:
            vara_nueva = bool(plan["ordenes"]) and conn.execute(
                "SELECT 1 FROM ordenes WHERE cartera_id = ?", (db.cartera_id(conn, "vara"),)).fetchone() is None
            return 200, {"simulada": True, "valida": True, "ordenes": [_plan_json(o) for o in plan["ordenes"]],
                         "revisiones": plan["revisiones"], "efectivo_libre": plan["efectivo_libre"],
                         "valor_cartera": plan["valor_cartera"], "compra_la_vara": vara_nueva,
                         "mensaje": "Es válida. No se guardó nada: mandala sin simular=1 para anotarla."}
        with db.transaccion(conn):
            if tiene_ordenes and db.leer(conn, "pausada", False):
                return 409, {"error": "La mesa entró en pausa recién: no se guardó."}
            errores, plan = reglas.validar(conn, cuerpo, refrescar=False)
            if errores:
                raise _Rechazo(errores)
            did, ords = cartera.guardar_decision(conn, plan)
        msg = (f"Anotada la decisión {did} con {len(ords)} {'orden' if len(ords) == 1 else 'órdenes'}: "
               f"{'se ejecuta' if len(ords) == 1 else 'se ejecutan'} en la próxima apertura." if ords
               else f"Anotada la decisión {did}, sin órdenes.")
        return 201, {"id": did, "ordenes": ords, "mensaje": msg}


class _Rechazo(Exception):
    def __init__(self, errores):
        self.errores = errores


def _plan_json(o: dict) -> dict:
    return {k: o.get(k) for k in ("ticker", "lado", "monto_usd", "cantidad", "todo", "tipo", "estimado")}


@app.post("/api/decisiones")
async def api_decidir(request: Request):
    try:
        cuerpo = await request.json()
    except ValueError:
        return JSONResponse({"errores": ["El cuerpo no es JSON."]}, status_code=422)
    simular = request.query_params.get("simular", "").lower() in ("1", "true", "si", "sí")
    try:
        status, salida = await run_in_threadpool(_decidir, cuerpo, simular)
    except _Rechazo as r:
        status, salida = 422, {"errores": r.errores}
    return JSONResponse(salida, status_code=status)
