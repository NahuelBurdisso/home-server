"""Mensajes: the house's Telegram bot. Write or send a voice note to it, "comprar manteca" lands on
the Lista, "gasté 8500 en la verdulería" in Finanzas, "recordame el jueves…" in Recordatorios, a
photo of a warranty in Papeles; anything else goes to the local AI with the apps as tools.

The bot asks Telegram for messages itself (long polling): nothing is opened to the internet. This
page, tailnet only (the owner check below), holds the bot's token, invites people and says who
may write."""
import json
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import handle, house, intents, people, telegram
from .db import connect, get_state, set_state

HERE = Path(__file__).parent
ALLOWED = {s.strip().lower() for s in os.environ.get("MENSAJES_ALLOWED_LOGINS", "").split(",") if s.strip()}
PUBLIC_URL = os.environ.get("MENSAJES_URL", "https://mac-server.tailnet-ejemplo.ts.net:3019")
STALE = 180  # seconds without a good poll before /health says so (Kuma → ntfy)
BUSY_MAX = 20 * 60  # the longest one message may keep the poller busy before /health says so


# --- the poller: asks Telegram for messages and answers them, one at a time, in order ------------

def poller():
    conn = connect()
    bot, bot_token, offset = None, None, None
    while True:
        tok = telegram.token()
        if not tok:
            set_state(conn, "status", "sin_token")
            time.sleep(10)
            continue
        try:
            if bot is None or tok != bot_token:
                bot, bot_token = telegram.me(tok), tok
                set_state(conn, "bot", json.dumps(bot))
                telegram.commands()
                offset = int(get_state(conn, "offset") or 0) or None
            ups = telegram.updates(offset)
            set_state(conn, "status", "ok")
            set_state(conn, "last_poll", datetime.now().isoformat(timespec="seconds"))
            set_state(conn, "error", None)
            for u in ups:
                # A slow message (the AI, a long audio, Papeles' OCR) is the poller working, not stuck.
                set_state(conn, "last_poll", datetime.now().isoformat(timespec="seconds"))
                set_state(conn, "busy_since", datetime.now().isoformat(timespec="seconds"))
                try:
                    handle.update(conn, u, bot)
                except Exception as e:  # noqa: BLE001 - one bad message must not stop the others
                    print(f"handle: {type(e).__name__}: {e}", flush=True)
                finally:
                    set_state(conn, "busy_since", None)
                offset = u["update_id"] + 1
                set_state(conn, "offset", str(offset))
        except Exception as e:  # noqa: BLE001 - the poller must outlive any error
            print(f"telegram: {type(e).__name__}: {e}", flush=True)
            set_state(conn, "status", "error")
            set_state(conn, "error", str(e) if isinstance(e, telegram.TelegramError) else f"{type(e).__name__}: {e}")
            if "unauthorized" in str(e).lower() or "not found" in str(e).lower():
                bot = None  # a revoked or wrong token: wait for a new one
                time.sleep(60)
            else:
                time.sleep(10)  # Telegram or the internet down: Pulso and Kuma tell about that


@asynccontextmanager
async def lifespan(app):
    if os.environ.get("MENSAJES_WORKERS", "1") == "1":
        threading.Thread(target=poller, daemon=True, name="poller").start()
    yield


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals.update(PUBLIC_URL=PUBLIC_URL, ICONS=house.ICONS, KINDS=intents.KINDS, who=people.who)

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
OWN_HOSTS = {"mac-server.tailnet-ejemplo.ts.net", "localhost", "127.0.0.1"}


