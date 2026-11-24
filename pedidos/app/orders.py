"""What the pages, Siri, the bot and the AI do with orders: build drafts from the list, edit them,
write the WhatsApp message, and follow them until they arrive."""
import os
import re
from datetime import datetime
from urllib.parse import quote, urlsplit

from . import catalogo, lista, match
from .fetch import pedix_url
from .db import get_meta
from .text import fold, key_of, nice

PUBLIC_URL = os.environ.get("PEDIDOS_URL", "https://mac-server.tailnet-ejemplo.ts.net:3022")

# Lista's sections (lista/app/parse.py), in the order you walk the store.
SECTIONS = {"verduleria": "Verdulería", "carniceria": "Carnicería", "lacteos": "Lácteos y fiambres",
            "panaderia": "Panadería", "almacen": "Almacén", "bebidas": "Bebidas", "limpieza": "Limpieza",
            "higiene": "Higiene y farmacia", "mascotas": "Mascotas", "otros": "Otros"}
KINDS = {"pedix": "Tienda en pedix", "whatsapp": "Por WhatsApp", "web": "Tienda web"}
STATUS = {"borrador": "Borrador", "enviado": "Pedido", "recibido": "Llegó", "cancelado": "Cancelado",
          "guardado": "Guardado"}
EDITABLE = ("borrador", "guardado")  # the statuses whose lines can be changed
# "pedí la verdura", "pedí al súper": the word said → the section whose shop it means.
WORDS = {"verdura": "verduleria", "verduras": "verduleria", "verduleria": "verduleria", "fruta": "verduleria",
         "frutas": "verduleria", "carne": "carniceria", "carnes": "carniceria", "carniceria": "carniceria",
         "super": "almacen", "supermercado": "almacen", "almacen": "almacen"}


def money(v) -> str:
    """$ 23.400: whole pesos, dots for thousands (the shop weighs and charges anyway)."""
    return "$ " + f"{round(v or 0):,}".replace(",", ".")


def link(order_id: int) -> str:
    return f"{PUBLIC_URL}/p/{order_id}"


def sections_of(provider) -> list[str]:
    return [s.strip() for s in (provider["sections"] or "").split(",") if s.strip()]


def missing(provider) -> str | None:
    """What a shop lacks to send an order, said for the person; None when it's ready."""
    if provider["kind"] == "pedix" and not provider["url"]:
        return "Le falta el link de pedix."
    if provider["kind"] == "whatsapp" and not provider["phone"]:
        return "Le falta el teléfono de WhatsApp."
    if provider["kind"] == "web" and not provider["url"]:
        return "Le falta el link de la tienda."
    return None


def find_provider(conn, said: str):
    """A shop by its name ("Verdufresca") or by a word for what it sells ("verdulería", "carne",
    "súper"). None if nothing fits."""
    f = " ".join(re.sub(r"[^a-z0-9 ]", " ", fold(said)).split())
    f = re.sub(r"^(?:a |al |la |el |en )+", "", f)
    if not f:
        return None
    rows = conn.execute("SELECT * FROM providers WHERE active = 1 ORDER BY id").fetchall()
    for r in rows:
        if fold(r["name"]) == f:
            return r
    section = WORDS.get(f) or next((WORDS[w] for w in f.split() if w in WORDS), None)
    for r in rows:
        if section and section in sections_of(r):
            return r
    return next((r for r in rows if f in fold(r["name"])), None)


def provider_of(conn, order):
    return conn.execute("SELECT * FROM providers WHERE id = ?", (order["provider_id"],)).fetchone()


def lines(conn, order_id: int) -> list:
    return conn.execute("SELECT * FROM lines WHERE order_id = ? ORDER BY id", (order_id,)).fetchall()


def total(conn, order_id: int) -> float:
    rows = lines(conn, order_id)
    return sum(r["qty"] * r["unit_price"] for r in rows if r["unit_price"] is not None)


def recompute(conn, order_id: int):
    conn.execute("UPDATE orders SET total_est = ? WHERE id = ?", (total(conn, order_id), order_id))
    conn.commit()


