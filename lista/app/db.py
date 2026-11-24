"""SQLite storage. One file in /data, so the nightly restic backup picks it up (a VACUUM INTO copy
from setup/sqlite-dumps.sh). Open it only from inside the container (docker exec lista ...)."""
import os
import sqlite3

DB_PATH = os.environ.get("LISTA_DB", "/data/lista.db")

# AUTOINCREMENT: an id is never handed out twice, so the Telegram bot's "borrar" (it keeps the ids it
# added) can't take away something added later in the app.
ITEMS = """
CREATE TABLE IF NOT EXISTS {name} (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,              -- as written: "2 kg de papas"
    key        TEXT NOT NULL,              -- what it is, folded, without amounts: "papas"
    section    TEXT NOT NULL,              -- verduleria, almacen, ... (parse.SECTIONS)
    added_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    done_at    TEXT,
    cleared_at TEXT
);
"""
COLUMNS = "id, text, key, section, added_at, done_at, cleared_at"

SCHEMA = """
-- One thing to buy. On the list while done_at is NULL; in the cart once ticked; history once
-- "terminé las compras" (cleared_at) archives the cart.
""" + ITEMS.format(name="items") + """
CREATE INDEX IF NOT EXISTS items_open ON items (cleared_at, done_at);
"""


def _needs_autoincrement(conn) -> bool:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'items'").fetchone()
    return row is not None and "AUTOINCREMENT" not in row[0].upper()


def _autoincrement(conn):
    """Databases made before AUTOINCREMENT: the items table is rebuilt once, in one transaction,
    keeping every id. If anything fails it rolls back and the app goes on with the old table."""
    if not _needs_autoincrement(conn):
        return
    try:
        conn.execute("BEGIN IMMEDIATE")
        if _needs_autoincrement(conn):  # another connection may have done it while this one waited
            before = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            conn.execute(ITEMS.format(name="items_new"))
            conn.execute(f"INSERT INTO items_new ({COLUMNS}) SELECT {COLUMNS} FROM items")
            conn.execute("DROP TABLE items")  # its index goes with it
            conn.execute("ALTER TABLE items_new RENAME TO items")
            conn.execute("CREATE INDEX IF NOT EXISTS items_open ON items (cleared_at, done_at)")
            if conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] != before:
                raise sqlite3.DatabaseError("items rebuild lost rows")
            conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('items', 'items_new')")
            conn.execute("INSERT INTO sqlite_sequence (name, seq) SELECT 'items', COALESCE(MAX(id), 0) FROM items")
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        print(f"lista: AUTOINCREMENT migration skipped: {e!r}", flush=True)


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA)
    _autoincrement(conn)
    return conn
