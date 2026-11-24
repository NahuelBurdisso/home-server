"""What the pages, Siri and the AI do to the list."""
import re

from .parse import ORDER, SECTIONS, key_of, section_of, split


def pending(conn) -> list:
    rows = conn.execute("SELECT * FROM items WHERE done_at IS NULL AND cleared_at IS NULL ORDER BY added_at, id").fetchall()
    return sorted(rows, key=lambda r: ORDER.index(r["section"]) if r["section"] in ORDER else len(ORDER))


def cart(conn) -> list:
    return conn.execute("SELECT * FROM items WHERE done_at IS NOT NULL AND cleared_at IS NULL ORDER BY done_at DESC").fetchall()


def add(conn, text: str) -> dict:
    """{'added': [names], 'already': [names], 'ids': [ids of the added]}: something already on the
    list isn't added twice (one ticked in the cart goes back to the list instead)."""
    added, already, ids = [], [], []
    for thing in split(text if isinstance(text, str) else ""):
        key = key_of(thing)
        # The same thing, also in singular or plural: "2 leches" when "Leche" is there.
        open_rows = conn.execute("SELECT id, key, done_at FROM items WHERE cleared_at IS NULL"
                                 " ORDER BY key != ?, done_at IS NOT NULL, id", (key,)).fetchall()
        row = next((r for r in open_rows if _same(r["key"], key)), None)
        if row and row["done_at"] is None:
            already.append(thing)
            continue
        if row:  # in the cart: it's needed again
            conn.execute("UPDATE items SET done_at = NULL, text = ? WHERE id = ?", (thing, row["id"]))
            ids.append(row["id"])
        else:
            ids.append(conn.execute("INSERT INTO items (text, key, section) VALUES (?, ?, ?)",
                                    (thing, key, section_of(thing))).lastrowid)
        added.append(thing)
    conn.commit()
    return {"added": added, "already": already, "ids": ids}


def toggle(conn, item_id: int) -> bool | None:
    """Ticked (in the cart) or back on the list; None if it doesn't exist (any more)."""
    row = conn.execute("SELECT done_at FROM items WHERE id = ? AND cleared_at IS NULL", (item_id,)).fetchone()
    if not row:
        return None
    conn.execute("UPDATE items SET done_at = CASE WHEN done_at IS NULL THEN datetime('now', 'localtime') END WHERE id = ?",
                 (item_id,))
    conn.commit()
    return row["done_at"] is None


# What's said before the thing when it was bought: "ya compré la", "compré los", "tengo el"...
BOUGHT = re.compile(r"^(?:ya\s+)?(?:(?:compre|compramos|traje|trajimos|tengo|tenemos|consegui|esta|estan|hay)\b\s*)?"
                    r"(?:(?:los|las|la|el|unos|unas|una|un)\b\s*)?")


def _forms(word: str) -> set[str]:
    """A word and what it could be in singular: 'tomates' -> tomates, tomate, tomat."""
    out = {word}
    if word.endswith("s"):
        out.add(word[:-1])
    if word.endswith("es"):
        out.add(word[:-2])
    return out


def _same(a: str, b: str) -> bool:
    """The same thing, singular or plural: 'papas' = 'papa', 'tomates' = 'tomate', 'limones' = 'limon'."""
    wa, wb = a.split(), b.split()
    return len(wa) == len(wb) and all(_forms(x) & _forms(y) for x, y in zip(wa, wb))


def tick(conn, text: str) -> list[str]:
    """Ticks the things named in a sentence (for the AI: "ya compré la leche y el pan"): an item
    whose name is one of the things said, not just a word of it ("pan rallado" isn't "pan")."""
    said = [BOUGHT.sub("", key_of(t)).strip() for t in split(text or "")]
    done = []
    for r in pending(conn):
        if any(_same(r["key"], s) for s in said if s):
            conn.execute("UPDATE items SET done_at = datetime('now', 'localtime') WHERE id = ?", (r["id"],))
            done.append(r["text"])
    conn.commit()
    return done


def tick_ids(conn, ids: list[int]) -> int:
    """Ticks these items if they are still pending (for Pedidos, once an order is sent); ticking
    one twice does nothing. How many it ticked."""
    n = 0
    for i in dict.fromkeys(ids):
        n += conn.execute("UPDATE items SET done_at = datetime('now', 'localtime')"
                          " WHERE id = ? AND done_at IS NULL AND cleared_at IS NULL", (i,)).rowcount
    conn.commit()
    return n


def delete(conn, item_id: int):
    conn.execute("DELETE FROM items WHERE id = ? AND cleared_at IS NULL", (item_id,))
    conn.commit()


def finish(conn) -> int:
    """'Terminé las compras': what's in the cart becomes history."""
    n = conn.execute("UPDATE items SET cleared_at = datetime('now', 'localtime')"
                     " WHERE done_at IS NOT NULL AND cleared_at IS NULL").rowcount
    conn.commit()
    return n


def usual(conn, limit: int = 8, include_pending: bool = False) -> list:
    """What you buy most (bought at least twice), most often first; leaves out what's already on
    the list unless include_pending."""
    rows = conn.execute("""
        SELECT key, COUNT(*) AS times, MAX(cleared_at) AS last,
               (SELECT text FROM items i2 WHERE i2.key = i.key AND i2.cleared_at IS NOT NULL
                ORDER BY i2.cleared_at DESC LIMIT 1) AS text
        FROM items i WHERE cleared_at IS NOT NULL GROUP BY key HAVING COUNT(*) >= 2
        ORDER BY times DESC, last DESC""").fetchall()
    if not include_pending:
        open_keys = {r[0] for r in conn.execute("SELECT key FROM items WHERE cleared_at IS NULL AND done_at IS NULL")}
        rows = [r for r in rows if r["key"] not in open_keys]
    return rows[:limit]


def trips(conn, limit: int = 20) -> list[dict]:
    """Past shopping trips (each 'terminé las compras'), newest first."""
    out = {}
    for r in conn.execute("SELECT cleared_at, text, section FROM items WHERE cleared_at IS NOT NULL"
                          " ORDER BY cleared_at DESC, section").fetchall():
        out.setdefault(r["cleared_at"], []).append(r)
    return [{"when": k, "items": v} for k, v in list(out.items())[:limit]]


def _join(names: list[str]) -> str:
    names = [n[:1].lower() + n[1:] for n in names]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " y " + names[-1]


def added_sentence(res: dict) -> str:
    if not res["added"] and not res["already"]:
        return "No entendí qué agregar. Probá: leche, pan y yerba."
    parts = []
    if res["added"]:
        parts.append(f"Anotado: {_join(res['added'])}.")
    if res["already"]:
        parts.append(f"Ya estaba{'n' if len(res['already']) > 1 else ''}: {_join(res['already'])}.")
    return " ".join(parts)


def list_sentence(conn) -> str:
    rows = pending(conn)
    if not rows:
        return "La lista está vacía."
    by = {}
    for r in rows:
        by.setdefault(r["section"], []).append(r["text"])
    parts = [f"{SECTIONS[s][0]}: {_join(names)}" for s, names in by.items()]
    return f"Faltan {len(rows)} cosa{'s' if len(rows) > 1 else ''}. " + ". ".join(parts) + "."