def _insert_lines(conn, order_id: int, provider_id: int, text: str, key: str, lista_id, cat) -> int:
    """The lines for one thing; not in the catalog: one line as written. How many it wrote."""
    found = match.match(conn, provider_id, text, key, cat) if cat else []
    for ln in found or [{"ext_id": None, "pres_id": "", "product": "", "presentation": "", "qty": 1, "unit_price": None}]:
        conn.execute("INSERT INTO lines (order_id, text, key, lista_id, ext_id, pres_id, product, presentation, qty, unit_price)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (order_id, text, key, lista_id, ln["ext_id"], ln["pres_id"], ln["product"], ln["presentation"],
                      ln["qty"], ln["unit_price"]))
    return max(1, len(found))


class Unavailable(Exception):
    """Lista didn't answer; the message is for the person."""


def armar(conn, texto: str = "", proveedor: str = "", quien: str = "") -> dict:
    """One draft per shop that got something, replacing that shop's previous draft. The things come
    from the text (read by Lista) or else from what's pending on the list; split by the shops'
    sections, or all to one shop when it's named. Returns {"orders": [ids], "sin_proveedor": [texts],
    "error": sentence or None}. Raises Unavailable when Lista doesn't answer."""
    out = {"orders": [], "sin_proveedor": [], "error": None}
    target = None
    if proveedor.strip():
        target = conn.execute("SELECT * FROM providers WHERE id = ? AND active = 1", (int(proveedor),)).fetchone() \
            if proveedor.strip().isdigit() else find_provider(conn, proveedor)
        if target is None:
            out["error"] = f"No conozco el proveedor «{proveedor.strip()}». Están: " + \
                ", ".join(r["name"] for r in conn.execute("SELECT name FROM providers WHERE active = 1 ORDER BY id")) + "."
            return out
    try:
        items = lista.read(texto) if texto.strip() else lista.pending()
    except lista.Down as e:
        raise Unavailable("No pude leer la Lista (¿está andando?). Probá de nuevo en un rato.") from e
    providers = [target] if target else conn.execute("SELECT * FROM providers WHERE active = 1 ORDER BY id").fetchall()
    by: dict[int, list] = {}
    for it in items:
        if target and texto.strip():  # "pedí papas y bananas a la verdulería": all of it goes there
            by.setdefault(target["id"], []).append(it)
            continue
        p = next((p for p in providers if it["section"] in sections_of(p)), None)
        if p:
            by.setdefault(p["id"], []).append(it)
        elif not target:
            out["sin_proveedor"].append(it["text"])
    if not by:
        out["error"] = ("No entendí qué pedir." if texto.strip() else
                        f"No hay nada pendiente en la Lista para {target['name']}." if target else
                        "No hay nada pendiente en la Lista para pedir.")
        return out
    for p in providers:
        if p["id"] not in by:
            continue
        catalogo.refresh(conn, p)
        cat = match.catalog(conn, p["id"])
        with conn:
            for (old,) in conn.execute("SELECT id FROM orders WHERE provider_id = ? AND status = 'borrador'", (p["id"],)).fetchall():
                conn.execute("DELETE FROM lines WHERE order_id = ?", (old,))
                conn.execute("DELETE FROM orders WHERE id = ?", (old,))
            oid = conn.execute("INSERT INTO orders (provider_id, created_by) VALUES (?, ?)", (p["id"], quien[:60])).lastrowid
            for it in by[p["id"]]:
                _insert_lines(conn, oid, p["id"], it["text"], it["key"], it.get("id"), cat)
        recompute(conn, oid)
        out["orders"].append(oid)
    return out


def add_line(conn, order, texto: str = "", ext_id: str = "", pres_id: str | None = None) -> bool:
    """A line added on the draft page: typed ("1 kg de cebolla", matched like a list line) or a
    product picked ("lo de siempre")."""
    cat = match.catalog(conn, order["provider_id"])
    if ext_id:
        rows = cat.get(ext_id)
        if not rows:
            return False
        row = next((r for r in rows if r["pres_id"] == pres_id), rows[0])
        ln = match.line(row, 1)
        conn.execute("INSERT INTO lines (order_id, text, key, ext_id, pres_id, product, presentation, qty, unit_price)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
                     (order["id"], ln["product"], row["key"], ln["ext_id"], ln["pres_id"], ln["product"], ln["presentation"],
                      ln["unit_price"]))
    else:
        texto = " ".join((texto or "").split())[:200]
        if not texto:
            return False
        texto = texto[:1].upper() + texto[1:]
        _insert_lines(conn, order["id"], order["provider_id"], texto, key_of(texto), None, cat)
    recompute(conn, order["id"])
    return True


