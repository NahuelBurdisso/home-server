"""Monthly numbers for the screens, and the MEP dollar to show them in USD."""
import json
import sqlite3
import urllib.request
from datetime import date, datetime

from .db import ACCOUNTS
from .people import PEOPLE

RATES_URL = "https://api.argentinadatos.com/v1/cotizaciones/dolares/{}"
RATE_KINDS = ("bolsa", "oficial")  # MEP to show savings in dollars; oficial for card charges
INFLATION_URL = "https://api.argentinadatos.com/v1/finanzas/indices/inflacion"  # INDEC's monthly CPI


def months(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT month FROM transactions ORDER BY month")]


def _whose(who: str | None) -> tuple[str, list]:
    """The SQL to keep only one person's movements (nothing for everybody's)."""
    return (" AND who = ?", [who]) if who else ("", [])


def summary(conn: sqlite3.Connection, month: str, who: str | None = None) -> dict:
    """The month's numbers; with who, only that person's movements (Nahuel, Sofía or both)."""
    w, wa = _whose(who)
    totals = dict(conn.execute(
        f"SELECT kind, SUM(amount) FROM transactions WHERE month = ?{w} GROUP BY kind", (month, *wa)).fetchall())
    income = totals.get("income", 0)
    spent = -totals.get("expense", 0)
    by_account = [
        {"account": ACCOUNTS.get(r["account"], r["account"]), "spent": -r["total"]}
        for r in conn.execute(
            f"SELECT account, SUM(amount) AS total FROM transactions WHERE month = ? AND kind = 'expense'{w}"
            " GROUP BY account HAVING total < 0 ORDER BY total", (month, *wa))
    ]
    by_person = [
        {"who": r["who"], "label": PEOPLE.get(r["who"], r["who"]), "spent": -r["total"]}
        for r in conn.execute(
            "SELECT who, SUM(amount) AS total FROM transactions WHERE month = ? AND kind = 'expense'"
            " GROUP BY who HAVING total < 0 ORDER BY total", (month,))
    ]
    biggest = conn.execute(
        f"SELECT * FROM transactions WHERE month = ? AND kind = 'expense' AND amount < 0{w}"
        " ORDER BY amount LIMIT 8", (month, *wa)).fetchall()
    present = {r[0] for r in conn.execute("SELECT DISTINCT account FROM transactions WHERE month = ?", (month,))}
    excluded = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE month = ? AND kind IN ('internal', 'card_payment')",
        (month,)).fetchone()[0]
    return {
        "month": month,
        "income": income,
        "spent": spent,
        "left": income - spent,
        "usd_bought": -totals.get("usd_buy", 0),
        "usd_sold": totals.get("usd_sell", 0),
        "by_account": by_account,
        "by_person": by_person,  # always everybody's, to compare
        "biggest": biggest,
        "excluded": excluded,
        # The accounts that make a month complete; cash is optional.
        "missing": [ACCOUNTS[a] for a in ("bbva", "bbva_tarjeta", "mercadopago") if a not in present],
    }


def series(conn: sqlite3.Connection, last: int = 12) -> list[dict]:
    return [summary(conn, m) for m in months(conn)[-last:]]


