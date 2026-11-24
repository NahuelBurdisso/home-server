"""Lista, the house's shopping list (lista/, port 3016), is where the things to order come from.
Three small endpoints there: what's pending, read a sentence the way Lista would (split, key and
section, nothing stored), and tick what was ordered. Lista may be down: every call raises Down,
and the callers answer with a sentence instead of failing."""
import os

import httpx

LISTA_URL = os.environ.get("LISTA_URL", "http://host.docker.internal:3016")
TIMEOUT = 10.0

# Tests swap this for an httpx.MockTransport (no Lista running).
transport: httpx.BaseTransport | None = None


class Down(Exception):
    """Lista didn't answer, or answered something that isn't the list."""


def _call(method: str, path: str, body: dict | None = None):
    try:
        with httpx.Client(transport=transport, timeout=TIMEOUT) as client:
            r = client.request(method, LISTA_URL + path, json=body)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise Down(str(e)) from e


def _items(data, with_ids: bool) -> list[dict]:
    if not isinstance(data, list):
        raise Down("not a list")
    out = []
    for d in data:
        if not isinstance(d, dict) or not all(isinstance(d.get(k), str) for k in ("text", "key", "section")):
            continue
        item = {"text": d["text"], "key": d["key"], "section": d["section"]}
        if with_ids:
            if not isinstance(d.get("id"), int) or isinstance(d.get("id"), bool):
                continue
            item["id"] = d["id"]
        out.append(item)
    return out


def pending() -> list[dict]:
    """[{"id", "text", "key", "section"}] of what's still on the list."""
    return _items(_call("GET", "/api/pendientes"), with_ids=True)


def read(texto: str) -> list[dict]:
    """[{"text", "key", "section"}]: a sentence split the way Lista does it, nothing stored."""
    return _items(_call("POST", "/api/leer", {"texto": texto}), with_ids=False)


def tick(ids: list[int]) -> int:
    """Ticks those (if still pending) in Lista; how many it ticked."""
    if not ids:
        return 0
    data = _call("POST", "/api/tildar", {"ids": ids})
    n = data.get("tildados") if isinstance(data, dict) else None
    return n if isinstance(n, int) else 0
