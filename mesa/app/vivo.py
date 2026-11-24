"""Docker's health check (python -m app.vivo); exit code 0 means alive.

Alive: the web answers /health at all (a 503 there means Yahoo is behind, which a restart doesn't
fix), and the background thread wrote its heartbeat in mesa.db within the last 30 minutes (when it
is on: MESA_TAREAS=0 turns it off).
"""
import os
import sys
import urllib.error
import urllib.request


def web() -> bool:
    try:
        urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=4)
    except urllib.error.HTTPError:
        pass  # it answered
    return True


def hilo() -> bool:
    if os.environ.get("MESA_TAREAS", "1") == "0":
        return True
    from . import db
    conn = db.conectar()
    try:
        latido = db.leer(conn, "latido")
        return latido is not None and db.ahora_ms() - latido < 30 * 60_000
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        ok = web() and hilo()
    except Exception as e:
        print(e, file=sys.stderr)
        ok = False
    sys.exit(0 if ok else 1)