def change_line(conn, line, choice: str) -> bool:
    """The person picked another product or presentation ("ext_id|pres_id"): the line takes it,
    keeps its quantity, and the choice is remembered for this thing at this shop."""
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (line["order_id"],)).fetchone()
    ext_id, _, pres_id = (choice or "").partition("|")
    row = conn.execute("SELECT * FROM products WHERE provider_id = ? AND ext_id = ? AND pres_id = ?",
                       (order["provider_id"], ext_id, pres_id)).fetchone()
    if row is None:
        return False
    ln = match.line(row, line["qty"])
    conn.execute("UPDATE lines SET ext_id = ?, pres_id = ?, product = ?, presentation = ?, unit_price = ? WHERE id = ?",
                 (ln["ext_id"], ln["pres_id"], ln["product"], ln["presentation"], ln["unit_price"], line["id"]))
    match.remember(conn, order["provider_id"], line["key"], ext_id, pres_id)
    recompute(conn, order["id"])
    return True


def alternatives(conn, provider_id: int, line, cat: dict) -> list[dict]:
    """What the line could be instead: the candidates for its thing (and its own product), every
    presentation of each, for the draft page's picker."""
    ids = match.candidates(cat, line["key"])[:match.ALTERNATIVES] if line["key"] else []
    if line["ext_id"] and line["ext_id"] in cat and line["ext_id"] not in ids:
        ids.insert(0, line["ext_id"])
    out = []
    for e in ids:
        for r in cat[e]:
            label = nice(r["name"]) + (f" · {r['presentation']}" if r["presentation"] else "") + \
                (f" · {money(r['price'])}" if r["price"] is not None else "")
            out.append({"value": f"{r['ext_id']}|{r['pres_id']}", "label": label,
                        "on": r["ext_id"] == line["ext_id"] and r["pres_id"] == line["pres_id"]})
    return out if len(out) > 1 else []


def usual(conn, order) -> list:
    """'Lo de siempre': products in at least two past orders to this shop, not in this one."""
    return conn.execute("""
        SELECT l.ext_id, l.pres_id, l.product, l.presentation, COUNT(DISTINCT l.order_id) AS times
        FROM lines l JOIN orders o ON o.id = l.order_id
        WHERE o.provider_id = ? AND o.status IN ('enviado', 'recibido') AND l.ext_id IS NOT NULL
          AND l.ext_id IN (SELECT ext_id FROM products WHERE provider_id = ?)
          AND l.ext_id NOT IN (SELECT ext_id FROM lines WHERE order_id = ? AND ext_id IS NOT NULL)
        GROUP BY l.ext_id HAVING COUNT(DISTINCT l.order_id) >= 2
        ORDER BY times DESC, l.product LIMIT 8""", (order["provider_id"], order["provider_id"], order["id"])).fetchall()


def pedix_money(v) -> str:
    """$37.992 or $4.999,50: as pedix writes it (no space; cents only when there are any)."""
    whole, cents = divmod(round((v or 0) * 100), 100)
    return "$" + f"{whole:,}".replace(",", ".") + (f",{cents:02d}" if cents else "")


