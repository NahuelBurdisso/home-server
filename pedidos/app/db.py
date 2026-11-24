"""SQLite storage. One file in /data, so the nightly restic backup picks it up (a VACUUM INTO copy
from setup/sqlite-dumps.sh). Open it only from inside the container (docker exec pedidos ...)."""
import os
import sqlite3

DB_PATH = os.environ.get("PEDIDOS_DB", "/data/pedidos.db")

# AUTOINCREMENT: an id is never handed out twice, so the bot's "borrar" (it keeps the order ids it
# got in X-Ids) can't cancel an order made later.
SCHEMA = """
-- A shop. kind: pedix (catalog read from its page), whatsapp (takes orders by message), web (its own
-- site and cart). sections: comma list of Lista's section ids whose things go to this shop.
CREATE TABLE IF NOT EXISTS providers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL,              -- pedix, whatsapp, web
    url           TEXT NOT NULL DEFAULT '',   -- pedix: https://pedix.app/<slug>; web: the shop's site
    phone         TEXT NOT NULL DEFAULT '',   -- digits, international: 5435...
    sections      TEXT NOT NULL DEFAULT '',
    min_purchase  REAL,
    note          TEXT NOT NULL DEFAULT '',
    active        INTEGER NOT NULL DEFAULT 1,
    catalog_at    TEXT,                       -- the last good read of the catalog
    catalog_error TEXT                        -- the last read failed: why (NULL when it worked)
);

-- The catalog: one row per product × presentation; a product without presentations is one row
-- with presentation '' and its own price. Inactive products and categories aren't stored.
CREATE TABLE IF NOT EXISTS products (
    provider_id  INTEGER NOT NULL,
    ext_id       TEXT NOT NULL,
    name         TEXT NOT NULL,
    key          TEXT NOT NULL,               -- folded name, for matching
    category     TEXT NOT NULL DEFAULT '',
    presentation TEXT NOT NULL DEFAULT '',
    pres_id      TEXT NOT NULL DEFAULT '',
    price        REAL,
    grams        REAL,                        -- what the presentation weighs, if it says
    units        REAL,                        -- or how many units it is (0.5: a half)
    active       INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (provider_id, ext_id, pres_id)
);

-- The house's choices: for this thing at this shop, this product and presentation.
CREATE TABLE IF NOT EXISTS prefs (
    provider_id INTEGER NOT NULL,
    key         TEXT NOT NULL,                -- the thing as Lista keys it: "papas"
    ext_id      TEXT NOT NULL,
    pres_id     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (provider_id, key)
);

CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'borrador',  -- borrador, enviado, recibido, cancelado, guardado
    name        TEXT NOT NULL DEFAULT '',          -- a guardado's name ("Lo de siempre"); '' for the rest
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    sent_at     TEXT,
    received_at TEXT,
    total_est   REAL,
    note        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS orders_status ON orders (status, provider_id);

-- What goes in an order. ext_id NULL: not in the catalog, it goes as written.
CREATE TABLE IF NOT EXISTS lines (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     INTEGER NOT NULL,
    text         TEXT NOT NULL,               -- as on the list: "2 kg de papas"
    key          TEXT NOT NULL DEFAULT '',    -- what it is: "papas" (for prefs)
    lista_id     INTEGER,                     -- the Lista item it came from; NULL when typed here
    ext_id       TEXT,
    pres_id      TEXT NOT NULL DEFAULT '',
    product      TEXT NOT NULL DEFAULT '',
    presentation TEXT NOT NULL DEFAULT '',
    qty          INTEGER NOT NULL DEFAULT 1,
    unit_price   REAL
);
CREATE INDEX IF NOT EXISTS lines_order ON lines (order_id);

-- direccion, nombre, aclaracion, telefono, pago, referencia (the Ajustes page); seeded (the version of SEED applied).
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# The house's shops at first start (decision 3).
SEED = [
    ("Verdufresca", "pedix", "https://pedix.app/verdufresca", "", "verduleria"),
    ("Carnes del Centro", "whatsapp", "", "5493515550123", "carniceria"),
    ("Almacén del Centro", "whatsapp", "", "5493515550789",
     "lacteos,panaderia,almacen,bebidas,limpieza,higiene,mascotas,otros"),
]
SEED_VERSION = "3"
# Earlier seeds had placeholders for shops not known yet; they become the real ones, unless edited.
PLACEHOLDERS = {"Carnicería": SEED[1], "Súper": SEED[2]}


def _seed(conn):
    """Once per database (meta 'seeded'), so deleting every shop doesn't bring them back."""
    seeded = get_meta(conn, "seeded")
    if seeded == SEED_VERSION:
        return
    if seeded is None:
        for name, kind, url, phone, sections in SEED:
            conn.execute("INSERT INTO providers (name, kind, url, phone, sections) VALUES (?, ?, ?, ?, ?)",
                         (name, kind, url, phone, sections))
    else:
        for old, (name, kind, url, phone, _) in PLACEHOLDERS.items():
            conn.execute("UPDATE providers SET name = ?, kind = ?, url = ?, phone = ?"
                         " WHERE name = ? AND url = '' AND phone = ''", (name, kind, url, phone, old))
    set_meta(conn, "seeded", SEED_VERSION)


def _migrate(conn):
    """Columns added after a database was created (CREATE TABLE IF NOT EXISTS leaves old tables as they are)."""
    if "name" not in {r["name"] for r in conn.execute("PRAGMA table_info(orders)")}:
        conn.execute("ALTER TABLE orders ADD COLUMN name TEXT NOT NULL DEFAULT ''")
        conn.commit()


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass  # another request is switching it right now (a new file): WAL sticks to the file
    conn.executescript(SCHEMA)
    _migrate(conn)
    _seed(conn)
    return conn


def get_meta(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn, key: str, value: str | None):
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                 (key, value))
    conn.commit()
