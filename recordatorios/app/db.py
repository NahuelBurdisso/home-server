"""SQLite storage. One file in /data, so the nightly restic backup picks it up.
Open it only from inside the container (docker exec recordatorios ...): the Mac's sqlite3 and the
container don't share the WAL index safely, and new connections then fail with "file is not a database"."""
import os
import sqlite3

DB_PATH = os.environ.get("RECORDATORIOS_DB", "/data/recordatorios.db")

# AUTOINCREMENT: an id is never handed out twice. Papeles keeps the id of the reminder it made (and
# deletes it when the paper changes), the Telegram bot keeps it for "borrar": a reused id would
# delete somebody else's reminder.
REMINDERS = """
CREATE TABLE IF NOT EXISTS {name} (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    notes       TEXT NOT NULL DEFAULT '',
    date        TEXT NOT NULL,              -- YYYY-MM-DD, the first (or only) time
    time        TEXT,                       -- HH:MM; NULL = all day (its alerts count from 9:00)
    repeat      TEXT NOT NULL DEFAULT 'none',  -- none, daily, weekly, monthly, yearly
    alerts      TEXT NOT NULL DEFAULT '0',  -- minutes before, comma separated (0 = at the time)
    done_until  TEXT,                       -- YYYY-MM-DDTHH:MM: occurrences up to here are done
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""
COLUMNS = "id, title, notes, date, time, repeat, alerts, done_until, created_at"

SCHEMA = REMINDERS.format(name="reminders") + """
-- "+1 h" / "Mañana" from a notification: one more alert for that occurrence.
CREATE TABLE IF NOT EXISTS snoozes (
    id          INTEGER PRIMARY KEY,
    reminder_id INTEGER NOT NULL REFERENCES reminders(id) ON DELETE CASCADE,
    occ         TEXT NOT NULL,              -- the occurrence it is about
    fire_at     TEXT NOT NULL
);

-- Alerts already on the phone, so a restart never sends one twice.
CREATE TABLE IF NOT EXISTS sent (
    reminder_id INTEGER NOT NULL,
    fire_at     TEXT NOT NULL,
    PRIMARY KEY (reminder_id, fire_at)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _needs_autoincrement(conn) -> bool:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'reminders'").fetchone()
    return row is not None and "AUTOINCREMENT" not in row[0].upper()


def _autoincrement(conn):
    """Databases made before AUTOINCREMENT: the reminders table is rebuilt once, in one transaction,
    keeping every id (snoozes and sent point at them). Foreign keys are off meanwhile, or dropping
    the old table would cascade to the snoozes. The counter starts after the highest id ever seen,
    including deleted reminders still in `sent`. If anything fails it rolls back and the app goes on
    with the old table (and tries again next time)."""
    if not _needs_autoincrement(conn):
        return
    conn.execute("PRAGMA foreign_keys = OFF")  # only takes effect outside a transaction
    try:
        conn.execute("BEGIN IMMEDIATE")
        if _needs_autoincrement(conn):  # another connection may have done it while this one waited
            before = conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
            conn.execute(REMINDERS.format(name="reminders_new"))
            conn.execute(f"INSERT INTO reminders_new ({COLUMNS}) SELECT {COLUMNS} FROM reminders")
            conn.execute("DROP TABLE reminders")
            conn.execute("ALTER TABLE reminders_new RENAME TO reminders")
            if conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] != before:
                raise sqlite3.DatabaseError("reminders rebuild lost rows")
            conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('reminders', 'reminders_new')")
            conn.execute("INSERT INTO sqlite_sequence (name, seq) SELECT 'reminders', MAX(COALESCE(m, 0)) FROM ("
                         " SELECT MAX(id) AS m FROM reminders UNION ALL SELECT MAX(reminder_id) FROM sent"
                         " UNION ALL SELECT MAX(reminder_id) FROM snoozes)")
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        print(f"recordatorios: AUTOINCREMENT migration skipped: {e!r}", flush=True)


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")  # the alert thread writes too
    conn.executescript(SCHEMA)
    _autoincrement(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_meta(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn, key: str, value: str):
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))