def pedix_message(conn, order, p, now: datetime | None = None) -> str:
    """The message a pedix shop's own page writes when you order there, so the shop reads ours the same
    way: the header blocks, the lines under their category in capitals, each with its price. What only
    pedix has is left out (its order number and the map link: the order isn't in pedix), and so is
    what isn't set in Ajustes. Things not in the catalog go under OTROS, as written."""
    get = lambda k: (get_meta(conn, k) or "").strip()  # noqa: E731
    rows = lines(conn, order["id"])
    t = total(conn, order["id"])
    out = ["¡Hola! Te paso el resumen de mi pedido", ""]
    out.append(f"Tienda: {(p['url'] or '').rstrip('/').rsplit('/', 1)[-1] or p['name']}")
    out.append(f"Fecha: {now or datetime.now():%d/%m/%y - %H:%M}hs")
    out += [f"{label}: {get(k)}" for label, k in (("Nombre", "nombre"), ("Teléfono", "telefono")) if get(k)]
    out.append("")
    if get("pago"):
        out.append(f"Forma de pago: {get('pago')}")
    out += [f"Total: {pedix_money(t)}", "", "Entrega: Delivery"]
    out += [f"{label}: {get(k)}" for label, k in (("Dirección", "direccion"), ("Referencia", "referencia")) if get(k)]
    out += ["", "Mi pedido es"]
    groups: dict[str, list[str]] = {}
    for r in rows:
        if r["ext_id"] is None:
            groups.setdefault("OTROS", []).append(r["text"])
            continue
        cat = conn.execute("SELECT category FROM products WHERE provider_id = ? AND ext_id = ? LIMIT 1",
                           (p["id"], r["ext_id"])).fetchone()
        what = f"{r['qty']}x {r['product'].upper()}" + (f" ({r['presentation'].upper()})" if r["presentation"] else "")
        price = f": {pedix_money(r['qty'] * r['unit_price'])}" if r["unit_price"] is not None else ""
        groups.setdefault((cat["category"] if cat and cat["category"] else "Otros").upper(), []).append(what + price)
    for name in sorted(groups, key=lambda n: n == "OTROS"):  # as they come, what's as written last
        out += ["", name] + groups[name]
    out += ["", f"TOTAL: {pedix_money(t)}", ""]
    if get("aclaracion"):
        out += [get("aclaracion"), ""]
    out.append("Espero tu respuesta para confirmar mi pedido")
    return "\n".join(out)


def message(conn, order_id: int) -> str:
    """The WhatsApp text. Lines for what isn't set in Ajustes are left out. A pedix shop gets it the
    way its own page writes it (pedix_message)."""
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    p = provider_of(conn, order) if order else None
    if p is not None and p["kind"] == "pedix":
        return pedix_message(conn, order, p)
    out = ["Hola! Quiero hacer un pedido a domicilio:"]
    for r in lines(conn, order_id):
        if r["ext_id"] is None:
            out.append(f"• {r['text']}")
        else:
            out.append(f"• {r['qty']} × {r['product']}" + (f" ({r['presentation']})" if r["presentation"] else ""))
    t = total(conn, order_id)
    if t:
        out.append(f"Total estimado: {money(t)}")
    if direccion := (get_meta(conn, "direccion") or "").strip():
        referencia = (get_meta(conn, "referencia") or "").strip()
        out.append(f"Entrega: {direccion}" + (f" ({referencia})" if referencia else ""))
    if aclaracion := (get_meta(conn, "aclaracion") or "").strip():
        out.append(aclaracion)
    nombre = (get_meta(conn, "nombre") or "").strip()
    out.append(f"Gracias! {nombre}".strip())
    return "\n".join(out)


def wa_link(phone: str, text: str) -> str | None:
    digits = re.sub(r"\D", "", phone or "")
    return f"https://wa.me/{digits}?text={quote(text, safe='')}" if digits else None


def send_links(conn, order) -> dict:
    """{"whatsapp": url or None, "tienda": url or None, "falta": sentence or None}."""
    p = provider_of(conn, order)
    wa = wa_link(p["phone"], message(conn, order["id"])) if p["kind"] in ("pedix", "whatsapp") else None
    shop = p["url"] if p["kind"] in ("pedix", "web") and p["url"] else None
    falta = missing(p) or (None if wa or shop else "Le falta el teléfono de WhatsApp.")
    return {"whatsapp": wa, "tienda": shop, "falta": falta}


def short_of_minimum(conn, order) -> float:
    p = provider_of(conn, order)
    t = total(conn, order["id"])
    return max(0.0, (p["min_purchase"] or 0) - t) if p["min_purchase"] and t else 0.0