def refresh_rates(conn: sqlite3.Connection) -> None:
    """Downloads the dollar history at most once a day. Failing is fine: the last copy is used."""
    today = date.today().isoformat()
    row = conn.execute("SELECT value FROM meta WHERE key = 'rates_checked'").fetchone()
    if row and row[0] == today:
        return
    try:
        fetched = []
        for kind in RATE_KINDS:
            # The API refuses Python's default User-Agent (403).
            req = urllib.request.Request(RATES_URL.format(kind), headers={"User-Agent": "finanzas/1.0 (self-hosted)"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.load(resp)
            fetched += [(d["fecha"], kind, d["venta"]) for d in data if d.get("venta")]
        # Written once both arrived: waiting on the network with the write lock taken would make
        # everyone else's writes (Siri, the Gmail, Mercado Pago) fail with 'database is locked'.
        conn.executemany("INSERT OR REPLACE INTO rates (date, kind, value) VALUES (?, ?, ?)", fetched)
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('rates_checked', ?)", (today,))
        conn.commit()
    except Exception:
        conn.rollback()
    # The CPI apart: if it fails, the dollar rates are still saved, and it's tried again tomorrow.
    try:
        req = urllib.request.Request(INFLATION_URL, headers={"User-Agent": "finanzas/1.0 (self-hosted)"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            conn.executemany("INSERT OR REPLACE INTO inflation (month, pct) VALUES (?, ?)",
                             [(d["fecha"][:7], d["valor"]) for d in json.load(resp) if d.get("valor") is not None])
        conn.commit()
    except Exception:
        conn.rollback()


def rate_on(conn: sqlite3.Connection, kind: str, day: str) -> float | None:
    """The price on that day, or the last one before it (weekends, holidays)."""
    row = conn.execute("SELECT value FROM rates WHERE kind = ? AND date <= ? ORDER BY date DESC LIMIT 1",
                       (kind, day)).fetchone()
    return row[0] if row else None


def mep_for(conn: sqlite3.Connection, month: str) -> float | None:
    """The MEP on the last day with a price in that month (today's for the current month)."""
    return rate_on(conn, "bolsa", f"{month}-31")


def inflation_factors(conn: sqlite3.Connection, months_: list[str]) -> tuple[dict, str | None]:
    """How many pesos of the last month INDEC published each month's peso is worth: {month: factor},
    and that month. The CPI is a monthly average, so a month's prices are its own index: its factor
    is the inflation of the months after it, up to the last one (which is 1.0, as is any later
    month). A month before the series starts gets None."""
    rows = conn.execute("SELECT month, pct FROM inflation ORDER BY month").fetchall()
    if not rows:
        return {}, None
    pct = {m: p for m, p in rows}
    last = rows[-1][0]
    out = {}
    for m in months_:
        if m >= last:
            out[m] = 1.0
        elif m not in pct:
            out[m] = None
        else:
            f = 1.0
            for k, p in rows:
                if k > m:
                    f *= 1 + p / 100
            out[m] = f
    return out, last


def budgets_for(conn: sqlite3.Connection, month: str) -> dict[str, int]:
    """{group: limit in centavos} for a month: each one as set, grown (or shrunk) by the inflation
    between the month it was set and this one."""
    rows = conn.execute("SELECT grp, amount, base_month FROM budgets").fetchall()
    if not rows:
        return {}
    factors, _ = inflation_factors(conn, [month] + [r["base_month"] for r in rows])
    out = {}
    for r in rows:
        fb, fm = factors.get(r["base_month"]), factors.get(month)
        out[r["grp"]] = round(r["amount"] * fb / fm) if fb and fm else r["amount"]
    return out


def budget_status(conn: sqlite3.Connection, month: str) -> list[dict]:
    """Every group with a limit: spent, limit, % used, worst first."""
    from . import categories
    limits = budgets_for(conn, month)
    if not limits:
        return []
    spent = {it["id"]: it["value"] for it in spending_view(conn, month, 1, "group", None)["items"]}
    out = []
    for g, limit in limits.items():
        info = categories.GROUP.get(g)
        if not info or limit <= 0:
            continue
        s = spent.get(g, 0)
        out.append({"id": g, "emoji": info["emoji"], "label": info["label"], "limit": limit, "spent": s,
                    "pct": round(s / limit * 100)})
    return sorted(out, key=lambda b: -b["pct"])


def set_budget(conn: sqlite3.Connection, group: str, amount: int | None, month: str) -> None:
    if amount:
        conn.execute("INSERT INTO budgets (grp, amount, base_month) VALUES (?, ?, ?) ON CONFLICT(grp) DO UPDATE"
                     " SET amount = excluded.amount, base_month = excluded.base_month", (group, amount, month))
    else:
        conn.execute("DELETE FROM budgets WHERE grp = ?", (group,))
    conn.commit()


def last_import(conn: sqlite3.Connection) -> dict:
    """When each account was last loaded, for the 'Cargar' screen."""
    out = {}
    for r in conn.execute("SELECT account, MAX(created_at) FROM imports GROUP BY account"):
        out[r[0]] = datetime.fromisoformat(r[1]).strftime("%d/%m")
    return out


# --- spending by category -------------------------------------------------------------

def _months_back(month: str, n: int) -> list[str]:
    from .money import shift_month
    return [shift_month(month, -i) for i in range(n - 1, -1, -1)]


def spending(conn: sqlite3.Connection, months: list[str], level: str = "group", within: str | None = None,
             who: str | None = None) -> list[dict]:
    """Spending per group, per subcategory ('sub', within a group) or per merchant ('merchant',
    within a group or a subcategory), biggest first. Refunds net out against their category."""
    from . import categories
    marks = ",".join("?" * len(months))
    w, wa = _whose(who)
    rows = conn.execute(
        f"SELECT COALESCE(category, ?) AS category, merchant, description, amount, date FROM transactions"
        f" WHERE kind = 'expense' AND month IN ({marks}){w} ORDER BY date DESC",
        [categories.UNKNOWN, *months, *wa]).fetchall()
    totals, names, counts = {}, {}, {}
    for r in rows:
        cat = r["category"]
        if within and not (cat == within or cat.startswith(within + ".")):
            continue
        key = {"group": categories.group_of(cat), "sub": cat}.get(level, r["merchant"] or r["description"])
        totals[key] = totals.get(key, 0) - r["amount"]
        counts[key] = counts.get(key, 0) + 1
        names.setdefault(key, r["description"])  # the most recent wording, for merchants
    out = []
    for key, total in totals.items():
        if total <= 0:
            continue
        if level == "group":
            g = categories.GROUP[key]
            item = {"id": key, "emoji": g["emoji"], "label": g["label"]}
        elif level == "sub":
            item = {"id": key, "emoji": categories.GROUP[categories.group_of(key)]["emoji"],
                    "label": categories.CATEGORIES.get(key, "Sin categoría")}
        else:
            item = {"id": key, "emoji": "", "label": names[key]}
        out.append({**item, "total": total, "count": counts[key]})
    return sorted(out, key=lambda x: -x["total"])


def spending_view(conn: sqlite3.Connection, month: str, span: int, level: str, within: str | None,
                  who: str | None = None) -> dict:
    """Totals for the chosen period (shown as a monthly average when it covers several months),
    each item's share, and for a single month the change against the average of the (up to 3)
    months before it that are complete: an incomplete month would make any change look huge."""
    period = _months_back(month, span)
    items = spending(conn, period, level, within, who)
    for it in items:
        it["value"] = it["total"] / span
        it["delta"] = None
    if span == 1:
        known = set(months(conn))
        before = [m for m in _months_back(month, 4)[:3] if m in known and not summary(conn, m)["missing"]]
        if before:
            avg = {it["id"]: it["total"] / len(before) for it in spending(conn, before, level, within, who)}
            for it in items:
                if avg.get(it["id"]):
                    it["delta"] = round((it["value"] - avg[it["id"]]) / avg[it["id"]] * 100)
    total = sum(it["value"] for it in items)
    top = max([it["value"] for it in items] + [1])
    for it in items:
        it["share"] = round(it["value"] / total * 100) if total else 0
        it["width"] = round(it["value"] / top * 100, 1)
    return {"items": items, "total": total, "months": period}


# --- what you have today --------------------------------------------------------------

BALANCE_ACCOUNTS = ("efectivo", "mercadopago", "bbva", "dolares")  # the card is a debt, not savings
CASH_OUT = "efectivo.retiros"


def balances(conn: sqlite3.Connection, today: str | None = None) -> dict:
    """What each account has today: what you counted on a day, plus every movement after it. Cash
    also grows with what you withdraw from the bank or Mercado Pago. Dollars stay as counted (a
    statement doesn't say how many dollars a peso purchase bought): count them again when they change.
    The total is in pesos, dollars at today's MEP."""
    today = today or date.today().isoformat()
    mep = rate_on(conn, "bolsa", today)
    anchors = {r["account"]: r for r in conn.execute("SELECT * FROM balances")}
    items = []
    for acc in BALANCE_ACCOUNTS:
        a = anchors.get(acc)
        if a is None:
            continue
        item = {"account": acc, "label": ACCOUNTS[acc], "since": a["date"], "start": a["amount"], "moved": 0}
        if acc == "dolares":
            item["usd"] = a["amount"]
            item["now"] = round(a["amount"] * mep) if mep else None
        else:
            item["moved"] = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE account = ? AND date > ?",
                                         (acc, a["date"])).fetchone()[0]
            if acc == "efectivo":
                item["moved"] -= conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE account != 'efectivo' AND category = ?"
                    " AND date > ?", (CASH_OUT, a["date"])).fetchone()[0]
            item["now"] = a["amount"] + item["moved"]
        items.append(item)
    total = sum(it["now"] or 0 for it in items)
    return {"accounts": items, "total": total, "total_usd": total / 100 / mep if mep else None, "mep": mep}


def set_balances(conn: sqlite3.Connection, amounts: dict[str, int], day: str) -> None:
    """What you counted on that day (pesos in centavos; dollars in US$ centavos)."""
    for acc, amount in amounts.items():
        if acc in BALANCE_ACCOUNTS:
            conn.execute("INSERT INTO balances (account, date, amount) VALUES (?, ?, ?) ON CONFLICT(account) DO UPDATE"
                         " SET date = excluded.date, amount = excluded.amount, set_at = datetime('now', 'localtime')",
                         (acc, day, amount))
    conn.commit()
