"""SQLite storage. One file in /data, so the nightly restic backup picks it up (a VACUUM INTO copy
from setup/sqlite-dumps.sh). Open it only from inside the container (docker exec vigia ...)."""
import os
import sqlite3

DB_PATH = os.environ.get("VIGIA_DB", "/data/vigia.db")

# AUTOINCREMENT everywhere: an id is never handed out twice, so the Telegram bot's "borrar" (it keeps
# the ids it added) can't take away something added later on the page.
SCHEMA = """
-- A page checked on a schedule. kind: cambio (its text), precio (a number), texto (a needle).
CREATE TABLE IF NOT EXISTS watches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    url         TEXT NOT NULL,
    kind        TEXT NOT NULL,                -- cambio, precio, texto
    selector    TEXT NOT NULL DEFAULT '',     -- CSS; empty: the whole page
    needle      TEXT NOT NULL DEFAULT '',     -- texto: what to look for
    cuando      TEXT NOT NULL DEFAULT 'aparece',  -- texto: aparece or desaparece
    objetivo    REAL,                         -- precio: alert when it goes under this
    umbral      REAL NOT NULL DEFAULT 1,      -- precio: % drop that is news
    every_min   INTEGER NOT NULL DEFAULT 60,
    active      INTEGER NOT NULL DEFAULT 1,
    added_by    TEXT NOT NULL DEFAULT '',
    chat        TEXT,                         -- the Telegram chat to answer; NULL: added on the page
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    last_check  TEXT,                         -- the last try, good or bad
    last_ok     TEXT,
    fails       INTEGER NOT NULL DEFAULT 0,   -- tries in a row that failed
    last_error  TEXT,
    value_num   REAL,                         -- precio: the price; texto: 1 present, 0 absent
    value_text  TEXT,                         -- cambio: the text; precio/texto: what it read, for the card
    last_change TEXT
);

-- One row per change, plus one per day (the price line and the history page).
CREATE TABLE IF NOT EXISTS checks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id  INTEGER NOT NULL,
    ts        TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    value_num REAL,
    changed   INTEGER NOT NULL DEFAULT 0,
    note      TEXT                            -- the diff, or what it read
);
CREATE INDEX IF NOT EXISTS checks_watch ON checks (watch_id, ts);

-- Something kept: an article, a video's captions, a long voice note or a note.
CREATE TABLE IF NOT EXISTS saved (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    url        TEXT,                          -- normalized; NULL for notes and audios
    title      TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL,                 -- articulo, video, audio, nota
    text       TEXT NOT NULL DEFAULT '',
    summary    TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'pendiente',  -- pendiente, listo, sin_resumen, error
    error      TEXT,
    added_by   TEXT NOT NULL DEFAULT '',
    chat       TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    tried_at   TEXT                           -- the last summary try (sin_resumen waits 15 min)
);
CREATE INDEX IF NOT EXISTS saved_url ON saved (url);
CREATE INDEX IF NOT EXISTS saved_status ON saved (status);

-- Full-text search over saved, kept in step by the triggers. remove_diacritics: "cancion" finds
-- "canción" and the other way around.
CREATE VIRTUAL TABLE IF NOT EXISTS saved_fts USING fts5(
    title, summary, text, content='saved', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS saved_ai AFTER INSERT ON saved BEGIN
    INSERT INTO saved_fts (rowid, title, summary, text) VALUES (new.id, new.title, new.summary, new.text);
END;
CREATE TRIGGER IF NOT EXISTS saved_ad AFTER DELETE ON saved BEGIN
    INSERT INTO saved_fts (saved_fts, rowid, title, summary, text) VALUES ('delete', old.id, old.title, old.summary, old.text);
END;
CREATE TRIGGER IF NOT EXISTS saved_au AFTER UPDATE OF title, summary, text ON saved BEGIN
    INSERT INTO saved_fts (saved_fts, rowid, title, summary, text) VALUES ('delete', old.id, old.title, old.summary, old.text);
    INSERT INTO saved_fts (rowid, title, summary, text) VALUES (new.id, new.title, new.summary, new.text);
END;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass  # another thread is switching it right now (a new file, at start-up): WAL sticks to the file
    conn.executescript(SCHEMA)
    return conn


def get_meta(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn, key: str, value: str | None):
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                 (key, value))
    conn.commit()