def set_status(conn, order_id: int, status: str) -> str:
    """enviado (ticks its Lista lines), recibido or cancelado. A sentence for the page."""
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        return "Ese pedido no existe."
    allowed = {"enviado": ("borrador",), "recibido": ("borrador", "enviado"), "cancelado": ("borrador", "enviado")}
    if order["status"] == "guardado":
        return "Es un pedido guardado: usalo para armar un borrador y mandá ese."
    if order["status"] not in allowed.get(status, ()):
        return f"Ese pedido ya está {STATUS[order['status']].lower()}."
    col = {"enviado": "sent_at", "recibido": "received_at"}.get(status)
    conn.execute(f"UPDATE orders SET status = ?{f', {col} = datetime(?, ?)' if col else ''} WHERE id = ?",
                 (status, "now", "localtime", order_id) if col else (status, order_id))
    conn.commit()
    if status == "enviado":
        ids = sorted({r["lista_id"] for r in lines(conn, order_id) if r["lista_id"] is not None})
        if not ids:
            return "Listo, quedó como pedido."
        try:
            lista.tick(ids)
        except lista.Down:
            return "Listo, quedó como pedido. No pude tildarlo en la Lista (¿está andando?): hacelo a mano."
        return "Listo, quedó como pedido y lo tildé en la Lista."
    return "Listo: llegó." if status == "recibido" else "Pedido cancelado."


# --- guardados: orders saved to reuse ("lo de siempre de la verdulería") ------------------------------

def clean_name(name: str) -> str:
    return " ".join((name or "").split())[:60]


def save(conn, order, name: str = "", quien: str = "") -> int:
    """Copies an order (any status) into a guardado of the same shop, named `name` (empty: the shop's
    name). One with the same name at that shop is replaced. Lines lose lista_id: using a guardado must
    never tick the Lista. A guardado isn't saved again (its own id). Returns the guardado's id."""
    if order["status"] == "guardado":
        return order["id"]
    p = provider_of(conn, order)
    name = clean_name(name) or (p["name"] if p else "Pedido")
    with conn:
        same = conn.execute("SELECT id, name FROM orders WHERE provider_id = ? AND status = 'guardado'",
                            (order["provider_id"],)).fetchall()
        old = next((r["id"] for r in same if fold(r["name"]) == fold(name)), None)
        if old:
            conn.execute("DELETE FROM lines WHERE order_id = ?", (old,))
            conn.execute("UPDATE orders SET name = ?, created_by = ?, created_at = datetime('now', 'localtime')"
                         " WHERE id = ?", (name, quien[:60], old))
            sid = old
        else:
            sid = conn.execute("INSERT INTO orders (provider_id, status, name, created_by) VALUES (?, 'guardado', ?, ?)",
                               (order["provider_id"], name, quien[:60])).lastrowid
        conn.execute("INSERT INTO lines (order_id, text, key, lista_id, ext_id, pres_id, product, presentation, qty, unit_price)"
                     " SELECT ?, text, key, NULL, ext_id, pres_id, product, presentation, qty, unit_price"
                     " FROM lines WHERE order_id = ? ORDER BY id", (sid, order["id"]))
    recompute(conn, sid)
    return sid


def reprice(conn, order_id: int):
    """Today's product, presentation and price for each catalog line, from the stored catalog (no
    network). A product that left the catalog: the line goes as written."""
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    for r in lines(conn, order_id):
        if r["ext_id"] is None:
            continue
        row = conn.execute("SELECT * FROM products WHERE provider_id = ? AND ext_id = ? AND pres_id = ?",
                           (order["provider_id"], r["ext_id"], r["pres_id"])).fetchone()
        if row:
            ln = match.line(row, r["qty"])
            conn.execute("UPDATE lines SET product = ?, presentation = ?, unit_price = ? WHERE id = ?",
                         (ln["product"], ln["presentation"], ln["unit_price"], r["id"]))
        else:  # as written, only the text goes in the message: the amount moves into it
            text = r["text"] if r["qty"] == 1 else f"{r['qty']} × {r['text']}"
            conn.execute("UPDATE lines SET text = ?, qty = 1, ext_id = NULL, pres_id = '', product = '', presentation = '',"
                         " unit_price = NULL WHERE id = ?", (text[:200], r["id"]))
    recompute(conn, order_id)


