"""The apps of the house, from the bot: each call sends the person's sentence to the app that
understands it (the same endpoints Siri uses) and gives back what to answer, which app it was and,
when something was written, how to take it back ("borrar"): [method, url] pairs.

They answer this container directly on the Mac (host.docker.internal, no proxy headers), as they
answer n8n and Hoy."""
import os
import re
from datetime import datetime
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

HOST = os.environ.get("APPS_HOST", "http://host.docker.internal")
URLS = {
    "finanzas": os.environ.get("FINANZAS_URL", f"{HOST}:3013"),
    "recordatorios": os.environ.get("RECORDATORIOS_URL", f"{HOST}:3014"),
    "pulso": os.environ.get("PULSO_URL", f"{HOST}:3015"),
    "lista": os.environ.get("LISTA_URL", f"{HOST}:3016"),
    "papeles": os.environ.get("PAPELES_URL", f"{HOST}:3017"),
    "hoy": os.environ.get("HOY_URL", f"{HOST}:3018"),
    "mesa": os.environ.get("MESA_URL", f"{HOST}:3020"),
    "vigia": os.environ.get("VIGIA_URL", f"{HOST}:3021"),
    "pedidos": os.environ.get("PEDIDOS_URL", f"{HOST}:3022"),
}
NAMES = {"finanzas": "Finanzas", "recordatorios": "Recordatorios", "pulso": "Pulso", "lista": "Lista",
         "papeles": "Papeles", "hoy": "Hoy", "mesa": "Mesa", "vigia": "Vigía",
         "pedidos": "Pedidos"}
ICONS = {"finanzas": "💸", "recordatorios": "⏰", "pulso": "🖥️", "lista": "🛒", "papeles": "📄", "hoy": "☀️", "mesa": "📈",
         "vigia": "🔭", "pedidos": "🛵"}


@dataclass
class Answer:
    text: str
    app: str | None = None
    ok: bool = True
    undo: list = field(default_factory=list)  # [[method, url], ...]
    # It changed something "borrar" can't take back (ticked the list, saved a paper, the AI wrote):
    # "borrar" right after it says so instead of undoing an older, unrelated action.
    wrote: bool = False
    # The app didn't recognize it (Finanzas: not a bill it knows): the message can go where it would
    # have gone otherwise.
    unknown: bool = False


def _down(app: str) -> Answer:
    return Answer(f"{NAMES[app]} no respondió. Probá en un rato.", app, ok=False)


def _post_text(app: str, path: str, text: str, timeout: float = 90, who: str = "") -> httpx.Response | None:
    """who: the person's name, so the app knows whose it is (Finanzas: X-Quien)."""
    headers = {"Content-Type": "text/plain; charset=utf-8"}
    if who:
        headers["X-Quien"] = quote(who)
    try:
        return httpx.post(URLS[app] + path, content=text.encode(), timeout=timeout, headers=headers)
    except httpx.HTTPError:
        return None


def _get(app: str, path: str, timeout: float = 20, **params) -> httpx.Response | None:
    try:
        return httpx.get(URLS[app] + path, params=params, timeout=timeout)
    except httpx.HTTPError:
        return None


def _ids(r: httpx.Response, header: str) -> list[str]:
    return [i for i in r.headers.get(header, "").split(",") if i.strip().isdigit()]


# --- Lista ----------------------------------------------------------------------------------------

def list_add(text: str) -> Answer:
    r = _post_text("lista", "/api/agregar", text)
    if r is None or r.status_code >= 500:
        return _down("lista")
    undo = [["POST", f"{URLS['lista']}/i/{i}/borrar"] for i in _ids(r, "X-Ids")]
    return Answer(r.text, "lista", ok=bool(undo), undo=undo)


def list_show() -> Answer:
    r = _get("lista", "/api/lista")
    return Answer(r.text, "lista") if r is not None and r.status_code == 200 else _down("lista")


def list_tick(text: str) -> Answer:
    try:
        r = httpx.post(URLS["lista"] + "/ia/comprado", json={"texto": text}, timeout=20)
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return _down("lista")
    done = data.get("marcado") or [] if isinstance(data, dict) else []
    if not done:
        return Answer("No encontré eso en la lista.", "lista", ok=False)
    return Answer(f"Al carrito: {_join(done)}.", "lista", wrote=True)


