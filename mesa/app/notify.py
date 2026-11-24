"""Phone notifications through the server's ntfy (Finanzas' pattern), sent by the background thread:

- a decision filled (normal): what it bought and sold, and at how much;
- the week's summary on Friday after the close (normal): Claude against the vara;
- «Claude no vino»: 5 days without a decision (high), once until one comes;
- odd bars from Yahoo that weren't used (high); the verdict, once, at 12 months (high).

Enabled with MESA_NOTIFY=1; the topic is read from the mounted secret, like the server's other
alerts. avisar() only queues: one worker thread does the HTTP, so a slow or dead ntfy never holds
up the web or a fill. A failure is logged and dropped.

    docker exec mesa python -m app.notify prueba     # one test notification, right now
"""
import json
import logging
import os
import queue
import sys
import threading
import urllib.request

log = logging.getLogger("mesa.notify")

NTFY_URL = os.environ.get("MESA_NTFY_URL", "http://host.docker.internal:8091")
TOPIC_FILE = os.environ.get("MESA_NTFY_TOPIC_FILE", "/run/secrets/ntfy-topic")
PRIORIDADES = {1: "min", 2: "low", 3: "default", 4: "high", 5: "urgent"}

_cola: "queue.Queue[dict]" = queue.Queue(maxsize=200)
_hilo: threading.Thread | None = None
_lock = threading.Lock()


def activo() -> bool:
    return os.environ.get("MESA_NOTIFY") == "1"


def app_url() -> str:
    return os.environ.get("MESA_URL", "https://mac-server.tailnet-ejemplo.ts.net:3020").rstrip("/")


def send(titulo: str, cuerpo: str, ruta: str = "/", tags: str = "chart_with_upwards_trend", prioridad: int = 3):
    """Publish one message now (JSON publishing, so accents survive). Raises on failure."""
    topic = open(TOPIC_FILE).read().strip()
    msg = {"topic": topic, "title": titulo, "message": cuerpo, "tags": [tags], "priority": prioridad,
           "click": app_url() + ruta}
    req = urllib.request.Request(NTFY_URL, json.dumps(msg).encode(), {"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()


def _trabajar():
    while True:
        a = _cola.get()
        try:
            send(a["titulo"], a["cuerpo"], a["ruta"], a["tags"], a["prioridad"])
            log.info("ntfy: enviado «%s» (%s)", a["titulo"], PRIORIDADES.get(a["prioridad"], a["prioridad"]))
        except Exception as e:
            log.warning("ntfy: no se pudo enviar «%s»: %s", a["titulo"], e)


def avisar(titulo: str, cuerpo: str, prioridad: int = 3, tags: str = "chart_with_upwards_trend", ruta: str = "/"):
    """Queue one notification for the worker (started on first use). Never raises, never blocks."""
    global _hilo
    if not activo():
        log.info("aviso (MESA_NOTIFY apagado): %s — %s", titulo, cuerpo)
        return
    with _lock:
        if _hilo is None or not _hilo.is_alive():
            _hilo = threading.Thread(target=_trabajar, name="ntfy", daemon=True)
            _hilo.start()
    try:
        _cola.put_nowait({"titulo": titulo, "cuerpo": cuerpo, "prioridad": prioridad, "tags": tags, "ruta": ruta})
    except queue.Full:
        log.warning("ntfy: cola llena, descarto «%s»", titulo)


PRUEBA = ("Mesa: aviso de prueba", "Esto es una prueba de la Mesa (Claude contra el S&P 500, con plata de mentira). "
          "Si te llegó, también te van a llegar las ejecuciones, el resumen de los viernes y el aviso si Claude no viene.")

if __name__ == "__main__":
    if sys.argv[1:] != ["prueba"]:
        sys.exit("uso: python -m app.notify prueba")
    send(*PRUEBA, tags="test_tube")
    print("enviado")