def use(conn, saved, quien: str = "") -> dict:
    """A guardado's lines into its shop's draft, at today's prices: added to the draft there is (never
    replacing it), or a new one. Lines already in the draft are skipped. Returns {"order": draft id,
    "added": n, "skipped": n, "new": bool}. Raises Invalid when the shop is gone or the guardado is empty."""
    p = provider_of(conn, saved)
    if p is None or not p["active"]:
        raise Invalid(f"{p['name'] if p else 'Ese proveedor'} no está activo: activalo en Proveedores para usar "
                      f"«{saved['name']}».")
    catalogo.refresh(conn, p)
    reprice(conn, saved["id"])
    wanted = lines(conn, saved["id"])
    if not wanted:
        raise Invalid(f"«{saved['name']}» está vacío: agregale algo antes de usarlo.")
    with conn:
        draft = conn.execute("SELECT id FROM orders WHERE provider_id = ? AND status = 'borrador' ORDER BY id DESC LIMIT 1",
                             (p["id"],)).fetchone()
        did = draft["id"] if draft else \
            conn.execute("INSERT INTO orders (provider_id, created_by) VALUES (?, ?)", (p["id"], quien[:60])).lastrowid
        have = lines(conn, did)
        products = {(r["ext_id"], r["pres_id"]) for r in have if r["ext_id"] is not None}
        texts = {fold(" ".join(r["text"].split())) for r in have if r["ext_id"] is None}
        added = skipped = 0
        for r in wanted:
            if r["ext_id"] is not None:
                seen, mark = products, (r["ext_id"], r["pres_id"])
            else:
                seen, mark = texts, fold(" ".join(r["text"].split()))
            if mark in seen:
                skipped += 1
                continue
            seen.add(mark)
            conn.execute("INSERT INTO lines (order_id, text, key, lista_id, ext_id, pres_id, product, presentation, qty,"
                         " unit_price) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)",
                         (did, r["text"], r["key"], r["ext_id"], r["pres_id"], r["product"], r["presentation"], r["qty"],
                          r["unit_price"]))
            added += 1
    recompute(conn, did)
    return {"order": did, "added": added, "skipped": skipped, "new": draft is None}


def reprice_saved(conn, fetch: bool = False) -> int:
    """Every guardado at today's prices, so the lists and sentences that show their totals don't show old
    ones. From the stored catalogs; fetch=True reads the shops' catalogs again first (the ones older
    than 3 h): that's the background job in main.py. How many guardados there are."""
    rows = conn.execute("SELECT id, provider_id FROM orders WHERE status = 'guardado'").fetchall()
    if fetch:
        for pid in sorted({r["provider_id"] for r in rows}):
            p = conn.execute("SELECT * FROM providers WHERE id = ? AND active = 1", (pid,)).fetchone()
            if p:
                catalogo.refresh(conn, p)  # a failure keeps the old catalog and leaves its error for the page
    for r in rows:
        reprice(conn, r["id"])
    return len(rows)


def saved_orders(conn) -> list:
    """The guardados with their shop's name and how many things (n), by shop and then name."""
    return conn.execute("SELECT o.*, p.name AS provider, (SELECT COUNT(DISTINCT text) FROM lines WHERE order_id = o.id) AS n"
                        " FROM orders o JOIN providers p ON p.id = o.provider_id WHERE o.status = 'guardado'"
                        " ORDER BY o.provider_id, o.name COLLATE NOCASE, o.id").fetchall()


def find_saved(conn, said: str):
    """A guardado by its name ("lo de siempre"), a part of it, or its shop when the shop has only one."""
    f = " ".join(fold(re.sub(r"[«»\"'¿?¡!.]", " ", said or "")).split())
    if not f:
        return None
    rows = saved_orders(conn)
    for test in (lambda r: fold(r["name"]) == f, lambda r: f in fold(r["name"])):
        if found := next((r for r in rows if test(r)), None):
            return found
    p = find_provider(conn, said)
    mine = [r for r in rows if p and r["provider_id"] == p["id"]]
    return mine[0] if len(mine) == 1 else None


