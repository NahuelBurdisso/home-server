"""Saving imported rows and deciding what each movement means."""
import hashlib
import re
import sqlite3
from collections import Counter
from datetime import date

from . import categories, people, reports
from .importers import ImportError_, Row, norm
from .money import shift_month

# Checked in order against the description (lowercase, no accents). Anything else is an
# income or an expense by its sign.
RULES = [
    ("card_payment", r"\bpago (de )?(tu |su )?(tarjeta|resumen|visa|master|amex)|\bpago tc\b|su pago en (pesos|usd|dolares)"),
    ("usd_buy", r"compra (de )?(dolares|u\$s|usd|moneda extranjera|me\b)|compra dolar"),
    ("expense", r"devolucion|reintegro|contracargo|anulacion"),  # money back lowers spending
    # Mercado Pago: your savings pocket, and money loaded from an account of yours.
    ("internal", r"^(reserva (por gastos|programada)|dinero retirado) ahorro|^ingreso de dinero\b"),
]

INTERNAL_ACCOUNTS = ("bbva", "mercadopago")  # transfers between these cancel out
MATCH_DAYS = 3
# Already 'internal' by a rule but still worth pairing, so the other bank's side stops
# counting as an expense (savings-pocket moves never leave the account, so they're left out).
TRANSFER_HINT = re.compile(r"^ingreso de dinero|transferencia|\btransf\b|debin")


# Pesos <-> dollars at the bank: savings changing currency, neither income nor spending.
FX = re.compile(r"cambio de moneda extranjera|venta (de )?(dolares|u\$s|usd)")


def auto_kind(description: str, amount: int) -> str:
    text = norm(description)
    if FX.search(text):
        return "usd_sell" if amount > 0 else "usd_buy"
    for kind, pattern in RULES:
        if re.search(pattern, text):
            return kind
    return "income" if amount > 0 else "expense"


def _statement_windows(conn: sqlite3.Connection, account: str) -> list[tuple]:
    return conn.execute("SELECT prev_close, close FROM statements WHERE account = ? AND prev_close IS NOT NULL",
                        (account,)).fetchall()


