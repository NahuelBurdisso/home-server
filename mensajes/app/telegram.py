"""Talks to Telegram's Bot API. The bot asks Telegram for new messages (long polling, main.py's
poller): nothing is opened to the internet and no webhook is needed. The token comes from
@BotFather and lives in secrets/telegram-token (the page can save it there too)."""
import html
import os
import re
import threading

import httpx

API = os.environ.get("TELEGRAM_API", "https://api.telegram.org")
TOKEN_FILE = os.environ.get("TELEGRAM_TOKEN_FILE", "/run/secrets/telegram-token")
TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


class TelegramError(Exception):
    pass


def token() -> str:
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def save_token(value: str):
    """Written in place: the file is a single-file bind mount, a rename wouldn't reach the Mac."""
    with open(TOKEN_FILE, "w") as f:
        f.write(value.strip() + "\n")


def call(method: str, params: dict | None = None, *, limit: float = 30, tok: str | None = None):
    """A Bot API method. params are Telegram's own (getUpdates has its own 'timeout' among them);
    limit is how long to wait for the answer."""
    tok = tok or token()
    if not tok:
        raise TelegramError("Falta el token del bot.")
    try:
        r = httpx.post(f"{API}/bot{tok}/{method}", json=params or {}, timeout=limit)
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise TelegramError(f"Telegram no respondió ({type(e).__name__}).") from e
    if not data.get("ok"):
        raise TelegramError(data.get("description") or f"Error {r.status_code}")
    return data["result"]


def me(tok: str | None = None) -> dict:
    """{'id', 'username', 'first_name', 'can_read_all_group_messages', ...}"""
    return call("getMe", limit=15, tok=tok)


def updates(offset: int | None, wait: int = 50) -> list[dict]:
    params = {"timeout": wait, "allowed_updates": ["message"]}
    if offset is not None:
        params["offset"] = offset
    return call("getUpdates", params, limit=wait + 15)


def to_html(text: str) -> str:
    """The answers use *bold* and _italic_ (as the apps write them); Telegram wants HTML.
    An unpaired * (a merchant like 'Pago Dlo*pedidosya') stays as it is."""
    t = html.escape(text, quote=False)
    t = re.sub(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])", r"<b>\1</b>", t)
    return re.sub(r"(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])", r"<i>\1</i>", t)


def send(chat: int, text: str, reply_to: int | None = None) -> int | None:
    """The id of the message sent. Falls back to plain text if Telegram refuses the HTML."""
    text = text[:4000]
    base = {"chat_id": chat, "link_preview_options": {"is_disabled": True}}
    if reply_to:
        base["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    try:
        return call("sendMessage", {**base, "text": to_html(text), "parse_mode": "HTML"}).get("message_id")
    except TelegramError as e:
        if "parse" not in str(e).lower() and "entit" not in str(e).lower():
            raise
    return call("sendMessage", {**base, "text": text}).get("message_id")


class typing:
    """'escribiendo…' while the bot works (Telegram shows it ~5 s per call, so it's repeated)."""

    def __init__(self, chat: int):
        self.chat, self.stop = chat, threading.Event()

    def _run(self):
        while not self.stop.is_set():
            try:
                call("sendChatAction", {"chat_id": self.chat, "action": "typing"}, limit=10)
            except TelegramError:
                return
            self.stop.wait(4)

    def __enter__(self):
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.stop.set()


def download(file_id: str) -> tuple[bytes, str]:
    """A file from a message (bots get up to 20 MB) and its path at Telegram (for the extension)."""
    f = call("getFile", {"file_id": file_id})
    path = f.get("file_path") or ""
    try:
        r = httpx.get(f"{API}/file/bot{token()}/{path}", timeout=120)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise TelegramError(f"No pude bajar el archivo ({type(e).__name__}).") from e
    return r.content, path


def commands():
    """The menu Telegram shows under '/'."""
    call("setMyCommands", {"commands": [
        {"command": "lista", "description": "Lo que falta comprar"},
        {"command": "agenda", "description": "Lo que tenés esta semana"},
        {"command": "saldos", "description": "Cuánto tenés hoy"},
        {"command": "pedidos", "description": "Los pedidos a domicilio"},
        {"command": "pagos", "description": "Lo que falta pagar este mes"},
        {"command": "buendia", "description": "El día: clima, agenda, lista"},
        {"command": "server", "description": "Cómo está el server"},
        {"command": "mesa", "description": "Cómo va la cartera contra el S&P"},
        {"command": "borrar", "description": "Deshace lo último que anoté"},
        {"command": "ayuda", "description": "Todo lo que entiendo"},
    ]})