def delete_saved(conn, order_id: int) -> bool:
    with conn:
        if conn.execute("SELECT 1 FROM orders WHERE id = ? AND status = 'guardado'", (order_id,)).fetchone() is None:
            return False
        conn.execute("DELETE FROM lines WHERE order_id = ?", (order_id,))
        conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
    return True


def rename(conn, order_id: int, name: str) -> bool:
    """Only a guardado, and only to a name that isn't empty."""
    name = clean_name(name)
    if not name:
        return False
    cur = conn.execute("UPDATE orders SET name = ? WHERE id = ? AND status = 'guardado'", (name, order_id))
    conn.commit()
    return cur.rowcount == 1


# --- sentences for Siri, the bot and the AI ------------------------------------------------------------

def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " y " + names[-1]


def block(conn, order_id: int) -> str:
    """One order, for /api/armar: shop, lines, not found, total, minimum, links."""
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    p = provider_of(conn, order)
    rows = lines(conn, order_id)
    things = list(dict.fromkeys(r["text"] for r in rows))
    lost = list(dict.fromkeys(r["text"] for r in rows if r["ext_id"] is None))
    t = total(conn, order_id)
    out = [f"{p['name']}: {len(things)} cosa{'s' if len(things) != 1 else ''}"
           + (f", {money(t)} estimado." if t else ".")]
    if lost and p["kind"] == "pedix":
        out.append(f"No está en el catálogo (va como está escrito): {_join([x[:1].lower() + x[1:] for x in lost])}.")
    if short := short_of_minimum(conn, order):
        out.append(f"El mínimo es {money(p['min_purchase'])}: faltan {money(short)}.")
    if p["kind"] == "pedix" and p["catalog_error"]:
        out.append(f"Precios de {p['catalog_at'][:16] if p['catalog_at'] else 'nunca'} (no pude leer el catálogo).")
    out.append(f"Revisalo: {link(order_id)}")
    links = send_links(conn, order)
    if links["whatsapp"]:
        out.append(f"Para mandarlo: {links['whatsapp']}")
    elif links["falta"]:
        out.append(f"{links['falta']} Cargalo en {PUBLIC_URL}/proveedores.")
    return "\n".join(out)


def armar_sentence(conn, res: dict) -> str:
    if res["error"]:
        return res["error"]
    parts = [block(conn, oid) for oid in res["orders"]]
    if res["sin_proveedor"]:
        parts.append(f"Sin proveedor (quedan en la Lista): {_join(res['sin_proveedor'])}.")
    return "\n\n".join(parts)


def open_orders(conn) -> list:
    return conn.execute("SELECT o.*, p.name AS provider FROM orders o JOIN providers p ON p.id = o.provider_id"
                        " WHERE o.status IN ('borrador', 'enviado') ORDER BY o.status = 'enviado', o.created_at DESC, o.id DESC"
                        ).fetchall()


def open_sentence(conn) -> str:
    rows = open_orders(conn)
    if not rows:
        return "No hay pedidos abiertos."
    parts = []
    for o in rows:
        n = len({r["text"] for r in lines(conn, o["id"])})
        what = f"{n} cosa{'s' if n != 1 else ''}" + (f", {money(o['total_est'])}" if o["total_est"] else "")
        if o["status"] == "borrador":
            parts.append(f"{o['provider']}: borrador sin mandar, {what}")
        else:
            parts.append(f"{o['provider']}: pedido el {o['sent_at'][8:10]}/{o['sent_at'][5:7]} a las {o['sent_at'][11:16]},"
                         f" {what}, todavía no llegó")
    return f"Hay {len(rows)} pedido{'s' if len(rows) != 1 else ''} abierto{'s' if len(rows) != 1 else ''}. " + \
        ". ".join(parts) + "."


def _things(n: int) -> str:
    return f"{n} cosa{'s' if n != 1 else ''}"


def saved_sentence(conn) -> str:
    rows = saved_orders(conn)
    if not rows:
        return "No hay pedidos guardados."
    parts = [f"«{o['name']}» ({o['provider']}, {_things(o['n'])}"
             + (f", {money(o['total_est'])})" if o["total_est"] else ")") for o in rows]
    return f"Hay {len(rows)} pedido{'s' if len(rows) != 1 else ''} guardado{'s' if len(rows) != 1 else ''}: {_join(parts)}."


