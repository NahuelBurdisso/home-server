"""A pedix shop's catalog, read from its page. pedix renders on the server and leaves the whole
catalog as JSON in <script id="ng-state">, so no JavaScript is needed: products, presentations,
prices, the minimum purchase and the shop's phone.

Everything in it is untrusted data: names are only stored and shown (escaped), numbers are checked
to be numbers, and nothing of it is ever given to a model."""
import json
import re
from datetime import datetime, timedelta

from . import fetch
from .text import fold, presentation_amount

STATE = re.compile(r"""<script[^>]*\bid=["']ng-state["'][^>]*>(.*?)</script>""", re.S | re.I)
STALE = timedelta(hours=3)
MAX_NAME = 120


class CatalogError(Exception):
    """The page doesn't carry a catalog we can read. The message is for the person."""


def _price(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None


def _name(v) -> str:
    return " ".join(v.split())[:MAX_NAME] if isinstance(v, str) else ""


def key(name: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", fold(name)).split())


def parse(html: str) -> dict:
    """{"name", "phone", "min_purchase", "products": [one dict per product × presentation]}.
    Raises CatalogError."""
    m = STATE.search(html or "")
    if not m:
        raise CatalogError("La página no trae el catálogo (¿cambió pedix?).")
    try:
        state = json.loads(m.group(1))
        catalog = state["app-request-context-state"]["catalog"]
        est = catalog["establishment"]
        categories = catalog["categories"]
    except (ValueError, KeyError, TypeError) as e:
        raise CatalogError("No entiendo el catálogo de la página (¿cambió pedix?).") from e
    if not isinstance(categories, list) or not isinstance(est, dict):
        raise CatalogError("No entiendo el catálogo de la página (¿cambió pedix?).")
    config = est.get("catalogConfiguration") if isinstance(est.get("catalogConfiguration"), dict) else {}
    phone = re.sub(r"\D", "", est.get("phone") if isinstance(est.get("phone"), str) else "")
    products = []
    for cat in categories:
        if not isinstance(cat, dict) or cat.get("active") is not True:
            continue
        for p in cat.get("products") or []:
            if not isinstance(p, dict) or p.get("active") is not True or not isinstance(p.get("id"), str):
                continue
            name = _name(p.get("name"))
            if not name:
                continue
            base = {"ext_id": p["id"][:80], "name": name, "key": key(name), "category": _name(cat.get("name"))}
            pres = p.get("presentations")
            items = pres.get("items") if isinstance(pres, dict) else None
            items = [i for i in items or [] if isinstance(i, dict) and isinstance(i.get("id"), str) and _name(i.get("name"))]
            if not items:
                products.append({**base, "presentation": "", "pres_id": "", "price": _price(p.get("price")),
                                 "grams": None, "units": None})
            for i in items:
                grams, units = presentation_amount(i["name"])
                products.append({**base, "presentation": _name(i["name"]), "pres_id": i["id"][:80],
                                 "price": _price(i.get("price")), "grams": grams, "units": units})
    if not products:
        raise CatalogError("El catálogo de la página está vacío.")
    return {"name": _name(est.get("name")), "phone": phone[:20],
            "min_purchase": _price(config.get("minimumPurchase")), "products": products}


def store(conn, provider_id: int, cat: dict):
    """Replaces the provider's catalog in one transaction; the phone and minimum fill in only when
    the person hasn't set them (a hand-written one wins)."""
    with conn:
        conn.execute("DELETE FROM products WHERE provider_id = ?", (provider_id,))
        conn.executemany(
            "INSERT OR IGNORE INTO products (provider_id, ext_id, name, key, category, presentation, pres_id, price, grams, units)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(provider_id, p["ext_id"], p["name"], p["key"], p["category"], p["presentation"], p["pres_id"], p["price"],
              p["grams"], p["units"]) for p in cat["products"]])
        conn.execute("UPDATE providers SET catalog_at = datetime('now', 'localtime'), catalog_error = NULL,"
                     " phone = CASE WHEN phone = '' THEN ? ELSE phone END,"
                     " min_purchase = COALESCE(min_purchase, ?) WHERE id = ?",
                     (cat["phone"], cat["min_purchase"], provider_id))


def refresh(conn, provider, force: bool = False) -> str | None:
    """Reads the catalog again if it's older than 3 h (or force). On failure the old one stays and
    the error is kept for the page. Returns the error, or None."""
    if provider["kind"] != "pedix":
        return None
    if not force and provider["catalog_at"] and \
            datetime.now() - datetime.fromisoformat(provider["catalog_at"]) < STALE:
        return None
    try:
        store(conn, provider["id"], parse(fetch.get(provider["url"])))
        return None
    except (fetch.FetchError, CatalogError) as e:
        conn.execute("UPDATE providers SET catalog_error = ? WHERE id = ?", (str(e), provider["id"]))
        conn.commit()
        return str(e)
