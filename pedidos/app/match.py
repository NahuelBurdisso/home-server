"""Which product of a shop's catalog a list line is, and how many of which presentation. Rules
decide, never a model (decision 5):

1. The house's choice for this thing at this shop (prefs), if that product still exists.
2. Else products whose name has every word of the thing, singular or plural, ranked: name equal to
   it, then starting with it, then fewest extra words, then name. None → it goes as written.
3. The amount is covered with presentations, largest first (1.5 kg → 1 KG + 500 GR); what's left
   over rounds up with the smallest one. No amount, or one the product can't express → 1 × the
   preferred presentation, else the first."""
from .text import forms, line_amount, nice, same, words

STOP = {"de", "del", "la", "el", "los", "las", "con", "y", "a", "al", "en", "para"}
ALTERNATIVES = 6


def catalog(conn, provider_id: int) -> dict[str, list]:
    """ext_id → its rows (one per presentation), in the catalog's order."""
    out: dict[str, list] = {}
    for r in conn.execute("SELECT * FROM products WHERE provider_id = ? ORDER BY rowid", (provider_id,)):
        out.setdefault(r["ext_id"], []).append(r)
    return out


def _key_words(key: str) -> list[str]:
    ws = words(key)
    return [w for w in ws if w not in STOP] or ws


def _has(name_words: list[str], kw: str) -> bool:
    f = forms(kw)
    return any(f & forms(n) for n in name_words)


def candidates(cat: dict[str, list], key: str) -> list[str]:
    """ext_ids whose name has every word of key, best first."""
    kws = _key_words(key)
    if not kws:
        return []
    ranked = []
    for ext_id, rows in cat.items():
        name = rows[0]["key"]
        nws = name.split()
        if not all(_has(nws, kw) for kw in kws):
            continue
        starts = len(nws) >= len(kws) and same(" ".join(nws[:len(kws)]), " ".join(kws))
        ranked.append(((not same(name, " ".join(kws)), not starts, len(nws) - len(kws), name), ext_id))
    return [e for _, e in sorted(ranked)]


def _cover(amount: float, rows: list, attr: str) -> list[tuple]:
    """Greedy, largest first; a leftover takes one more of the smallest."""
    sized = sorted((r for r in rows if r[attr]), key=lambda r: -r[attr])
    if not sized:
        return []
    out, left = {}, amount
    for r in sized:
        n = int(left / r[attr] + 1e-9)
        if n:
            out[r["pres_id"]] = (r, n)
            left -= n * r[attr]
    if left > 1e-6:
        r = sized[-1]
        out[r["pres_id"]] = (r, out.get(r["pres_id"], (r, 0))[1] + 1)
    return sorted(out.values(), key=lambda x: -x[0][attr])


def pieces(rows: list, grams: float | None, units: float | None, pres_id: str | None = None) -> list[tuple]:
    """[(product row, qty)] for an amount of this product."""
    if grams and (got := _cover(grams, rows, "grams")):
        return got
    if units:
        if got := _cover(units, rows, "units"):
            return got
        if len(rows) == 1 and rows[0]["presentation"] == "" and units == int(units):
            return [(rows[0], int(units))]  # "3 mangos", sold one by one at its own price
    chosen = next((r for r in rows if pres_id is not None and r["pres_id"] == pres_id), rows[0])
    return [(chosen, 1)]


def line(row, qty: int) -> dict:
    return {"ext_id": row["ext_id"], "pres_id": row["pres_id"], "product": nice(row["name"]),
            "presentation": row["presentation"], "qty": qty, "unit_price": row["price"]}


def match(conn, provider_id: int, text: str, key: str, cat: dict | None = None) -> list[dict]:
    """The order lines for one list line; [] when the catalog doesn't have it (it goes as written)."""
    cat = catalog(conn, provider_id) if cat is None else cat
    if not cat:
        return []
    pref = conn.execute("SELECT ext_id, pres_id FROM prefs WHERE provider_id = ? AND key = ?", (provider_id, key)).fetchone()
    if pref and pref["ext_id"] in cat:
        ext_id, pres_id = pref["ext_id"], pref["pres_id"]
    else:
        found = candidates(cat, key)
        if not found:
            return []
        ext_id, pres_id = found[0], None
    grams, units = line_amount(text)
    return [line(r, n) for r, n in pieces(cat[ext_id], grams, units, pres_id)]


def remember(conn, provider_id: int, key: str, ext_id: str, pres_id: str):
    """The house chose this product (and presentation) for this thing: next time too."""
    if key:
        conn.execute("INSERT INTO prefs (provider_id, key, ext_id, pres_id) VALUES (?, ?, ?, ?)"
                     " ON CONFLICT (provider_id, key) DO UPDATE SET ext_id = excluded.ext_id, pres_id = excluded.pres_id",
                     (provider_id, key, ext_id, pres_id))
        conn.commit()