def list_finish() -> Answer:
    pending = _get("lista", "/ia/lista")
    try:
        state = pending.json() if pending is not None and pending.status_code == 200 else None
    except ValueError:  # a 502 page or a traceback instead of JSON
        state = None
    if not isinstance(state, dict):
        return _down("lista")
    cart = state.get("en_el_carrito") or []
    if not cart:
        return Answer("No hay nada en el carrito: tildá lo que compraste (o escribime «compré la leche»).", "lista",
                      ok=False)
    try:
        done = httpx.post(URLS["lista"] + "/terminar", timeout=20)
    except httpx.HTTPError:
        return _down("lista")
    if done.status_code >= 400:
        return _down("lista")
    left = state.get("cantidad") or 0
    tail = f" Quedan {left} en la lista." if left else " La lista quedó vacía."
    return Answer(f"Listo, guardé la compra ({len(cart)} cosa{'s' if len(cart) > 1 else ''}).{tail}", "lista",
                  wrote=True)


def _join(names: list[str]) -> str:
    names = [n[:1].lower() + n[1:] for n in names]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " y " + names[-1]


# --- Finanzas -------------------------------------------------------------------------------------

def expense(text: str, who: str = "") -> Answer:
    """who: the name of whoever wrote it, so the movement is theirs."""
    r = _post_text("finanzas", "/api/gasto", text, who=who)
    if r is None or r.status_code >= 500:
        return _down("finanzas")
    if r.status_code != 200:
        return Answer(r.text, "finanzas", ok=False)
    return Answer(r.text, "finanzas", undo=[["POST", f"{URLS['finanzas']}/movimientos/{i}/borrar"]
                                            for i in _ids(r, "X-Id")])


def balances() -> Answer:
    r = _get("finanzas", "/api/estado")
    if r is None or r.status_code != 200:
        return _down("finanzas")
    s = (r.json() or {}).get("saldos")
    if not s:
        return Answer("Todavía no cargaste saldos: entrá a Finanzas → Saldos.", "finanzas", ok=False)
    lines = [f"*Hoy tenés {s['total']}*" + (f" ({s['total_usd']})" if s.get("total_usd") else "")]
    lines += [f"• {c['cuenta']}: {c['saldo']}" for c in s["cuentas"]]
    return Answer("\n".join(lines), "finanzas")


def bills() -> Answer:
    """What's left to pay this month (Finanzas' "Pagos del mes"), in its own sentence."""
    r = _get("finanzas", "/api/pagos")
    return Answer(r.text, "finanzas") if r is not None and r.status_code == 200 else _down("finanzas")


def bill_paid(text: str, who: str = "") -> Answer:
    """'pagué la luz': this month's bill marked paid today. Undone by removing that mark; an "already
    paid" answer has no X-Id, so there's nothing to take back."""
    r = _post_text("finanzas", "/api/pagos/pagar", text, timeout=20, who=who)
    if r is None or r.status_code >= 500:
        return _down("finanzas")
    if r.status_code != 200:
        return Answer(r.text, "finanzas", ok=False, unknown=r.status_code == 422)
    bill, month = r.headers.get("X-Id", "").strip(), r.headers.get("X-Mes", "").strip()
    undo = ([["POST", f"{URLS['finanzas']}/api/pagos/{bill}/pendiente?m={month}"]]
            if bill.isdigit() and re.fullmatch(r"\d{4}-\d{2}", month) else [])
    return Answer(r.text, "finanzas", undo=undo)


def statement(name: str, data: bytes, mime: str) -> Answer:
    """A card or bank statement (PDF, Excel) to Finanzas, like its Shortcut."""
    try:
        r = httpx.post(URLS["finanzas"] + "/api/subir", files={"files": (name, data, mime)}, timeout=180)
    except httpx.HTTPError:
        return _down("finanzas")
    return Answer(r.text, "finanzas", ok=r.status_code == 200, wrote=r.status_code == 200)


# --- Recordatorios --------------------------------------------------------------------------------

def reminder(text: str) -> Answer:
    r = _post_text("recordatorios", "/api/rapido", text)
    if r is None or r.status_code >= 500:
        return _down("recordatorios")
    if r.status_code != 200:
        return Answer(r.text, "recordatorios", ok=False)
    return Answer(r.text, "recordatorios", undo=[["POST", f"{URLS['recordatorios']}/r/{i}/borrar"]
                                                 for i in _ids(r, "X-Id")])


def agenda(days: int) -> Answer:
    r = _get("recordatorios", "/api/proximos", dias=days)
    return Answer(r.text, "recordatorios") if r is not None and r.status_code == 200 else _down("recordatorios")


# --- Hoy, Pulso, Papeles --------------------------------------------------------------------------