def same_site(request: Request) -> bool:
    """A form or script on another web site can make the browser POST here (CSRF): browsers say so
    in Sec-Fetch-Site and Origin. n8n and curl send neither, and are let through."""
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
    MENSAJES_ALLOWED_LOGINS get in; direct requests on this Mac (no proxy) are allowed too."""
    if request.method not in SAFE_METHODS and not same_site(request):
        return JSONResponse({"error": "Pedido desde otro sitio: rechazado."}, status_code=403)
    if request.url.path != "/health":
        login = request.headers.get("tailscale-user-login", "").lower()
        proxied = "x-forwarded-for" in request.headers
        if (login and ALLOWED and login not in ALLOWED) or (proxied and not login):
            return JSONResponse({"error": "Solo para el dueño del tailnet."}, status_code=403)
    return await call_next(request)


def _bot(conn) -> dict:
    try:
        return json.loads(get_state(conn, "bot") or "{}")
    except ValueError:
        return {}


def _polling_ok(conn) -> bool:
    last = get_state(conn, "last_poll")
    if bool(last) and (datetime.now() - datetime.fromisoformat(last)).total_seconds() < STALE:
        return True
    # Answering one slow message: the AI can take 5 model calls of up to 3 min each, Papeles' OCR 5 min.
    busy = get_state(conn, "busy_since")
    return bool(busy) and (datetime.now() - datetime.fromisoformat(busy)).total_seconds() < BUSY_MAX


@app.get("/health")
def health():
    """503 when there is a token but no good poll in 3 minutes (Telegram unreachable, token revoked)."""
    conn = connect()
    if not telegram.token():
        return {"ok": True, "telegram": "sin token"}
    if not _polling_ok(conn):
        return JSONResponse({"ok": False, "telegram": get_state(conn, "error") or "sin respuesta"}, status_code=503)
    return {"ok": True, "telegram": "ok"}


# --- pages ----------------------------------------------------------------------------------------

def render(request: Request, template: str, **ctx):
    return templates.TemplateResponse(request, template, {"page": template.split(".")[0], **ctx})


def _recent(conn, limit: int = 30, before: int | None = None):
    q = ("SELECT m.*, p.name AS who_name, p.username AS who_user FROM messages m LEFT JOIN people p ON p.id = m.person_id"
         + (" WHERE m.id < ?" if before else "") + " ORDER BY m.id DESC LIMIT ?")
    return conn.execute(q, (before, limit) if before else (limit,)).fetchall()


def _invite_link(conn, bot: dict) -> tuple[str, str] | None:
    """(link, until HH:MM) while an invitation is open."""
    inv = people.current_invite(conn)
    if not inv or not bot.get("username"):
        return None
    return f"https://t.me/{bot['username']}?start={inv[0]}", inv[1][11:16]


@app.get("/")
def home(request: Request, error: str = ""):
    conn = connect()
    bot = _bot(conn)
    has_token = bool(telegram.token())
    waiting = conn.execute("SELECT COUNT(*) FROM people WHERE allowed = 0").fetchone()[0]
    allowed = conn.execute("SELECT COUNT(*) FROM people WHERE allowed = 1").fetchone()[0]
    return render(request, "inicio.html", has_token=has_token, bot=bot, ok=has_token and _polling_ok(conn),
                  status_error=get_state(conn, "error"), error=error, waiting=waiting, allowed=allowed,
                  invite=_invite_link(conn, bot), recent=_recent(conn, 12))


@app.post("/token")
def save_token(token: str = Form("")):
    """The token from @BotFather, checked with Telegram before it's kept in secrets/telegram-token."""
    token = token.strip()
    if not telegram.TOKEN_RE.match(token):
        return RedirectResponse("/?error=" + quote("Eso no parece un token: es como 123456789:AAH…"), status_code=303)
    try:
        bot = telegram.me(token)
    except telegram.TelegramError as e:
        return RedirectResponse("/?error=" + quote(f"Telegram no lo aceptó: {e}"), status_code=303)
    telegram.save_token(token)
    conn = connect()
    old = json.loads(get_state(conn, "bot") or "null") or {}
    if old.get("id") != bot.get("id"):
        # Another bot: its message ids start over at 1, and the old bot's "chat:id" keys would make
        # its first messages look already seen.
        conn.execute("UPDATE messages SET tg_key = NULL WHERE tg_key IS NOT NULL")
        conn.commit()
    set_state(conn, "bot", json.dumps(bot))
    set_state(conn, "offset", None)
    return RedirectResponse("/", status_code=303)


@app.post("/invitar")
def make_invite(volver: str = Form("/")):
    people.invite(connect())
    return RedirectResponse("/personas" if volver == "/personas" else "/", status_code=303)


@app.get("/personas")
def people_page(request: Request):
    conn = connect()
    rows = conn.execute("SELECT p.*, (SELECT COUNT(*) FROM messages m WHERE m.person_id = p.id AND m.incoming = 1) AS n"
                        " FROM people p ORDER BY allowed, COALESCE(last_at, created_at) DESC").fetchall()
    bot = _bot(conn)
    return render(request, "personas.html", waiting=[r for r in rows if not r["allowed"]],
                  allowed=[r for r in rows if r["allowed"]], invite=_invite_link(conn, bot), has_bot=bool(bot))


@app.post("/personas/{pid}/{what}")
def people_change(pid: int, what: str, nombre: str = Form("")):
    conn = connect()
    if what == "permitir":
        people.allow(conn, pid)
    elif what == "nombre":
        conn.execute("UPDATE people SET name = ? WHERE id = ?", (nombre.strip(), pid))
    elif sql := {
        "bloquear": "UPDATE people SET allowed = 0, avisos = 0, notified = 1 WHERE id = ?",
        "avisos": "UPDATE people SET avisos = 1 - avisos WHERE id = ?",
        "borrar": "DELETE FROM people WHERE id = ?",
    }.get(what):
        conn.execute(sql, (pid,))
    conn.commit()
    return RedirectResponse("/personas", status_code=303)