def store(conn: sqlite3.Connection, filename: str, account: str, rows: list[Row]) -> dict:
    """Saves new rows, skipping ones already imported from an earlier (overlapping) file.
    A card statement (PDF) replaces what other files loaded for its period; other card
    files skip periods a statement already covers."""
    statement = rows if getattr(rows, "close", None) else None
    replaced = 0
    if statement:
        if conn.execute("SELECT 1 FROM statements WHERE account = ? AND close = ?",
                        (account, statement.close)).fetchone():
            return {"account": account, "new": 0, "dup": len(rows), "internal": 0, "replaced": 0,
                    "statement": statement.close}
    else:
        windows = _statement_windows(conn, account)

        def covered(r) -> bool:
            # An installment ('cuota 3/6') keeps its purchase date but belongs to the month it's
            # charged: a statement covers it when that statement closes in that month.
            if r.month and r.month != r.date.isoformat()[:7]:
                return any(hi[:7] == r.month for _, hi in windows)
            return any(lo < r.date.isoformat() <= hi for lo, hi in windows)
        if account == "mercadopago" and not any(r.key for r in rows):
            # A report downloaded by hand: the days Mercado Pago's API already brought are skipped
            # (same movements, other wording). See mpsync.py.
            lo, hi = conn.execute("SELECT MIN(date), MAX(date) FROM transactions WHERE dedupe_key LIKE 'mp:%'").fetchone()

            def covered(r) -> bool:
                return bool(lo) and lo <= r.date.isoformat() <= hi
        kept = [r for r in rows if not covered(r)]
        skipped = len(rows) - len(kept)
        rows = kept
    if any(r.usd for r in rows):
        reports.refresh_rates(conn)
    for row in rows:
        if row.usd and not row.amount:
            rate = reports.rate_on(conn, "oficial", row.date.isoformat())
            if rate is None:
                raise ImportError_("Hay consumos en dólares y no pude bajar la cotización oficial. Probá más tarde.")
            row.amount = round(row.usd * rate)

    seen = Counter()
    fresh = []
    for row in rows:
        # Keyed on what the file says (not the converted pesos), so re-imports always match.
        base = f"{account}|{row.date}|{row.amount if not row.usd else ''}|{row.usd or ''}|{norm(row.description)}"
        seen[base] += 1  # identical rows in one file are distinct movements
        key = row.key or hashlib.sha1(f"{base}#{seen[base]}".encode()).hexdigest()
        fresh.append((row, key))

    cur = conn.execute("INSERT INTO imports (filename, account, rows_new, rows_dup) VALUES (?, ?, 0, 0)",
                       (filename, account))
    import_id = cur.lastrowid
    gone = []  # the rows the statement replaces, so what you chose on them goes on (_carry_over)
    if statement:
        if statement.prev_close:
            # Its period's purchases by date, and the installments charged in it by month (their date
            # is the original purchase's, months earlier).
            window = ("account = ? AND ((date > ? AND date <= ?) OR (month = ? AND month != substr(date, 1, 7)"
                      " AND date <= ?)) AND import_id NOT IN (SELECT import_id FROM statements WHERE import_id IS NOT NULL)")
            args = (account, statement.prev_close, statement.close, statement.close[:7], statement.prev_close)
            gone = conn.execute(f"SELECT * FROM transactions WHERE {window} ORDER BY date, id", args).fetchall()
            replaced = conn.execute(f"DELETE FROM transactions WHERE {window}", args).rowcount
        conn.execute("INSERT INTO statements (account, close, prev_close, due, next_close, total, total_usd,"
                     " minimum, import_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (account, statement.close, statement.prev_close, statement.due, statement.next_close,
                      statement.total, statement.total_usd, statement.minimum, import_id))
    new = 0
    added = []
    for row, key in fresh:
        cur = conn.execute(
            "INSERT OR IGNORE INTO transactions"
            " (account, date, month, description, amount, amount_usd, kind, import_id, dedupe_key, who)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (account, row.date.isoformat(), row.month or row.date.isoformat()[:7], row.description,
             row.amount, row.usd, auto_kind(row.description, row.amount), import_id, key, people.HOLDER))
        new += cur.rowcount
        if cur.rowcount:
            added.append((cur.lastrowid, row))
    if gone:
        _carry_over(conn, gone, added)
    conn.execute("UPDATE imports SET rows_new = ?, rows_dup = ? WHERE id = ?", (new, len(rows) - new, import_id))
    internal = match_internal(conn)
    categorize(conn)
    conn.commit()
    res = {"account": account, "new": new, "dup": len(rows) - new, "internal": internal, "replaced": replaced}
    if statement:
        res["statement"] = statement.close
    elif skipped:
        res["dup"] += skipped  # already in a card statement
    return res


def _charge(day: str, amount: int | None, usd: int | None) -> tuple:
    """What identifies a charge in both the Excel and the PDF: the day and the amount (in dollars
    when it was charged in dollars: the pesos depend on the rate). The wording differs between them."""
    return (day, "usd", usd) if usd else (day, "ars", amount)


def _carry_over(conn: sqlite3.Connection, gone: list, added: list[tuple[int, Row]]) -> None:
    """A statement replaces what the Excel loaded for its period; what you chose on those movements
    (whose it is, a kind you changed, a category you picked or the AI suggested) goes on to the same
    charge in the statement. With several equal charges, the one worded alike first."""
    pool = {}
    for old in gone:
        pool.setdefault(_charge(old["date"], old["amount"], old["amount_usd"]), []).append(old)
    for tx_id, row in added:
        candidates = pool.get(_charge(row.date.isoformat(), row.amount, row.usd))
        if not candidates:
            continue
        first = norm(row.description).split()[:1]
        old = next((o for o in candidates if norm(o["description"]).split()[:1] == first), candidates[0])
        candidates.remove(old)
        sets, args = ["who = ?"], [old["who"] or people.HOLDER]
        if old["kind_source"] == "manual":
            sets += ["kind = ?", "kind_source = 'manual'"]
            args.append(old["kind"])
        if old["category_source"] in ("user", "ai", "ai_sure"):  # rules and what you taught come back by themselves
            sets += ["category = ?", "category_source = ?", "category_conf = ?"]
            args += [old["category"], old["category_source"], old["category_conf"]]
        conn.execute(f"UPDATE transactions SET {', '.join(sets)} WHERE id = ?", (*args, tx_id))