def morning(mesa: bool = True) -> Answer:
    """mesa: whether to say how the Mesa goes (it's Nahuel's: Hoy leaves it out with mesa=0)."""
    r = _get("hoy", "/api/frase", timeout=30, **({} if mesa else {"mesa": 0}))
    return Answer(r.text, "hoy") if r is not None and r.status_code == 200 else _down("hoy")


def server() -> Answer:
    r = _get("pulso", "/api/frase")
    return Answer(r.text, "pulso") if r is not None and r.status_code == 200 else _down("pulso")


# --- Mesa (the long-term paper portfolio Claude decides; Nahuel's) ---------------------------------------------------------------

def mesa() -> Answer:
    r = _get("mesa", "/api/frase")
    return Answer(r.text, "mesa") if r is not None and r.status_code == 200 else _down("mesa")


def _short(text, n: int = 160) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _when(d: dict) -> str:
    try:
        if isinstance(d.get("ts"), (int, float)):
            return datetime.fromtimestamp(d["ts"] / 1000).strftime("%d/%m")
        return datetime.fromisoformat(str(d["fecha"])[:10]).strftime("%d/%m")
    except (KeyError, ValueError, TypeError, OSError):
        return "?"


def mesa_por_que(ticker: str = "") -> Answer:
    """The latest orders Claude put in (date, bought/sold, ticker, amount or price, and the thesis or
    the reason), from /api/decisiones; with a ticker ("MSFT"), only that one."""
    ticker = ticker.upper()
    r = _get("mesa", "/api/decisiones", n=20, **({"ticker": ticker} if ticker else {}))
    if r is None or r.status_code >= 500:
        return _down("mesa")
    try:
        data = r.json() if r.status_code == 200 else None
        todas = data.get("decisiones") if isinstance(data, dict) else data
        if not isinstance(todas, list):
            return _down("mesa")
        lines = []
        for d in todas:
            for o in d.get("ordenes") or []:
                if ticker and str(o.get("ticker") or "").upper() != ticker:
                    continue
                if str(o.get("estado") or "").startswith(("rechaz", "cancel")):
                    continue
                verbo = "vendió" if str(o.get("lado") or "").lower().startswith("v") else "compró"
                monto = o.get("monto_usd") or o.get("monto")
                cuanto = (f" por {_usd(monto)}" if isinstance(monto, (int, float)) else "")
                if isinstance(o.get("precio"), (int, float)):
                    cuanto += f" a {_usd(o['precio'])}"
                why = o.get("tesis") or o.get("motivo") or o.get("motivo_venta_anticipada") or d.get("resumen") or ""
                lines.append(f"• {_when(d)} {verbo} {o.get('ticker') or '?'}{cuanto}"
                             + (f"\n  _{_short(why)}_" if why else ""))
    except (AttributeError, TypeError, ValueError):
        return _down("mesa")
    cuando = f" de {ticker}" if ticker else ""
    if not lines:
        return Answer(f"La mesa no compró ni vendió nada{cuando} en sus últimas decisiones.", "mesa")
    return Answer("\n".join([f"*Lo último que hizo la mesa{cuando}*"] + lines[:5]), "mesa")


def _usd(n: float) -> str:
    return "USD " + f"{n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".").removesuffix(",00")


def mesa_control(accion: str, who: str = "") -> Answer:
    """accion: 'pausar' or 'reanudar', through the web, which asks the motor. The web logs who asked."""
    headers = {"Accept": "application/json"}
    if who:
        headers["X-Quien"] = quote(who)
    try:
        r = httpx.post(f"{URLS['mesa']}/accion/mesa/{accion}", json={}, headers=headers, timeout=20)
        data = r.json()
    except httpx.HTTPError:
        return _down("mesa")
    except ValueError:
        return Answer("La mesa contestó algo raro.", "mesa", ok=False)
    if r.status_code == 200:
        return Answer(data.get("mensaje") or ("Mesa pausada." if accion == "pausar" else "Mesa reanudada."), "mesa",
                      wrote=True)
    return Answer(data.get("error") or "No se pudo.", "mesa", ok=False)


# --- Vigía (pages watched, links kept) ----------------------------------------------------------

LINK = re.compile(r"(?:https?://|www\.)[^\s<>\"'«»“”]+", re.I)


def _chat(chat) -> str | None:
    """Where Vigía sends the alert or the summary later: this Telegram chat (the page's try box has
    none: there it goes to ntfy)."""
    return str(chat) if chat not in (None, "", "web") else None


def _vigia(path: str, body: dict, timeout: float = 30) -> httpx.Response | None:
    try:
        return httpx.post(URLS["vigia"] + path, json=body, timeout=timeout)
    except httpx.HTTPError:
        return None


