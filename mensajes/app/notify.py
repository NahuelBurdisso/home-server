"""To the iPhone through the server's ntfy (the server's own topic): someone wrote to the bot without
an invitation, or used one. (Telegram unreachable shows on /health: Kuma tells.) Everything that
goes through ntfy also lands in Pulso → eventos."""
import json
import os
import urllib.request

NTFY_URL = os.environ.get("NTFY_URL", "http://host.docker.internal:8091")
TOPIC_FILE = os.environ.get("NTFY_TOPIC_FILE", "/run/secrets/ntfy-topic")
PUBLIC_URL = os.environ.get("MENSAJES_URL", "https://mac-server.tailnet-ejemplo.ts.net:3019")


def enabled() -> bool:
    return os.environ.get("MENSAJES_NOTIFY", "1") == "1" and os.path.exists(TOPIC_FILE)


def publish(title: str, message: str, tags: list[str], priority: int = 3, path: str = "/"):
    if not enabled():
        return
    with open(TOPIC_FILE) as f:
        topic = f.read().strip()
    body = {"topic": topic, "title": title, "message": message, "tags": tags, "priority": priority,
            "click": PUBLIC_URL + path}
    req = urllib.request.Request(NTFY_URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except OSError:
        pass  # ntfy down: Kuma already tells about that