def match_internal(conn: sqlite3.Connection) -> int:
    """Pairs money leaving one of your accounts with the same amount arriving in another
    within a few days, and marks both as 'between my accounts'. Returns pairs found."""
    rows = [r for r in conn.execute(
        "SELECT id, account, date, amount, kind, description FROM transactions"
        " WHERE matched_id IS NULL AND kind_source = 'auto' AND kind IN ('income', 'expense', 'internal')"
        f" AND account IN ({','.join('?' * len(INTERNAL_ACCOUNTS))}) ORDER BY date, id",
        INTERNAL_ACCOUNTS)
        if r["kind"] != "internal" or TRANSFER_HINT.search(norm(r["description"]))]
    used = set()
    pairs = 0
    for out in rows:
        if out["amount"] >= 0 or out["id"] in used:
            continue
        day = date.fromisoformat(out["date"])
        for inn in rows:
            if (inn["id"] in used or inn["account"] == out["account"] or inn["amount"] != -out["amount"]
                    or abs((date.fromisoformat(inn["date"]) - day).days) > MATCH_DAYS
                    or out["kind"] == inn["kind"] == "internal"):
                continue
            used |= {out["id"], inn["id"]}
            conn.execute("UPDATE transactions SET kind = 'internal', matched_id = ? WHERE id = ?", (inn["id"], out["id"]))
            conn.execute("UPDATE transactions SET kind = 'internal', matched_id = ? WHERE id = ?", (out["id"], inn["id"]))
            pairs += 1
            break
    return pairs