def unknown_saved_sentence(conn, said: str) -> str:
    """What to answer when no guardado fits what was said: which ones there are."""
    rows = saved_orders(conn)
    if not rows:
        return "No hay pedidos guardados."
    names = _join([f"«{o['name']}»" for o in rows])
    return (f"No encontré «{said.strip()}» entre los guardados. " if said.strip() else "¿Cuál? ") + f"Están: {names}."


def use_sentence(conn, res: dict, saved) -> str:
    """What use() did, in its first line; then the draft as /api/armar tells it."""
    p = provider_of(conn, saved)
    if res["new"]:
        first = f"Armé el borrador con «{saved['name']}»."
    elif res["added"]:
        first = f"Agregué {_things(res['added'])} de «{saved['name']}» al borrador de {p['name']}" + \
            (f" ({res['skipped']} ya estaba{'n' if res['skipped'] != 1 else ''})." if res["skipped"] else ".")
    else:
        first = f"Todo lo de «{saved['name']}» ya estaba en el borrador."
    return first + "\n" + block(conn, res["order"])


def prices(conn, q: str) -> list[dict]:
    """What the catalogs have for q: [{"proveedor", "producto", "presentaciones": [(name, price)]}]."""
    out = []
    key = key_of(q)
    for p in conn.execute("SELECT * FROM providers WHERE active = 1 AND kind = 'pedix' ORDER BY id"):
        cat = match.catalog(conn, p["id"])
        for e in match.candidates(cat, key)[:3]:
            out.append({"proveedor": p["name"], "producto": nice(cat[e][0]["name"]), "al": p["catalog_at"],
                        "presentaciones": [(r["presentation"], r["price"]) for r in cat[e] if r["price"] is not None]})
    return out


def price_sentence(conn, q: str) -> str:
    q = (q or "").strip()
    if not q:
        return "¿De qué querés saber el precio?"
    found = prices(conn, q)
    if not found:
        return f"No encontré «{q}» en los catálogos."
    parts = []
    for f in found:
        each = [f"{money(price)} {'el ' + name if name else ''}".strip() for name, price in f["presentaciones"]]
        parts.append(f"{f['producto']} en {f['proveedor']}: " + (", ".join(each) if each else "sin precio"))
    return ". ".join(parts) + ". Son precios estimados: pesan y cobran ellos."


# --- shops --------------------------------------------------------------------------------------------

class Invalid(Exception):
    """A shop's link or phone that can't be used, or a guardado that can't be used now. The message is
    for the person."""


def phone_of(s: str) -> str:
    """WhatsApp's international digits: "+54 9 351 555-0123" → 5493515550123; a local number with
    area code ("0351 15 ...", "351 5550123") gets Argentina's 549 in front."""
    digits = re.sub(r"\D", "", s or "")
    if digits.startswith("0"):
        digits = digits.lstrip("0")
    if len(digits) == 10:
        digits = "549" + digits
    if not 10 <= len(digits) <= 15:
        raise Invalid("Ese teléfono no parece completo: poné código de área (ej. 351 207 9635).")
    return digits


def web_url(s: str) -> str:
    s = (s or "").strip()
    try:
        parts = urlsplit(s)
    except ValueError:
        parts = None
    if not parts or parts.scheme != "https" or not parts.hostname or "." not in parts.hostname:
        raise Invalid("El link de la tienda tiene que empezar con https://.")
    return s


def classify(s: str) -> tuple[str, str, str]:
    """What was pasted on the Proveedores page → (kind, url, phone): a pedix link, a phone, or a site."""
    s = (s or "").strip()
    if not s:
        raise Invalid("Pegá un link de pedix, un teléfono o el link de la tienda.")
    if "pedix.app" in s.lower():
        url = pedix_url(s)
        if not url:
            raise Invalid("Ese link de pedix no lo entiendo: tiene que ser https://pedix.app/<tienda>.")
        return "pedix", url, ""
    if re.fullmatch(r"[\d\s()+.-]+", s):
        return "whatsapp", "", phone_of(s)
    return "web", web_url(s), ""
