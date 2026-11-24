"""SQLite storage. One file in /data, so the nightly restic backup picks it up."""
import os
import sqlite3

DB_PATH = os.environ.get("FINANZAS_DB", "/data/finanzas.db")

ACCOUNTS = {
    "bbva": "BBVA cuenta",
    "bbva_tarjeta": "BBVA tarjeta",
    "mercadopago": "Mercado Pago",
    "efectivo": "Efectivo",
    "dolares": "Dólares",
}

# What a movement means for "how much was left this month".
KINDS = {
    "income": "Ingreso",
    "expense": "Gasto",
    "internal": "Entre mis cuentas",
    "card_payment": "Pago de tarjeta",
    "usd_buy": "Compra de dólares",
    "usd_sell": "Venta de dólares",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
    id          INTEGER PRIMARY KEY,
    filename    TEXT NOT NULL,
    account     TEXT NOT NULL,
    rows_new    INTEGER NOT NULL,
    rows_dup    INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY,
    account     TEXT NOT NULL,
    date        TEXT NOT NULL,              -- YYYY-MM-DD, when it happened
    month       TEXT NOT NULL,              -- YYYY-MM it counts for (a card statement's month)
    description TEXT NOT NULL,
    amount      INTEGER NOT NULL,           -- centavos; negative = money out
    amount_usd  INTEGER,                    -- US$ centavos when charged in dollars (amount is its value in pesos)
    kind        TEXT NOT NULL,
    kind_source TEXT NOT NULL DEFAULT 'auto',  -- 'manual' once you change it by hand
    matched_id  INTEGER,                    -- the other half of a transfer between your accounts
    import_id   INTEGER REFERENCES imports(id),
    dedupe_key  TEXT UNIQUE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS transactions_month ON transactions(month);

CREATE TABLE IF NOT EXISTS rates (
    date  TEXT NOT NULL,                    -- YYYY-MM-DD
    kind  TEXT NOT NULL,                    -- 'bolsa' (MEP) or 'oficial'
    value REAL NOT NULL,                    -- pesos per dollar, sell price
    PRIMARY KEY (date, kind)
);

-- Same amount in dollars every month (income paid in USD, rent...). Each month becomes a
-- transaction valued at that month's MEP; deleting one month only skips that month.
CREATE TABLE IF NOT EXISTS recurring (
    id          INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    amount_usd  INTEGER NOT NULL,           -- US$ centavos; negative = money out
    kind        TEXT NOT NULL,              -- 'income' or 'expense'
    start_month TEXT NOT NULL               -- YYYY-MM
);
CREATE TABLE IF NOT EXISTS recurring_skip (
    recurring_id INTEGER NOT NULL REFERENCES recurring(id) ON DELETE CASCADE,
    month        TEXT NOT NULL,
    PRIMARY KEY (recurring_id, month)
);

-- Card statements loaded from the PDF: each one owns the movements between the previous
-- close (exclusive) and its close, so the same period never counts twice.
CREATE TABLE IF NOT EXISTS statements (
    id          INTEGER PRIMARY KEY,
    account     TEXT NOT NULL,
    close       TEXT NOT NULL,              -- YYYY-MM-DD
    prev_close  TEXT,
    due         TEXT,
    next_close  TEXT,
    import_id   INTEGER REFERENCES imports(id),
    UNIQUE (account, close)
);

-- BBVA's mail saying the statement is ready (app/aviso.py): due date and amounts, days before the PDF.
CREATE TABLE IF NOT EXISTS card_notices (
    account   TEXT NOT NULL,
    due       TEXT NOT NULL,                -- YYYY-MM-DD
    close     TEXT,                         -- the close the last PDF announced, when it matches
    total     INTEGER,                      -- centavos
    total_usd INTEGER,                      -- US$ centavos
    minimum   INTEGER,                      -- centavos
    received  TEXT NOT NULL,                -- YYYY-MM-DD
    PRIMARY KEY (account, due)
);

-- What you taught the app: every movement from this merchant goes to this category.
CREATE TABLE IF NOT EXISTS rules (
    merchant    TEXT PRIMARY KEY,           -- categories.merchant_key()
    category    TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- A monthly spending limit per group, in the pesos of the month it was set: it follows inflation
-- by itself (reports.budgets_for).
CREATE TABLE IF NOT EXISTS budgets (
    grp        TEXT PRIMARY KEY,           -- a categories.GROUPS id
    amount     INTEGER NOT NULL,           -- centavos
    base_month TEXT NOT NULL               -- YYYY-MM it was set in
);

-- Monthly inflation (INDEC's CPI, % against the month before), to show amounts in today's pesos.
CREATE TABLE IF NOT EXISTS inflation (
    month TEXT PRIMARY KEY,                 -- YYYY-MM
    pct   REAL NOT NULL
);

-- What each account had on a day you counted it. From then on the movements after that day move
-- it (reports.balances); counting again replaces it.
CREATE TABLE IF NOT EXISTS balances (
    account TEXT PRIMARY KEY,               -- a key of ACCOUNTS (not the card)
    date    TEXT NOT NULL,                  -- YYYY-MM-DD; that day's movements are already in it
    amount  INTEGER NOT NULL,               -- centavos; US$ centavos for 'dolares'
    set_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- What the house pays every month: power, gas, water, the city tax, the house itself (app/pagos.py).
CREATE TABLE IF NOT EXISTS bills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,  -- never reused: reminders link to /pagos/<id>
    name            TEXT NOT NULL,              -- 'Luz (EPEC)'
    emoji           TEXT NOT NULL DEFAULT '🧾',
    details         TEXT NOT NULL DEFAULT '',   -- what the payment page asks for, one per line ('Cliente: 123')
    url             TEXT NOT NULL DEFAULT '',   -- where it's paid
    notes           TEXT NOT NULL DEFAULT '',   -- anything else worth remembering
    day             INTEGER NOT NULL,           -- day of the month it's due / to be reminded (1..28)
    match           TEXT NOT NULL DEFAULT '',   -- comma separated; an expense whose description has one pays it
    amount_usd      INTEGER,                    -- fixed amount in dollars (US$ centavos); NULL: pesos, whatever the bill says
    total_usd       INTEGER,                    -- paying something off: its whole price (US$ centavos)
    paid_before_usd INTEGER NOT NULL DEFAULT 0, -- ...and what was paid outside these monthly payments
    start_month     TEXT NOT NULL,              -- YYYY-MM, the first month it's due
    active          INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS bill_months (
    bill_id         INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    month           TEXT NOT NULL,              -- YYYY-MM
    paid_on         TEXT,                       -- YYYY-MM-DD it was marked paid by hand; NULL: not marked
    paid_via        TEXT,                       -- 'manual' (page, bot, Siri) or 'recordatorio' (its "Hecho")
    amount          INTEGER,                    -- what you said it was (centavos; US$ centavos for a dollar one)
    reminder_id     INTEGER,                    -- its reminder in Recordatorios
    reminder_closed INTEGER NOT NULL DEFAULT 0, -- that reminder was already told it's done
    PRIMARY KEY (bill_id, month)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# Columns added after the first version; created on databases that don't have them yet.
ADDED_COLUMNS = {
    "transactions": [
        ("merchant", "TEXT"),           # categories.merchant_key(description)
        ("category", "TEXT"),           # 'group.sub', expenses only
        ("category_source", "TEXT"),    # 'rule' (starter), 'learned', 'user' (this one only), 'ai', 'ai_sure'
        ("category_conf", "REAL"),      # the classifier's confidence, 0..1
        ("who", "TEXT"),                # a people.PEOPLE key: whose movement it is
    ],
    "statements": [
        ("total", "INTEGER"),           # SALDO ACTUAL $, centavos
        ("total_usd", "INTEGER"),       # SALDO ACTUAL U$S, US$ centavos
        ("minimum", "INTEGER"),         # PAGO MÍNIMO $, centavos
    ],
}


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")  # the background classifier writes too
    conn.executescript(SCHEMA)
    for table, columns in ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, sql_type in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
                if (table, name) == ("transactions", "who"):
                    # Everything before there were two people was the holder's.
                    from .people import HOLDER
                    conn.execute("UPDATE transactions SET who = ?", (HOLDER,))
                    conn.commit()
    return conn