def set_kind(conn: sqlite3.Connection, tx_id: int, kind: str) -> None:
    """A manual change wins over the automatic one. Undoing half of a matched transfer
    frees the other half too."""
    row = conn.execute("SELECT matched_id FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    if row is None:
        return
    if row["matched_id"] and kind != "internal":
        other = conn.execute("SELECT amount, description FROM transactions WHERE id = ?", (row["matched_id"],)).fetchone()
        conn.execute("UPDATE transactions SET matched_id = NULL, kind = ? WHERE id = ? AND kind_source = 'auto'",
                     (auto_kind(other["description"], other["amount"]), row["matched_id"]))
    conn.execute("UPDATE transactions SET kind = ?, kind_source = 'manual', matched_id = NULL WHERE id = ?",
                 (kind, tx_id))
    categorize(conn)
    conn.commit()


def apply_recurring(conn: sqlite3.Connection, until: str | None = None) -> None:
    """Creates (or re-values) one transaction per month for each dollar item, up to this month.
    The current month follows today's MEP; a kind you changed by hand is kept."""
    until = until or date.today().isoformat()[:7]
    reports.refresh_rates(conn)
    for rec in conn.execute("SELECT * FROM recurring").fetchall():
        skip = {r[0] for r in conn.execute("SELECT month FROM recurring_skip WHERE recurring_id = ?", (rec["id"],))}
        month = rec["start_month"]
        while month <= until:
            mep = reports.mep_for(conn, month)
            if month not in skip and mep:
                conn.execute(
                    "INSERT INTO transactions (account, date, month, description, amount, amount_usd, kind, dedupe_key, who)"
                    " VALUES ('dolares', ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(dedupe_key) DO UPDATE SET amount = excluded.amount,"
                    " amount_usd = excluded.amount_usd, description = excluded.description",
                    (f"{month}-01", month, rec["description"], round(rec["amount_usd"] * mep), rec["amount_usd"],
                     rec["kind"], f"recurring:{rec['id']}:{month}", people.HOLDER))
            month = shift_month(month, 1)
    categorize(conn)
    conn.commit()


def categorize(conn: sqlite3.Connection) -> None:
    """Gives every expense a category: what you taught it first, then the starter rules.
    Your one-off choices ('user') are never touched; an AI guess stays until a rule covers it.
    Whatever is left without a category waits for the classifier (see classifier.py)."""
    for row in conn.execute("SELECT id, description, merchant FROM transactions").fetchall():
        key = categories.merchant_key(row["description"])
        if key != row["merchant"]:  # new rows, or rows saved before merchant_key() changed
            conn.execute("UPDATE transactions SET merchant = ? WHERE id = ?", (key, row["id"]))
    learned = dict(conn.execute("SELECT merchant, category FROM rules").fetchall())
    rows = conn.execute(
        "SELECT id, description, merchant, category, category_source FROM transactions"
        " WHERE kind = 'expense' AND (category_source IS NULL OR category_source != 'user')").fetchall()
    for row in rows:
        if row["merchant"] in learned:
            new = (learned[row["merchant"]], "learned")
        elif (seed := categories.seed_category(row["description"])):
            new = (seed, "rule")
        elif row["category_source"] in ("ai", "ai_sure"):
            continue
        else:
            new = (None, None)
        if (row["category"], row["category_source"]) != new:
            conn.execute("UPDATE transactions SET category = ?, category_source = ?, category_conf = NULL WHERE id = ?",
                         (*new, row["id"]))


def set_category(conn: sqlite3.Connection, tx_id: int, category: str, learn: bool = True) -> int:
    """Your choice for one movement. With learn=True it becomes the rule for that merchant,
    so the others (and future imports) follow. Returns how many movements changed."""
    if category not in categories.CATEGORIES:
        return 0
    row = conn.execute("SELECT merchant FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    if row is None:
        return 0
    if learn and row["merchant"]:
        conn.execute("INSERT INTO rules (merchant, category) VALUES (?, ?)"
                     " ON CONFLICT(merchant) DO UPDATE SET category = excluded.category", (row["merchant"], category))
        # Your earlier one-off choices for this merchant give way to the new rule.
        conn.execute("UPDATE transactions SET category_source = NULL WHERE merchant = ? AND category_source = 'user'",
                     (row["merchant"],))
        before = conn.total_changes
        categorize(conn)
        changed = conn.total_changes - before
    else:
        conn.execute("UPDATE transactions SET category = ?, category_source = 'user', category_conf = NULL"
                     " WHERE id = ?", (category, tx_id))
        changed = 1
    conn.commit()
    return changed


def pending_review(conn: sqlite3.Connection, month: str | None = None) -> list:
    """Expenses with no category yet, or only an AI guess you haven't confirmed."""
    sql = ("SELECT * FROM transactions WHERE kind = 'expense' AND amount < 0"
           " AND (category IS NULL OR category_source = 'ai')")
    args = []
    if month:
        sql += " AND month = ?"
        args.append(month)
    return conn.execute(sql + " ORDER BY amount", args).fetchall()


def add_recurring(conn: sqlite3.Connection, description: str, amount_usd: int, kind: str, start_month: str) -> None:
    signed = abs(amount_usd) if kind == "income" else -abs(amount_usd)
    conn.execute("INSERT INTO recurring (description, amount_usd, kind, start_month) VALUES (?, ?, ?, ?)",
                 (description, signed, kind, start_month))
    apply_recurring(conn)


def delete_recurring(conn: sqlite3.Connection, rec_id: int) -> None:
    conn.execute("DELETE FROM transactions WHERE dedupe_key LIKE ?", (f"recurring:{rec_id}:%",))
    conn.execute("DELETE FROM recurring WHERE id = ?", (rec_id,))
    conn.commit()


def delete_manual(conn: sqlite3.Connection, tx_id: int) -> None:
    """Removes something entered by hand. For a monthly dollar item, only that month goes."""
    row = conn.execute("SELECT dedupe_key FROM transactions WHERE id = ? AND import_id IS NULL", (tx_id,)).fetchone()
    if row is None:
        return
    if (row["dedupe_key"] or "").startswith("recurring:"):
        _, rec_id, month = row["dedupe_key"].split(":")
        conn.execute("INSERT OR IGNORE INTO recurring_skip (recurring_id, month) VALUES (?, ?)", (int(rec_id), month))
    conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
    conn.commit()


def add_manual(conn: sqlite3.Connection, when: date, description: str, amount: int, kind: str,
               account: str = "efectivo", who: str = people.HOLDER) -> int:
    """Returns the new movement's id (not MAX(id): someone else may write at the same time)."""
    cur = conn.execute(
        "INSERT INTO transactions (account, date, month, description, amount, kind, kind_source, who)"
        " VALUES (?, ?, ?, ?, ?, ?, 'manual', ?)",
        (account, when.isoformat(), when.isoformat()[:7], description, amount, kind, people.valid(who) or people.HOLDER))
    conn.commit()
    return cur.lastrowid


def set_who(conn: sqlite3.Connection, tx_id: int, who: str) -> None:
    """Whose movement it is: Nahuel, Sofía or both. Re-imports keep it (rows are matched, not replaced;
    a card statement that replaces the Excel's rows carries it over, see _carry_over)."""
    if people.valid(who):
        conn.execute("UPDATE transactions SET who = ? WHERE id = ?", (who, tx_id))
        conn.commit()
