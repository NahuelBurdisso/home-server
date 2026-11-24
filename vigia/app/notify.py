"""Where a watch's alert or a finished summary goes: to the Telegram chat that asked for it, through
Mensajes (/api/enviar with "chat"); when there's no chat (it was added on the page) or Mensajes
doesn't take it, to ntfy on the house's topic, the one Recordatorios uses (both phones are
subscribed to it; the server's own topic is only Nahuel's)."""
import json
import os
import re
import urllib.request

import httpx

MENSAJES_URL = os.environ.get("MENSAJES_URL", "http://host.docker.internal:3019")
NTFY_URL = os.environ.get("NTFY_URL", "http://host.docker.internal:8091")
TOPIC_FILE = os.environ.get("NTFY_TOPIC_FILE", "/run/secrets/ntfy-topic")
PUBLIC_URL = os.environ.get("VIGIA_URL", "https://mac-server.tailnet-ejemplo.ts.net:3021")


def telegram(text: str, chat: str) -> bool:
    try:
        r = httpx.post(f"{MENSAJES_URL}/api/enviar", json={"texto": text, "chat": str(chat)}, timeout=20)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def ntfy(title: str, text: str, path: str = "/") -> bool:
    """JSON publishing, so accents survive (HTTP headers can't carry them). Telegram's *bold* and
    _italic_ marks are taken out: ntfy would show them as they are."""
    try:
        with open(TOPIC_FILE) as f:
            topic = f.read().strip()
        plain = re.sub(r"(?<!\w)[*_]|[*_](?!\w)", "", text)
        msg = {"topic": topic, "title": title, "message": plain, "tags": ["eyes"], "click": PUBLIC_URL + path}
        req = urllib.request.Request(NTFY_URL, json.dumps(msg).encode(), {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).close()
        return True
    except Exception as e:  # noqa: BLE001 - ntfy down or no topic: Kuma and Pulso tell about that
        print(f"vigia: ntfy: {type(e).__name__}: {e}", flush=True)
        return False


def send(text: str, chat: str | None = None, title: str = "Vigía", path: str = "/") -> str | None:
    """'telegram', 'ntfy' or None (nothing took it)."""
    if chat and telegram(text, chat):
        return "telegram"
    return "ntfy" if ntfy(title, text, path) else None