@app.get("/historial")
def history(request: Request, antes: int | None = None):
    rows = _recent(connect(), 60, antes)
    return render(request, "historial.html", rows=rows, more=rows[-1]["id"] if len(rows) == 60 else None)


@app.post("/probar")
def try_it(texto: str = Form("")):
    """The box on the home page: what the bot would do with a message, done for real (it writes
    to the apps like a Telegram message would; 'borrar' there takes it back)."""
    conn = connect()
    texto = texto.strip()
    if texto:
        row_id = conn.execute("INSERT INTO messages (chat, person_id, incoming, text) VALUES ('web', 0, 1, ?)",
                              (texto,)).lastrowid
        it = intents.read(texto)
        conn.execute("UPDATE messages SET intent = ? WHERE id = ?", (it.kind, row_id))
        conn.commit()
        # The page is Nahuel's (the tailnet policy): the person the bot would see for him.
        me = {"id": 0, "name": "", "avisos": 1}
        a = handle.run(conn, it, "web", me, row_id) or house.Answer("(nada que responder)")
        conn.execute("INSERT INTO messages (chat, person_id, incoming, text, app, undo) VALUES ('web', 0, 0, ?, ?, ?)",
                     (handle._with_icon(a), a.app, handle.undo_of(a)))
        conn.commit()
    return RedirectResponse("/#probar", status_code=303)


@app.get("/ayuda")
def help_page(request: Request):
    return render(request, "ayuda.html", help=handle.HELP, bot=_bot(connect()))


# --- for n8n and the other apps --------------------------------------------------------------------

@app.post("/api/enviar")
async def api_send(request: Request):
    """Sends a Telegram message from the bot. JSON {"texto": "...", "a": "Sofi" (optional: a name or
    @username on Personas)}, or the text itself. Without "a": to everyone with avisos on.
    Only to authorized people. {"texto", "chat": "<id>"} answers a chat instead (Vigía: the alert
    goes where it was asked for, a group too), but only a chat where an authorized person has
    written to the bot."""
    raw = (await request.body()).decode("utf-8", "replace").strip()
    texto, to, chat = raw, "", ""
    if raw.startswith("{"):
        try:
            body = json.loads(raw)
            texto, to = str(body.get("texto", "")).strip(), str(body.get("a", "")).strip().lstrip("@").lower()
            chat = str(body.get("chat") or "").strip()
        except (ValueError, AttributeError):
            return PlainTextResponse("JSON inválido.", status_code=422)
    if not texto:
        return PlainTextResponse("Falta el texto.", status_code=422)
    conn = connect()
    if chat:
        known = conn.execute("SELECT 1 FROM messages m JOIN people p ON p.id = m.person_id WHERE m.chat = ?"
                             " AND m.incoming = 1 AND p.allowed = 1 AND m.chat != 'web' LIMIT 1", (chat,)).fetchone()
        if not known or not re.fullmatch(r"-?\d+", chat):
            return PlainTextResponse("Ese chat no habló con el bot (o no está autorizado).", status_code=422)
        handle.send(conn, int(chat), texto)
        return PlainTextResponse("Enviado a 1.")
    if to:
        targets = [r for r in conn.execute("SELECT * FROM people WHERE allowed = 1").fetchall()
                   if to in (r["name"].lower(), r["username"].lower(), str(r["tg_id"]))]
    else:
        targets = people.recipients(conn)
    if not targets:
        return PlainTextResponse("No hay a quién mandarlo (autorizá a alguien y prendé sus avisos en Personas).",
                                 status_code=422)
    for r in targets:
        handle.send(conn, r["tg_id"], texto, r["id"])
    return PlainTextResponse(f"Enviado a {len(targets)}.")


@app.get("/api/estado")
def api_status():
    """For Hoy, Pulso, n8n and the health check: whether the bot is connected, and how much it worked today."""
    conn = connect()
    bot = _bot(conn)
    today = conn.execute("SELECT COUNT(*) FROM messages WHERE incoming = 1 AND date(at) = date('now', 'localtime')"
                         " AND chat != 'web'").fetchone()[0]
    return {"telegram": "sin token" if not telegram.token() else "ok" if _polling_ok(conn) else "error",
            "bot": f"@{bot['username']}" if bot.get("username") else None, "mensajes_hoy": today,
            "esperando_autorizacion": conn.execute("SELECT COUNT(*) FROM people WHERE allowed = 0").fetchone()[0],
            "error": get_state(conn, "error")}