def save(text: str, who: str = "", chat=None) -> Answer:
    """A link to Guardado (it reads it, summarizes it and sends the summary to this chat). Without a
    link ("/guardar una idea…"), the text is kept as a note. Undone by deleting the item; the same
    link sent again is the existing item, with nothing to take back."""
    m = LINK.search(text or "")
    body = {"url": m.group().rstrip(".,;:!?)")} if m else {"texto": text}
    r = _vigia("/api/guardar", {**body, "quien": who, "chat": _chat(chat)})
    if r is None or r.status_code >= 500:
        return _down("vigia")
    if r.status_code != 200:
        return Answer(r.text, "vigia", ok=False)
    return Answer(r.text, "vigia", undo=[["POST", f"{URLS['vigia']}/g/{i}/borrar"] for i in _ids(r, "X-Ids")])


def save_audio(text: str, title: str, who: str = "", chat=None) -> Answer:
    """A long voice note's transcript to Guardado, as an audio: the summary comes back to the chat."""
    r = _vigia("/api/guardar", {"texto": text, "titulo": title, "tipo": "audio", "quien": who, "chat": _chat(chat)})
    if r is None or r.status_code >= 500:
        return _down("vigia")
    if r.status_code != 200:
        return Answer(r.text, "vigia", ok=False)
    return Answer(r.text, "vigia", undo=[["POST", f"{URLS['vigia']}/g/{i}/borrar"] for i in _ids(r, "X-Ids")])


def watch(text: str, who: str = "", chat=None) -> Answer:
    """'seguí el precio de <link>': Vigía reads the page right away (up to 20 s, plus redirects) and
    says what it read; its alerts come back to this chat."""
    r = _vigia("/api/vigilar", {"texto": text, "quien": who, "chat": _chat(chat)}, timeout=90)
    if r is None or r.status_code >= 500:
        return _down("vigia")
    if r.status_code != 200:
        return Answer(r.text, "vigia", ok=False)
    return Answer(r.text, "vigia", undo=[["POST", f"{URLS['vigia']}/v/{i}/borrar"] for i in _ids(r, "X-Ids")])


def watches() -> Answer:
    r = _get("vigia", "/api/vigilancias")
    return Answer(r.text, "vigia") if r is not None and r.status_code == 200 else _down("vigia")


# --- Pedidos (the house's delivery orders) -----------------------------------------------------------

def order_make(proveedor: str = "", texto: str = "", who: str = "") -> Answer:
    """'armá el pedido' / 'pedí la verdura' / 'pedí 2 kg de papas a la verdulería': Pedidos builds the
    drafts (it may refresh a shop's catalog first) and says where each one is; nothing is sent. Undone by
    cancelling the drafts."""
    body = {k: v for k, v in (("proveedor", proveedor), ("texto", texto)) if v}
    try:
        r = httpx.post(URLS["pedidos"] + "/api/armar", json={**body, "quien": who}, timeout=60)
    except httpx.HTTPError:
        return _down("pedidos")
    if r.status_code >= 500:
        return _down("pedidos")
    if r.status_code != 200:
        return Answer(r.text, "pedidos", ok=False)
    return Answer(r.text, "pedidos", undo=[["POST", f"{URLS['pedidos']}/p/{i}/cancelar"] for i in _ids(r, "X-Ids")])


def orders() -> Answer:
    r = _get("pedidos", "/api/pedidos")
    return Answer(r.text, "pedidos") if r is not None and r.status_code == 200 else _down("pedidos")


def price(q: str) -> Answer:
    r = _get("pedidos", "/api/precio", q=q)
    if r is not None and r.status_code in (200, 404):
        return Answer(r.text, "pedidos")
    return _down("pedidos")


def paper(name: str, data: bytes, mime: str) -> Answer:
    """A photo or PDF to Papeles (OCR, buscable, avisa si vence), like its Shortcut."""
    try:
        r = httpx.post(URLS["papeles"] + "/api/subir", files={"files": (name, data, mime)}, timeout=300)
    except httpx.HTTPError:
        return _down("papeles")
    return Answer(r.text, "papeles", ok=r.status_code == 200, wrote=r.status_code == 200)


def take_back(undo: list) -> bool:
    """Runs what an answer said would undo it. True if every step went through."""
    ok = True
    for method, url in undo:
        try:
            r = httpx.request(method, url, timeout=20)
            ok = ok and r.status_code < 400
        except httpx.HTTPError:
            ok = False
    return ok
